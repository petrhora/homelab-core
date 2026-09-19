import asyncio

import pytest

from homelab_core.config import AppConfig, PowerButtonConfig
from homelab_core.devices.power_button import FakePowerButtonDevice
from homelab_core.managers.power_button import PowerButtonManager
from homelab_core.runtime.events import EventBus
from homelab_core.runtime.state import StateStore


class FakeSystemActions:
    def __init__(self):
        self.shutdown_calls = 0
        self.reboot_calls = 0

    async def shutdown(self):
        self.shutdown_calls += 1

    async def reboot(self):
        self.reboot_calls += 1


@pytest.mark.asyncio
async def test_power_button_emits_single_click():
    device = FakePowerButtonDevice()
    events = EventBus()
    manager = PowerButtonManager(
        PowerButtonConfig(enabled=True, debounce_ms=0, double_click_window_ms=100),
        StateStore(), events, device, FakeSystemActions(),
    )
    queue = events.subscribe()
    await manager.start()
    await device.press()
    await device.release()

    event = await asyncio.wait_for(queue.get(), 1)
    assert event.type == "power_button.click"
    assert (await manager.snapshot())["last_event"]["type"] == "click"
    await manager.stop()


@pytest.mark.asyncio
async def test_power_button_emits_double_click_without_single_click():
    device = FakePowerButtonDevice()
    events = EventBus()
    manager = PowerButtonManager(
        PowerButtonConfig(enabled=True, debounce_ms=0, double_click_window_ms=100),
        StateStore(), events, device, FakeSystemActions(),
    )
    queue = events.subscribe()
    await manager.start()
    await device.press()
    await device.release()
    await device.press()
    await device.release()

    event = await asyncio.wait_for(queue.get(), 1)
    assert event.type == "power_button.double_click"
    assert queue.empty()
    await manager.stop()


@pytest.mark.asyncio
async def test_power_button_long_press_requests_shutdown_once():
    device = FakePowerButtonDevice()
    actions = FakeSystemActions()
    events = EventBus()
    manager = PowerButtonManager(
        PowerButtonConfig(enabled=True, debounce_ms=0, long_press_ms=500),
        StateStore(), events, device, actions,
    )
    queue = events.subscribe()
    await manager.start()
    await device.press()

    event = await asyncio.wait_for(queue.get(), 1)
    assert event.type == "power_button.long_press"
    assert actions.shutdown_calls == 1
    await device.release()
    await asyncio.sleep(0.05)
    assert actions.shutdown_calls == 1
    await manager.stop()


@pytest.mark.asyncio
async def test_power_button_prepares_hardware_before_shutdown():
    device = FakePowerButtonDevice()
    actions = FakeSystemActions()
    events = EventBus()
    order = []

    async def prepare(action):
        order.append(action)

    original_shutdown = actions.shutdown

    async def shutdown():
        order.append("poweroff")
        await original_shutdown()

    actions.shutdown = shutdown
    manager = PowerButtonManager(
        PowerButtonConfig(enabled=True, debounce_ms=0, long_press_ms=1),
        StateStore(), events, device, actions, prepare,
    )
    await manager.start()
    await device.press()
    await asyncio.sleep(0.05)

    assert order == ["shutdown", "poweroff"]
    await manager.stop()


@pytest.mark.asyncio
async def test_power_button_release_does_not_cancel_dispatched_shutdown():
    class BlockingSystemActions:
        def __init__(self):
            self.started = asyncio.Event()
            self.complete = asyncio.Event()
            self.finished = False

        async def shutdown(self):
            self.started.set()
            await self.complete.wait()
            self.finished = True

        async def reboot(self):
            raise AssertionError("reboot should not be called")

    device = FakePowerButtonDevice()
    actions = BlockingSystemActions()
    events = EventBus()
    manager = PowerButtonManager(
        PowerButtonConfig(enabled=True, debounce_ms=0, long_press_ms=500),
        StateStore(), events, device, actions,
    )
    queue = events.subscribe()
    await manager.start()
    await device.press()
    await asyncio.wait_for(queue.get(), 1)
    await asyncio.wait_for(actions.started.wait(), 1)
    await device.release()

    while (await manager.snapshot())["actual"]["pressed"]:
        await asyncio.sleep(0)
    actions.complete.set()
    while not actions.finished:
        await asyncio.sleep(0)

    await manager.stop()


@pytest.mark.asyncio
async def test_power_button_short_tap_within_debounce_does_not_trigger_long_press():
    device = FakePowerButtonDevice()
    actions = FakeSystemActions()
    manager = PowerButtonManager(
        PowerButtonConfig(enabled=True, debounce_ms=100, long_press_ms=500),
        StateStore(), EventBus(), device, actions,
    )
    await manager.start()
    await device.press()
    await device.release()

    await asyncio.sleep(0.6)

    assert actions.shutdown_calls == 0
    assert (await manager.snapshot())["actual"]["pressed"] is False
    await manager.stop()


@pytest.mark.asyncio
async def test_disabled_power_button_does_not_open_device():
    device = FakePowerButtonDevice()
    manager = PowerButtonManager(PowerButtonConfig(), StateStore(), EventBus(), device)

    await manager.start()

    assert not device.opened
    assert (await manager.health())["status"] == "ready"
    await manager.stop()


@pytest.mark.asyncio
async def test_power_button_reader_failure_degrades_manager():
    class FailingDevice(FakePowerButtonDevice):
        async def open(self):
            raise RuntimeError("input unavailable")

    manager = PowerButtonManager(
        PowerButtonConfig(enabled=True), StateStore(), EventBus(), FailingDevice()
    )

    await manager.start()

    health = await manager.health()
    assert health["status"] == "degraded"
    assert health["error"] == "input unavailable"
    await manager.stop()


@pytest.mark.asyncio
async def test_power_button_event_stream_ending_degrades_manager():
    device = FakePowerButtonDevice()
    manager = PowerButtonManager(
        PowerButtonConfig(enabled=True), StateStore(), EventBus(), device,
    )
    await manager.start()
    await device.events_queue.put(None)

    async def wait_for_degraded():
        while (await manager.health())["status"] != "degraded":
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_for_degraded(), 1)
    health = await manager.health()
    assert health["error"] == "Power button event stream ended unexpectedly"
    assert (await manager.snapshot())["actual"]["connected"] is False
    await manager.stop()


def test_power_button_defaults_are_safe():
    config = AppConfig()

    assert config.power_button.enabled is False
    assert config.power_button.long_press_action == "shutdown"
