import asyncio
import contextlib
import sys
import time
import types

import pytest

import homelab_core.managers.rgb as rgb_module
import homelab_core.managers.system as system_module
from homelab_core.config import AppConfig, ExternalFanConfig, FanLedConfig, FansConfig, RgbConfig, load_config
from homelab_core.devices.fan import GpioFanDevice
from homelab_core.devices.interfaces import FakeFanDevice, FakeRgbDevice
from homelab_core.devices.ws2812 import Ws2812SpiDevice
from homelab_core.managers.base import Manager
from homelab_core.managers.fans import FanManager
from homelab_core.managers.rgb import RgbManager
from homelab_core.managers.system import SystemManager
from homelab_core.rgb_patterns import apply_brightness, normalize_pattern
from homelab_core.runtime.commands import Command, CommandBus, CommandBusClosedError, CommandTimeoutError
from homelab_core.runtime.events import Event, EventBus, EventBusClosedError
from homelab_core.runtime.state import StateStore
from homelab_core.runtime.supervisor import Supervisor


def test_default_config(tmp_path):
    config = load_config(tmp_path / "missing.toml")
    assert config.api.host == "127.0.0.1"
    assert config.api.port == 8000
    assert config.rgb.led_count == 4
    assert config.fans.enabled is False


def test_invalid_fan_temperature_thresholds_are_rejected():
    with pytest.raises(ValueError):
        FansConfig(external=ExternalFanConfig(on_temperature_c=50, off_temperature_c=55))


@pytest.mark.asyncio
async def test_gpio_fan_initializes_active_low_outputs_inactive(monkeypatch):
    class Gpio:
        BCM = "BCM"
        OUT = "OUT"
        LOW = 0
        HIGH = 1

        def __init__(self):
            self.setups = []
            self.outputs = []

        def setmode(self, mode):
            self.mode = mode

        def setup(self, pin, mode, initial):
            self.setups.append((pin, mode, initial))

        def output(self, pin, value):
            self.outputs.append((pin, value))

        def cleanup(self, pins):
            self.cleaned = pins

    gpio = Gpio()
    monkeypatch.setitem(sys.modules, "RPi", types.SimpleNamespace(GPIO=gpio))
    device = GpioFanDevice(6, 5, fan_active_high=False, led_active_high=False)

    await device.set_fan(True)
    await device.set_led(True)

    assert gpio.setups == [(6, gpio.OUT, gpio.HIGH), (5, gpio.OUT, gpio.HIGH)]
    assert gpio.outputs == [(6, gpio.LOW), (5, gpio.LOW)]


@pytest.mark.asyncio
async def test_fans_auto_mode_hysteresis_and_independent_led():
    temperature = 54.0

    async def read_temperature():
        return temperature

    config = FansConfig(enabled=True, poll_interval_seconds=3600,
                        external=ExternalFanConfig(mode="auto", on_temperature_c=55, off_temperature_c=50),
                        led=FanLedConfig(mode="on"))
    device = FakeFanDevice()
    manager = FanManager(config, StateStore(), device, read_temperature)
    await manager.start()
    assert device.fan_enabled is False
    assert device.led_enabled is True

    temperature = 55.0
    await manager.execute(Command("fans.update", "fan-gpio", {}, "test", 1))
    assert device.fan_enabled is True
    assert device.led_enabled is True

    temperature = 51.0
    await manager.execute(Command("fans.update", "fan-gpio", {}, "test", 1))
    assert device.fan_enabled is True

    temperature = 50.0
    snapshot = await manager.execute(Command("fans.update", "fan-gpio", {"led_mode": "follow"}, "test", 1))
    assert device.fan_enabled is False
    assert device.led_enabled is False
    assert snapshot["actual"]["temperature_celsius"] == 50.0
    await manager.stop()
    assert device.closed


@pytest.mark.asyncio
async def test_fans_force_on_and_recover_after_temperature_sensor_failure():
    temperature: float | None = None

    async def read_temperature():
        if temperature is None:
            raise OSError("temperature sensor unavailable")
        return temperature

    config = FansConfig(enabled=True, poll_interval_seconds=0.001,
                        external=ExternalFanConfig(mode="auto"), led=FanLedConfig(mode="follow"))
    device = FakeFanDevice()
    manager = FanManager(config, StateStore(), device, read_temperature)
    await manager.start()

    assert device.fan_enabled is True
    assert device.led_enabled is True
    assert (await manager.health())["status"] == "degraded"
    assert (await manager.snapshot())["actual"]["temperature_celsius"] is None

    temperature = 40.0
    while (await manager.health())["status"] != "ready":
        await asyncio.sleep(0)
    assert device.fan_enabled is False
    assert device.led_enabled is False
    await manager.stop()


