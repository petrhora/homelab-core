import asyncio
import contextlib
import logging
import time
from typing import Any, Awaitable, Callable

from ..config import OledConfig
from ..devices.oled import OledDevice
from ..devices.ssd1306 import Ssd1306I2cDevice
from ..oled_renderer import render_shutdown, render_status
from ..runtime.commands import Command
from ..runtime.state import StateStore, utc_now
from .base import Manager, ManagerStatus

logger = logging.getLogger(__name__)
SHUTDOWN_SCREEN_SECONDS = 1


class OledManager(Manager):
    name = "oled"

    def __init__(self, config: OledConfig, state: StateStore, device: OledDevice | None = None,
                 snapshot_reader: Callable[[], Awaitable[dict[str, Any]]] | None = None) -> None:
        self.config = config
        self.state = state
        self.device = device or Ssd1306I2cDevice(config.bus, config.address, config.rotation)
        self.snapshot_reader = snapshot_reader or state.snapshot
        self._status = ManagerStatus.STARTING
        self._error: str | None = None
        self._enabled = config.enabled
        self._display_enabled = False
        self._sleep_timeout = config.sleep_timeout_seconds
        self._last_wake = time.monotonic()
        self._last_render_at: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._operation_lock = asyncio.Lock()

    async def start(self) -> None:
        try:
            if self._enabled:
                async with self._operation_lock:
                    await self._render()
                self._task = asyncio.create_task(self._refresh_loop())
            self._status = ManagerStatus.READY
            self._error = None
        except Exception as error:
            self._status = ManagerStatus.DEGRADED
            self._error = str(error)
            logger.warning("OLED unavailable: %s", error)
        await self._publish()

    async def stop(self) -> None:
        await self._stop_refresh_task()
        try:
            async with self._operation_lock:
                if self._display_enabled:
                    await self.device.display(render_shutdown())
                    await asyncio.sleep(SHUTDOWN_SCREEN_SECONDS)
                    await self.device.clear()
                    await self.device.set_enabled(False)
                    self._display_enabled = False
        except Exception as error:
            self._error = str(error)
            logger.warning("Failed to clear OLED during shutdown: %s", error)
        finally:
            try:
                await self.device.close()
            except Exception as error:
                self._error = str(error)
                logger.warning("Failed to close OLED during shutdown: %s", error)
            self._status = ManagerStatus.STOPPED
            await self._publish()

    async def health(self) -> dict:
        return {"status": self._status.value, "error": self._error}

    async def snapshot(self) -> dict:
        return {"desired": {"enabled": self._enabled, "sleep_timeout_seconds": self._sleep_timeout,
                             "rotation": self.config.rotation},
                "actual": {"enabled": self._display_enabled, "last_render_at": self._last_render_at},
                "health": await self.health()}

    async def execute(self, command: Command) -> dict:
        payload = command.payload
        enabled = self._enabled if "enabled" not in payload else bool(payload["enabled"])
        sleep_timeout = self._sleep_timeout if "sleep_timeout_seconds" not in payload else payload["sleep_timeout_seconds"]
        if not isinstance(sleep_timeout, (int, float)) or isinstance(sleep_timeout, bool) or not 0 <= sleep_timeout <= 3600:
            raise ValueError("sleep_timeout_seconds must be between 0 and 3600")

        try:
            if not enabled:
                await self._stop_refresh_task()
            async with self._operation_lock:
                if enabled:
                    await self._render()
                elif self._display_enabled:
                    await self.device.clear()
                    await self.device.set_enabled(False)
                    self._display_enabled = False
                self._enabled = enabled
                self._sleep_timeout = float(sleep_timeout)
                self._last_wake = time.monotonic()
                self._status = ManagerStatus.READY
                self._error = None
                if enabled:
                    self._start_refresh_task()
        except Exception as error:
            self._status = ManagerStatus.DEGRADED
            self._error = str(error)
            await self._publish()
            raise
        await self._publish()
        return await self.snapshot()

    async def wake(self) -> None:
        if not self._enabled:
            return
        async with self._operation_lock:
            await self._render()
            self._last_wake = time.monotonic()
        await self._publish()

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.refresh_interval_seconds)
            try:
                async with self._operation_lock:
                    if not self._enabled:
                        return
                    if self._sleep_timeout and time.monotonic() - self._last_wake >= self._sleep_timeout:
                        if self._display_enabled:
                            await self.device.set_enabled(False)
                            self._display_enabled = False
                    else:
                        await self._render()
            except Exception as error:
                self._status = ManagerStatus.DEGRADED
                self._error = str(error)
                await self._publish()

    async def _render(self) -> None:
        state = await self.snapshot_reader()
        system = state.get("system", state)
        if not self._display_enabled:
            await self.device.set_enabled(True)
        await self.device.display(render_status(system, rotation=self.config.rotation))
        self._display_enabled = True
        self._last_render_at = utc_now()

    def _start_refresh_task(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._refresh_loop())

    async def _stop_refresh_task(self) -> None:
        if self._task is not None and self._task is not asyncio.current_task():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None

    async def _publish(self) -> None:
        await self.state.update(self.name, await self.snapshot())
