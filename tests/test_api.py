import pytest
from httpx import ASGITransport, AsyncClient

from homelab_core.api.server import create_app
from homelab_core.config import AppConfig
from homelab_core.devices.ws2812 import Ws2812SpiDevice
from homelab_core.runtime.commands import CommandTimeoutError
from homelab_core.runtime.supervisor import Supervisor


@pytest.mark.asyncio
async def test_read_only_api(monkeypatch):
    async def unavailable_spi(self):
        raise RuntimeError("SPI device unavailable")

    monkeypatch.setattr(Ws2812SpiDevice, "clear", unavailable_spi)
    supervisor = Supervisor(AppConfig())
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