@pytest.mark.asyncio
async def test_fans_manual_off_does_not_depend_on_temperature_sensor():
    async def unavailable_temperature():
        raise AssertionError("manual fan mode must not read the temperature sensor")

    config = FansConfig(enabled=True, external=ExternalFanConfig(mode="off"), led=FanLedConfig(mode="follow"))
    device = FakeFanDevice()
    manager = FanManager(config, StateStore(), device, unavailable_temperature)
    await manager.start()

    assert device.fan_enabled is False
    assert device.led_enabled is False
    assert (await manager.health())["status"] == "ready"
    await manager.stop()


@pytest.mark.asyncio
async def test_fan_poll_cannot_overwrite_api_update():
    class BlockingFanDevice(FakeFanDevice):
        def __init__(self) -> None:
            super().__init__()
            self.fan_write_started = asyncio.Event()
            self.release_fan_write = asyncio.Event()

        async def set_fan(self, enabled: bool) -> None:
            if enabled and not self.fan_write_started.is_set():
                self.fan_write_started.set()
                await self.release_fan_write.wait()
            await super().set_fan(enabled)

    async def read_temperature():
        return 60.0

    config = FansConfig(enabled=True, poll_interval_seconds=0.001,
                        external=ExternalFanConfig(mode="auto"), led=FanLedConfig(mode="follow"))
    device = BlockingFanDevice()
    manager = FanManager(config, StateStore(), device, read_temperature)
    poll_task = asyncio.create_task(manager._poll())
    await asyncio.wait_for(device.fan_write_started.wait(), 1)
    command_task = asyncio.create_task(manager.execute(Command("fans.update", "fan-gpio", {"mode": "off"}, "test", 1)))
    await asyncio.sleep(0)
    assert not command_task.done()

    device.release_fan_write.set()
    await asyncio.wait_for(command_task, 1)
    poll_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await poll_task

    assert device.fan_enabled is False


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
async def test_system_health_recovers_after_transient_poll_failure(monkeypatch):
    config = AppConfig().system.model_copy(update={"poll_interval_seconds": 0.001})
    state = StateStore()
    manager = SystemManager(config, state)
    calls = 0

    def collect():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("temporary system collection failure")
        return {"collection": calls}

    monkeypatch.setattr(manager, "_collect", collect)
    await manager.start()

    async def wait_for_status(status):
        while (await manager.health())["status"] != status:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_for_status("degraded"), 1)
    health = await manager.health()
    assert health["error"] == "temporary system collection failure"
    assert (await state.snapshot())["system"] == {"collection": 1}

    async def wait_for_recovery():
        while (await manager.health())["status"] != "ready":
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_for_recovery(), 1)
    assert (await manager.health())["error"] is None
    assert (await state.snapshot())["system"] == {"collection": 3}
    await manager.stop()


@pytest.mark.asyncio
async def test_system_collection_does_not_block_event_loop(monkeypatch):
    manager = SystemManager(AppConfig().system, StateStore())

    def collect():
        time.sleep(0.05)
        return {"collection": "complete"}

    monkeypatch.setattr(manager, "_collect", collect)
    starting = asyncio.create_task(manager.start())
    progress = 0

    async def tick():
        nonlocal progress
        while not starting.done():
            progress += 1
            await asyncio.sleep(0)

    ticker = asyncio.create_task(tick())
    await starting
    await ticker

    assert progress > 0
    await manager.stop()


