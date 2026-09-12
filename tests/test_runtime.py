import asyncio
import sys
import types

import pytest

from homelab_core.config import AppConfig, RgbConfig, load_config
from homelab_core.devices.interfaces import FakeRgbDevice
from homelab_core.devices.ws2812 import Ws2812SpiDevice
from homelab_core.managers.base import Manager
from homelab_core.managers.rgb import RgbManager
from homelab_core.runtime.commands import Command, CommandBus
from homelab_core.runtime.events import Event, EventBus
from homelab_core.runtime.state import StateStore
from homelab_core.runtime.supervisor import Supervisor


def test_default_config(tmp_path):
    config = load_config(tmp_path / "missing.toml")
    assert config.api.host == "127.0.0.1"
    assert config.api.port == 8000
    assert config.rgb.led_count == 4


def test_unknown_config_is_rejected(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[api]\nunsafe = true\n")
    with pytest.raises(Exception):
        load_config(path)


def test_invalid_rgb_color_is_rejected(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[rgb]\ncolor = "blue"\n')

    with pytest.raises(Exception):
        load_config(path)


def test_lowercase_rgb_color_is_accepted(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[rgb]\ncolor = "#a1b2c3"\n')

    assert load_config(path).rgb.color == "#a1b2c3"


@pytest.mark.asyncio
async def test_ws2812_clear_opens_spi_and_sends_configured_pixel_count(monkeypatch):
    class Spi:
        def __init__(self):
            self.frames = []

        def open(self, bus, device):
            self.opened = (bus, device)

        def xfer2(self, payload):
            self.frames.append(payload)

        def close(self):
            self.closed = True

    spi = Spi()
    monkeypatch.setitem(sys.modules, "spidev", types.SimpleNamespace(SpiDev=lambda: spi))

    device = Ws2812SpiDevice("/dev/spidev0.0", 2_400_000, pixel_count=2)
    await device.clear()

    assert spi.opened == (0, 0)
    assert len(spi.frames) == 1
    assert len(spi.frames[0]) == 24 + (2 * 9) + 24


@pytest.mark.asyncio
async def test_ws2812_clear_blanks_previous_larger_frame_after_count_reduction(monkeypatch):
    class Spi:
        def __init__(self):
            self.frames = []

        def open(self, bus, device):
            pass

        def xfer2(self, payload):
            self.frames.append(payload)

        def close(self):
            pass

    spi = Spi()
    monkeypatch.setitem(sys.modules, "spidev", types.SimpleNamespace(SpiDev=lambda: spi))

    device = Ws2812SpiDevice("/dev/spidev0.0", 2_400_000, pixel_count=2)
    await device.set_pixels([(255, 0, 0)] * 4)
    await device.set_pixels([(0, 255, 0)] * 2)
    await device.clear()

    assert len(spi.frames[-1]) == 24 + (4 * 9) + 24


@pytest.mark.asyncio
async def test_state_is_copied():
    state = StateStore()
    value = {"nested": {"ok": True}}
    await state.update("test", value)
    value["nested"]["ok"] = False
    assert (await state.snapshot())["test"]["nested"]["ok"] is True


@pytest.mark.asyncio
async def test_event_bus_publishes():
    bus = EventBus()
    queue = bus.subscribe()
    await bus.publish(Event("test", {"value": 1}))
    assert (await queue.get()).payload["value"] == 1


@pytest.mark.asyncio
async def test_commands_are_serialized():
    bus = CommandBus()
    active = 0
    maximum = 0

    async def handler(command):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1

    bus.register("test", handler)
    await asyncio.gather(*(bus.dispatch("test", {}) for _ in range(5)))
    assert maximum == 1


@pytest.mark.asyncio
async def test_commands_sharing_resource_are_serialized():
    bus = CommandBus()
    active = 0
    maximum = 0
    resource_ids = []

    async def handler(command):
        nonlocal active, maximum
        resource_ids.append(command.resource_id)
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1

    bus.register("rgb.update", handler, resource_id="rgb-device")
    bus.register("rgb.clear", handler, resource_id="rgb-device")
    await asyncio.gather(
        bus.dispatch("rgb.update", {}),
        bus.dispatch("rgb.clear", {}),
    )

    assert maximum == 1
    assert resource_ids == ["rgb-device", "rgb-device"]


@pytest.mark.asyncio
async def test_commands_on_different_resources_can_run_concurrently():
    bus = CommandBus()
    active = 0
    maximum = 0

    async def handler(command):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1

    bus.register("rgb.update", handler, resource_id="rgb-device")
    bus.register("oled.update", handler, resource_id="oled-device")
    await asyncio.gather(
        bus.dispatch("rgb.update", {}),
        bus.dispatch("oled.update", {}),
    )

    assert maximum == 2


@pytest.mark.asyncio
async def test_command_timeout_includes_resource_queue_wait():
    bus = CommandBus()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def handler(command):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await release.wait()

    bus.register("rgb.update", handler, resource_id="rgb-device")
    first = asyncio.create_task(bus.dispatch("rgb.update", {}, timeout=1))
    await started.wait()
    second = asyncio.create_task(bus.dispatch("rgb.update", {}, timeout=0.01))

    with pytest.raises(TimeoutError):
        await second

    release.set()
    await first
    await bus.dispatch("rgb.update", {}, timeout=0.1)
    assert calls == 2


@pytest.mark.asyncio
async def test_rgb_applies_brightness_to_all_pixels():
    state = StateStore()
    device = FakeRgbDevice()
    manager = RgbManager(AppConfig().rgb, state, device)
    await manager.start()
    result = await manager.execute(Command("rgb.update", "rgb-device", {
        "enabled": True, "color": "#804020", "brightness": 0.5, "led_count": 2
    }, "test", 1))
    assert result["desired"]["color"] == "#804020"
    assert device.pixels == [(64, 32, 16), (64, 32, 16)]
    await manager.stop()
    assert device.closed


@pytest.mark.asyncio
async def test_rgb_rejects_partial_update_without_mutating_state():
    state = StateStore()
    device = FakeRgbDevice()
    manager = RgbManager(RgbConfig(enabled=True, color="#102030", brightness=0.5, led_count=2), state, device)
    await manager.start()
    before = await manager.snapshot()

    with pytest.raises(ValueError, match="brightness must be between 0 and 1"):
        await manager.execute(Command("rgb.update", "rgb-device", {
            "enabled": False, "color": "#AABBCC", "brightness": 2, "led_count": 4
        }, "test", 1))

    after = await manager.snapshot()
    assert after["desired"] == before["desired"]
    assert after["actual"] == before["actual"]
    assert device.pixels == [(8, 16, 24), (8, 16, 24)]


@pytest.mark.asyncio
async def test_rgb_clears_pixels_when_disabled_at_startup():
    state = StateStore()
    device = FakeRgbDevice()
    device.pixels = [(255, 0, 0), (255, 0, 0)]
    manager = RgbManager(RgbConfig(enabled=False), state, device)

    await manager.start()

    assert device.pixels == [(0, 0, 0), (0, 0, 0)]


@pytest.mark.asyncio
async def test_rgb_handles_unavailable_hardware_when_disabled_at_startup():
    class FailingRgbDevice(FakeRgbDevice):
        async def clear(self) -> None:
            raise RuntimeError("RGB hardware unavailable")

    state = StateStore()
    manager = RgbManager(RgbConfig(enabled=False), state, FailingRgbDevice())

    await manager.start()

    health = await manager.health()
    assert health["status"] == "degraded"
    assert health["error"] == "RGB hardware unavailable"


@pytest.mark.asyncio
async def test_rgb_shutdown_publishes_stopped_after_cleanup_failure():
    class FailingRgbDevice(FakeRgbDevice):
        async def clear(self) -> None:
            raise RuntimeError("RGB hardware unavailable")

    state = StateStore()
    manager = RgbManager(RgbConfig(enabled=False), state, FailingRgbDevice())

    await manager.stop()

    assert (await manager.health())["status"] == "stopped"
    published = (await state.snapshot())["rgb"]
    current = await manager.snapshot()
    assert published["desired"] == current["desired"]
    assert published["actual"] == current["actual"]
    assert published["operation"] == current["operation"]


@pytest.mark.asyncio
async def test_supervisor_continues_stopping_managers_after_failure():
    class StubManager(Manager):
        def __init__(self, name: str, fails: bool = False) -> None:
            self.name = name
            self.fails = fails
            self.stopped = False

        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            self.stopped = True
            if self.fails:
                raise RuntimeError("shutdown failed")

        async def health(self) -> dict:
            return {}

        async def snapshot(self) -> dict:
            return {}

    supervisor = Supervisor(AppConfig())
    failing = StubManager("failing", fails=True)
    succeeding = StubManager("succeeding")
    supervisor.managers = {"failing": failing, "succeeding": succeeding}

    await supervisor.stop()

    assert failing.stopped
    assert succeeding.stopped


def test_ws2812_encodes_grb_pixels():
    assert Ws2812SpiDevice._encode_pixels([(0x01, 0x02, 0x04)]) == [
        0x92, 0x49, 0x34, 0x92, 0x49, 0x26, 0x92, 0x49, 0xA4
    ]
