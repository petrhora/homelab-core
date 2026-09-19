import pytest
from httpx import ASGITransport, AsyncClient

from homelab_core.api.server import RgbUpdate, create_app
from homelab_core.config import AppConfig
from homelab_core.devices.ws2812 import Ws2812SpiDevice
from homelab_core.runtime.commands import CommandTimeoutError
from homelab_core.runtime.supervisor import Supervisor


@pytest.mark.asyncio
async def test_read_only_api(monkeypatch, tmp_path):
    async def unavailable_spi(self):
        raise RuntimeError("SPI device unavailable")

    monkeypatch.setattr(Ws2812SpiDevice, "clear", unavailable_spi)
    supervisor = Supervisor(AppConfig(persistence_path=tmp_path / "state.json"))
    await supervisor.start()
    try:
        async with AsyncClient(transport=ASGITransport(app=create_app(supervisor)), base_url="http://test") as client:
            response = await client.get("/api/v1/health")
            assert response.status_code == 200
            assert response.json()["managers"]["system"]["status"] == "ready"
            assert response.json()["managers"]["rgb"]["status"] == "degraded"
            assert (await client.get("/api/v1/overview")).status_code == 200
            async def process_page(offset, limit):
                return {"items": [{"pid": 2, "name": "demo", "status": "running", "username": "user"}],
                        "offset": offset, "limit": limit, "has_more": False}

            monkeypatch.setattr(supervisor.managers["system"], "processes", process_page)
            processes = await client.get("/api/v1/system/processes?offset=4&limit=7")
            assert processes.status_code == 200
            assert processes.json()["offset"] == 4
            assert processes.json()["limit"] == 7
            assert "cmdline" not in processes.json()["items"][0]
            rgb = await client.get("/api/v1/rgb")
            assert rgb.status_code == 200
            oled = await client.get("/api/v1/oled")
            assert oled.status_code == 200
            oled_update = await client.post("/api/v1/oled", json={"enabled": False})
            assert oled_update.status_code == 200
            fans = await client.get("/api/v1/fans")
            assert fans.status_code == 200
            assert fans.json()["actual"]["led"]["output_enabled"] is False
            lighting = await client.get("/api/v1/lighting")
            assert lighting.status_code == 200
            update_lighting = await client.post("/api/v1/lighting", json={
                "enabled": False, "timezone": "Europe/Prague",
                "windows": [{"start": "07:00", "end": "23:30", "days": ["mon"]}],
            })
            assert update_lighting.status_code == 200
            assert update_lighting.json()["desired"]["timezone"] == "Europe/Prague"
            power_button = await client.get("/api/v1/power-button")
            assert power_button.status_code == 200
            assert power_button.json()["desired"]["enabled"] is False
            assert (await client.post("/api/v1/fans", json={"mode": "on"})).status_code == 503
            update = await client.post("/api/v1/rgb", json={"enabled": False, "color": "#112233"})
            assert update.status_code == 503
            assert (await client.post("/api/v1/rgb", json={"color": "blue"})).status_code == 422
            assert "homelab_cpu_usage_ratio" in (await client.get("/metrics")).text

            async def timed_out(*args, **kwargs):
                raise CommandTimeoutError("rgb.update", "rgb-device", 10)

            monkeypatch.setattr(supervisor.commands, "dispatch", timed_out)
            timeout = await client.post("/api/v1/rgb", json={"enabled": True})
            assert timeout.status_code == 504
    finally:
        await supervisor.stop()


@pytest.mark.asyncio
async def test_lighting_update_reports_hardware_failure(monkeypatch):
    supervisor = Supervisor(AppConfig())

    async def unavailable(*args, **kwargs):
        raise RuntimeError("RGB unavailable")

    monkeypatch.setattr(supervisor.commands, "dispatch", unavailable)
    async with AsyncClient(transport=ASGITransport(app=create_app(supervisor)), base_url="http://test") as client:
        response = await client.post("/api/v1/lighting", json={"enabled": False})

    assert response.status_code == 503
    assert response.json()["detail"] == "RGB unavailable"


@pytest.mark.asyncio
async def test_update_reports_persistence_failure(monkeypatch, tmp_path):
    supervisor = Supervisor(AppConfig(persistence_path=tmp_path / "state.json"))
    await supervisor.start()

    async def unavailable(*args, **kwargs):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(supervisor.persistence, "update", unavailable)
    try:
        async with AsyncClient(transport=ASGITransport(app=create_app(supervisor)), base_url="http://test") as client:
            response = await client.post("/api/v1/lighting", json={"enabled": False})
    finally:
        await supervisor.stop()

    assert response.status_code == 503
    assert "Could not persist lighting state" in response.json()["detail"]


@pytest.mark.asyncio
async def test_api_updates_are_persisted_and_can_be_cleared(tmp_path):
    supervisor = Supervisor(AppConfig(persistence_path=tmp_path / "state.json"))
    await supervisor.start()
    try:
        async with AsyncClient(transport=ASGITransport(app=create_app(supervisor)), base_url="http://test") as client:
            response = await client.post("/api/v1/lighting", json={"enabled": False, "timezone": "Europe/Prague"})
            assert response.status_code == 200
            assert (tmp_path / "state.json").exists()
            assert "lighting" in (tmp_path / "state.json").read_text()

            cleared = await client.delete("/api/v1/state")
            assert cleared.status_code == 200
            assert cleared.json() == {"cleared": True, "effective_after_restart": True}
            assert not (tmp_path / "state.json").exists()
    finally:
        await supervisor.stop()


@pytest.mark.asyncio
async def test_oled_api_update_is_persisted_and_restored(tmp_path):
    config = AppConfig(persistence_path=tmp_path / "state.json")
    supervisor = Supervisor(config)
    await supervisor.start()
    try:
        async with AsyncClient(transport=ASGITransport(app=create_app(supervisor)), base_url="http://test") as client:
            response = await client.post("/api/v1/oled", json={"enabled": False, "sleep_timeout_seconds": 30})
            assert response.status_code == 200
    finally:
        await supervisor.stop()

    restored = Supervisor(config)
    await restored.start()
    try:
        desired = (await restored.managers["oled"].snapshot())["desired"]
        assert desired["enabled"] is False
        assert desired["sleep_timeout_seconds"] == 30
    finally:
        await restored.stop()


@pytest.mark.parametrize("pattern", [
    {"type": "flow", "pixels": ["#FFFFFF"] * 1025},
    {"type": "gradient", "colors": ["#FFFFFF"] * 1025},
    {"type": "rainbow", "virtual_led_count": 1025},
])
def test_rgb_update_rejects_oversized_pattern_sources(pattern):
    with pytest.raises(ValueError):
        RgbUpdate.model_validate({"pattern": pattern})