@pytest.mark.asyncio
async def test_system_processes_redacts_cmdline_and_bounds_iteration(monkeypatch):
    manager = SystemManager(AppConfig().system, StateStore())
    yielded = 0
    fields_requested = None

    class Process:
        def __init__(self, pid):
            self.info = {"pid": pid, "name": f"process-{pid}", "status": "running",
                         "username": "user", "cmdline": ["--secret"]}

    def process_iter(fields):
        nonlocal yielded, fields_requested
        fields_requested = fields
        for pid in range(5):
            yielded += 1
            yield Process(pid)

    monkeypatch.setattr(system_module.psutil, "process_iter", process_iter)

    page = await manager.processes(offset=1, limit=2)

    assert [item["pid"] for item in page["items"]] == [1, 2]
    assert page["has_more"] is True
    assert page["offset"] == 1
    assert page["limit"] == 2
    assert "cmdline" not in page["items"][0]
    assert "cmdline" not in fields_requested
    assert yielded == 4


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
async def test_event_bus_unsubscribes_and_closes():
    bus = EventBus()
    queue = bus.subscribe()
    bus.unsubscribe(queue)
    await bus.publish(Event("test", {"value": 1}))
    assert queue.empty()

    bus.close()
    with pytest.raises(EventBusClosedError):
        bus.subscribe()
    with pytest.raises(EventBusClosedError):
        await bus.publish(Event("test"))


@pytest.mark.asyncio
async def test_event_bus_drops_oldest_event_for_slow_subscriber():
    bus = EventBus(queue_size=2)
    slow_subscriber = bus.subscribe()

    await asyncio.wait_for(bus.publish(Event("test", {"value": 1})), 0.01)
    await asyncio.wait_for(bus.publish(Event("test", {"value": 2})), 0.01)
    await asyncio.wait_for(bus.publish(Event("test", {"value": 3})), 0.01)

    assert (await slow_subscriber.get()).payload["value"] == 2
    assert (await slow_subscriber.get()).payload["value"] == 3


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
    assert calls == 3


@pytest.mark.asyncio
async def test_command_timeout_leaves_handler_running():
    bus = CommandBus()
    started = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()

    async def handler(command):
        started.set()
        await release.wait()
        completed.set()
        return {"completed": True}

    bus.register("rgb.update", handler, resource_id="rgb-device")
    operation = asyncio.create_task(bus.dispatch("rgb.update", {}, timeout=0.001))
    await started.wait()

    with pytest.raises(CommandTimeoutError):
        await operation
    assert not release.is_set()

    release.set()
    await asyncio.wait_for(completed.wait(), 1)


@pytest.mark.asyncio
async def test_command_bus_shutdown_drains_active_work_and_cancels_queued_work():
    bus = CommandBus()
    started = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()
    queued_called = False

    async def handler(command):
        nonlocal queued_called
        if command.payload["active"]:
            started.set()
            await release.wait()
            completed.set()
        else:
            queued_called = True

    bus.register("fans.update", handler, resource_id="fan-gpio")
    active = asyncio.create_task(bus.dispatch("fans.update", {"active": True}, timeout=0.001))
    await started.wait()
    with pytest.raises(CommandTimeoutError):
        await active
    queued = asyncio.create_task(bus.dispatch("fans.update", {"active": False}))
    await asyncio.sleep(0)

    stopping = asyncio.create_task(bus.shutdown())
    await asyncio.sleep(0)
    assert not stopping.done()
    release.set()
    await stopping
    assert completed.is_set()
    assert queued_called is False
    with pytest.raises(asyncio.CancelledError):
        await queued
    with pytest.raises(CommandBusClosedError):
        await bus.dispatch("fans.update", {})


@pytest.mark.asyncio
async def test_supervisor_stops_managers_after_active_command_drains():
    supervisor = Supervisor(AppConfig())
    started = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()
    manager_stopped = asyncio.Event()

    async def handler(command):
        started.set()
        await release.wait()
        completed.set()

    class Manager:
        name = "fans"

        async def start(self):
            pass

        async def stop(self):
            assert completed.is_set()
            manager_stopped.set()

        async def health(self):
            return {"status": "ready"}

        async def snapshot(self):
            return {}

    supervisor.managers = {"fans": Manager()}
    supervisor.commands.register("fans.update", handler, resource_id="fan-gpio")
    operation = asyncio.create_task(supervisor.commands.dispatch("fans.update", {}, timeout=0.001))
    await started.wait()
    with pytest.raises(CommandTimeoutError):
        await operation

    stopping = asyncio.create_task(supervisor.stop())
    await asyncio.sleep(0)
    assert not manager_stopped.is_set()
    release.set()
    await stopping
    assert manager_stopped.is_set()


