from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from ..config import PowerButtonConfig
from ..devices.power_button import ButtonInputEvent, EvdevPowerButtonDevice, PowerButtonDevice
from ..runtime.events import Event, EventBus
from ..runtime.state import StateStore, utc_now
from ..runtime.system_actions import SystemActions, SystemctlActions
from .base import Manager, ManagerStatus


class PowerButtonManager(Manager):
    name = "power_button"

    def __init__(self, config: PowerButtonConfig, state: StateStore, events: EventBus,
                 device: PowerButtonDevice | None = None,
                 system_actions: SystemActions | None = None) -> None:
        self.config = config
        self.state = state
        self.events = events
        self.device = device or EvdevPowerButtonDevice(config.device_name, config.device_path)
        self.system_actions = system_actions or SystemctlActions()
        self._status = ManagerStatus.STARTING
        self._error: str | None = None
        self._error_count = 0
        self._reader_task: asyncio.Task[None] | None = None
        self._long_press_task: asyncio.Task[None] | None = None
        self._click_task: asyncio.Task[None] | None = None
        self._pressed = False
        self._press_started_at: float | None = None
        self._long_press_triggered = False
        self._click_count = 0
        self._last_transition_at = 0.0
        self._last_event: dict[str, Any] | None = None

    async def start(self) -> None:
        if not self.config.enabled:
            self._status = ManagerStatus.READY
            await self._publish()
            return
        try:
            await self.device.open()
            self._reader_task = asyncio.create_task(self._read_events(), name="power-button-reader")
            self._status = ManagerStatus.READY
        except Exception as error:
            self._record_error(error)
            self._status = ManagerStatus.DEGRADED
        await self._publish()

    async def stop(self) -> None:
        for task in (self._reader_task, self._long_press_task, self._click_task):
            if task is not None:
                task.cancel()
        for task in (self._reader_task, self._long_press_task, self._click_task):
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._reader_task = None
        self._long_press_task = None
        self._click_task = None
        with contextlib.suppress(Exception):
            await self.device.close()
        self._status = ManagerStatus.STOPPED
        await self._publish()

    async def health(self) -> dict[str, Any]:
        return {"status": self._status.value, "error": self._error, "error_count": self._error_count}

    async def snapshot(self) -> dict[str, Any]:
        return {
            "desired": {"enabled": self.config.enabled, "device_name": self.config.device_name,
                        "device_path": self.config.device_path, "long_press_action": self.config.long_press_action},
            "actual": {"pressed": self._pressed, "connected": self._reader_task is not None and not self._reader_task.done()},
            "last_event": self._last_event,
            "operation": {"error": self._error, "error_count": self._error_count},
            "collected_at": utc_now(),
        }

    async def _read_events(self) -> None:
        try:
            async for event in self.device.events():
                await self._handle_input(event)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._record_error(error)
            self._status = ManagerStatus.DEGRADED
            await self._publish()
        else:
            self._record_error(RuntimeError("Power button event stream ended unexpectedly"))
            self._status = ManagerStatus.DEGRADED
            await self._publish()

    async def _handle_input(self, event: ButtonInputEvent) -> None:
        now = time.monotonic()
        if event.pressed == self._pressed:
            return
        # A release completes the current press even when it is shorter than
        # the debounce interval; otherwise a short tap can become a long press.
        if event.pressed and now - self._last_transition_at < self.config.debounce_ms / 1000:
            return
        self._last_transition_at = now
        self._pressed = event.pressed
        if event.pressed:
            self._press_started_at = now
            self._long_press_triggered = False
            if self._click_task is not None:
                self._click_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._click_task
                self._click_task = None
            self._long_press_task = asyncio.create_task(self._long_press(), name="power-button-long-press")
        else:
            if self._long_press_task is not None:
                self._long_press_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._long_press_task
                self._long_press_task = None
            if not self._long_press_triggered:
                self._click_count += 1
                if self._click_count == 2:
                    await self._emit("double_click")
                    self._click_count = 0
                    self._click_task = None
                else:
                    self._click_task = asyncio.create_task(self._finish_click(), name="power-button-click")
        await self._publish()

    async def _finish_click(self) -> None:
        try:
            await asyncio.sleep(self.config.double_click_window_ms / 1000)
            if self._click_count == 1:
                await self._emit("click")
            self._click_count = 0
        except asyncio.CancelledError:
            raise
        finally:
            self._click_task = None

    async def _long_press(self) -> None:
        try:
            await asyncio.sleep(self.config.long_press_ms / 1000)
            if not self._pressed:
                return
            self._long_press_triggered = True
            await self._emit("long_press")
            action = self.config.long_press_action
            if action == "shutdown":
                await self.system_actions.shutdown()
            elif action == "reboot":
                await self.system_actions.reboot()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._record_error(error)
            self._status = ManagerStatus.DEGRADED
            await self._publish()

    async def _emit(self, gesture: str) -> None:
        timestamp = utc_now()
        self._last_event = {"type": gesture, "at": timestamp}
        await self.events.publish(Event(f"power_button.{gesture}", {"at": timestamp}))
        await self._publish()

    def _record_error(self, error: Exception) -> None:
        self._error = str(error)
        self._error_count += 1

    async def _publish(self) -> None:
        await self.state.update(self.name, await self.snapshot())