@pytest.mark.asyncio
async def test_supervisor_rolls_back_started_managers_after_start_failure():
    events = []

    class Manager:
        def __init__(self, name, error=None):
            self.name = name
            self.error = error

        async def start(self):
            events.append(f"start:{self.name}")
            if self.error is not None:
                raise self.error

        async def stop(self):
            events.append(f"stop:{self.name}")

        async def health(self):
            return {"status": "ready"}

        async def snapshot(self):
            return {}

    supervisor = Supervisor(AppConfig())
    first = Manager("first")
    failure = RuntimeError("second manager failed")
    supervisor.managers = {"first": first, "second": Manager("second", failure)}

    with pytest.raises(RuntimeError, match="second manager failed"):
        await supervisor.start()

    assert events == ["start:first", "start:second", "stop:first"]


@pytest.mark.asyncio
async def test_rgb_update_reconciles_after_direct_cancellation_during_io():
    class SlowRgbDevice(FakeRgbDevice):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def set_pixels(self, pixels):
            self.started.set()
            await self.release.wait()
            await super().set_pixels(pixels)

    state = StateStore()
    device = SlowRgbDevice()
    manager = RgbManager(RgbConfig(enabled=False, led_count=2), state, device)
    await manager.start()
    update = asyncio.create_task(manager.execute(Command("rgb.update", "rgb-device", {
        "enabled": True, "color": "#010203"
    }, "test", 1)))
    await device.started.wait()

    update.cancel()
    await asyncio.sleep(0)
    assert not update.done()
    device.release.set()
    with pytest.raises(asyncio.CancelledError):
        await update

    snapshot = await manager.snapshot()
    assert snapshot["desired"]["color"] == "#010203"
    assert snapshot["actual"]["color"] == "#010203"
    assert device.pixels == [(1, 2, 3), (1, 2, 3)]
    await manager.stop()


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
async def test_rgb_solid_supports_per_pixel_colors_and_brightness():
    state = StateStore()
    device = FakeRgbDevice()
    manager = RgbManager(RgbConfig(enabled=False, led_count=3), state, device)
    await manager.start()
    result = await manager.execute(Command("rgb.update", "rgb-device", {
        "enabled": True,
        "brightness": 0.5,
        "pattern": {"type": "solid", "pixels": [
            "#FF0000", {"color": "#00FF00", "brightness": 0.25}, "#0000FF"
        ]},
    }, "test", 1))

    assert device.pixels == [(128, 0, 0), (0, 32, 0), (0, 0, 128)]
    assert result["desired"]["pattern"]["type"] == "solid"
    await manager.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("pattern_type", ["solid", "breathing", "flow"])
async def test_rgb_led_count_update_regenerates_uniform_retained_pattern(pattern_type):
    state = StateStore()
    device = FakeRgbDevice()
    manager = RgbManager(RgbConfig(enabled=False, led_count=2), state, device)
    await manager.start()
    await manager.execute(Command("rgb.update", "rgb-device", {
        "enabled": True, "pattern": {"type": pattern_type, "color": "#102030"}
    }, "test", 1))

    await manager.execute(Command("rgb.update", "rgb-device", {"led_count": 4}, "test", 1))

    assert len(device.pixels) == 4
    assert all(pixel == device.pixels[0] for pixel in device.pixels)
    assert (await manager.snapshot())["desired"]["led_count"] == 4
    await manager.stop()


@pytest.mark.asyncio
async def test_rgb_led_count_update_rejects_retained_per_pixel_pattern():
    state = StateStore()
    device = FakeRgbDevice()
    manager = RgbManager(RgbConfig(enabled=False, led_count=2), state, device)
    await manager.start()
    await manager.execute(Command("rgb.update", "rgb-device", {
        "enabled": True,
        "pattern": {"type": "solid", "pixels": ["#102030", "#405060"]},
    }, "test", 1))
    before = await manager.snapshot()

    with pytest.raises(ValueError, match="replacement pattern"):
        await manager.execute(Command("rgb.update", "rgb-device", {"led_count": 4}, "test", 1))

    after = await manager.snapshot()
    assert after["desired"] == before["desired"]
    assert device.pixels == [(16, 32, 48), (64, 80, 96)]
    await manager.stop()


def test_animated_patterns_render_normalized_frames():
    pattern = normalize_pattern({
        "type": "breathing", "color": "#804020", "period_ms": 1000,
        "min_intensity": 0.5, "max_intensity": 1.0,
    }, 2, (0, 0, 0))

    frame = apply_brightness(pattern.render(2, 0), 1.0)

    assert len(frame) == 2
    assert frame[0] == (64, 32, 16)
    assert frame[1] == (64, 32, 16)


@pytest.mark.asyncio
async def test_rgb_starts_and_replaces_animation_task():
    state = StateStore()
    device = FakeRgbDevice()
    manager = RgbManager(RgbConfig(enabled=False, led_count=2), state, device)
    await manager.start()

    await manager.execute(Command("rgb.update", "rgb-device", {
        "enabled": True, "pattern": {"type": "rainbow", "speed": 1}
    }, "test", 1))
    first_task = manager._animation_task
    assert first_task is not None

    await manager.execute(Command("rgb.update", "rgb-device", {
        "pattern": {"type": "solid", "color": "#010203"}
    }, "test", 1))
    assert first_task.cancelled()
    assert manager._animation_task is None
    assert device.pixels == [(1, 2, 3), (1, 2, 3)]
    await manager.stop()


@pytest.mark.asyncio
async def test_rgb_animation_reports_degraded_and_recovers(monkeypatch):
    monkeypatch.setattr(rgb_module, "ANIMATION_RETRY_INITIAL_SECONDS", 0.001)
    monkeypatch.setattr(rgb_module, "ANIMATION_RETRY_MAX_SECONDS", 0.002)

    class FlakyRgbDevice(FakeRgbDevice):
        def __init__(self):
            super().__init__()
            self.failed = asyncio.Event()
            self.recovered = asyncio.Event()
            self.calls = 0
            self.failures = 1

        async def set_pixels(self, pixels):
            self.calls += 1
            if self.calls > 1 and self.failures:
                self.failures -= 1
                self.failed.set()
                raise RuntimeError("temporary RGB failure")
            if self.calls > 2:
                self.recovered.set()
            await super().set_pixels(pixels)

    state = StateStore()
    device = FlakyRgbDevice()
    manager = RgbManager(RgbConfig(enabled=False, led_count=2), state, device)
    await manager.start()
    await manager.execute(Command("rgb.update", "rgb-device", {
        "enabled": True, "pattern": {"type": "rainbow"}
    }, "test", 1))

    await asyncio.wait_for(device.failed.wait(), 1)
    async def wait_for_degraded():
        while (await manager.health())["status"] != "degraded":
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_for_degraded(), 1)
    assert (await state.snapshot())["rgb"]["operation"]["error"] == "temporary RGB failure"
    await asyncio.wait_for(device.recovered.wait(), 1)
    async def wait_for_ready():
        while (await manager.health())["status"] != "ready":
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_for_ready(), 1)
    health = await manager.health()
    assert health["status"] == "ready"
    assert health["error"] is None
    assert health["error_count"] == 1
    assert manager._animation_task is not None and not manager._animation_task.done()
    await manager.stop()


@pytest.mark.asyncio
async def test_rgb_animation_keeps_retrying_permanent_device_failure(monkeypatch):
    monkeypatch.setattr(rgb_module, "ANIMATION_RETRY_INITIAL_SECONDS", 0.001)
    monkeypatch.setattr(rgb_module, "ANIMATION_RETRY_MAX_SECONDS", 0.002)

    class FailingRgbDevice(FakeRgbDevice):
        def __init__(self):
            super().__init__()
            self.calls = 0
            self.failed = asyncio.Event()

        async def set_pixels(self, pixels):
            self.calls += 1
            if self.calls == 1:
                await super().set_pixels(pixels)
                return
            self.failed.set()
            raise RuntimeError("permanent RGB failure")

    state = StateStore()
    device = FailingRgbDevice()
    manager = RgbManager(RgbConfig(enabled=False, led_count=2), state, device)
    await manager.start()

    await manager.execute(Command("rgb.update", "rgb-device", {
        "enabled": True, "pattern": {"type": "rainbow"}
    }, "test", 1))
    task = manager._animation_task
    assert task is not None
    await asyncio.wait_for(device.failed.wait(), 1)
    assert not task.done()
    async def wait_for_degraded():
        while (await manager.health())["status"] != "degraded":
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_for_degraded(), 1)
    await manager.stop()


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
