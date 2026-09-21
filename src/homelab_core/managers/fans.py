import asyncio
import contextlib
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..config import FansConfig
from ..devices.fan import GpioFanDevice
from ..devices.interfaces import FanDevice
from ..runtime.commands import Command
from ..runtime.state import StateStore, utc_now
from .base import Manager, ManagerStatus


TemperatureReader = Callable[[], Awaitable[float | None]]
TEMPERATURE_UNAVAILABLE_ERROR = "CPU temperature is unavailable; external fan forced on"


class FanManager(Manager):
    name = "fans"

    def __init__(self, config: FansConfig, state: StateStore, device: FanDevice | None = None,
                 temperature_reader: TemperatureReader | None = None, schedule_allowed: bool = True) -> None:
        self.config = config
        self.state = state
        self.device = device
        if self.device is None and config.enabled:
            self.device = GpioFanDevice(config.external.pin, config.led.pin,
                                        fan_active_high=config.external.active_high,
                                        led_active_high=config.led.active_high)
        self._temperature_reader = temperature_reader or self._read_temperature
        self._mode = config.external.mode
        self._on_temperature_c = config.external.on_temperature_c
        self._off_temperature_c = config.external.off_temperature_c
        self._led_mode = config.led.mode
        self._schedule_allowed = schedule_allowed
        self._fan_enabled = False
        self._led_enabled = False
        self._temperature_c: float | None = None
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._status = ManagerStatus.STARTING
        self._error: str | None = None
        self._error_count = 0
        self._last_operation: str | None = None
        self._last_operation_at: str | None = None
        self._last_success_at: str | None = None
        self._last_error_at: str | None = None

    async def start(self) -> None:
        async with self._lock:
            try:
                temperature_available = True
                if self.config.enabled:
                    self._task = asyncio.create_task(self._poll(), name="fans-poll")
                    temperature_available = await self._refresh("start", force=True)
                if temperature_available:
                    self._record_success("start")
                    self._status = ManagerStatus.READY
                else:
                    self._record_error("start", RuntimeError(TEMPERATURE_UNAVAILABLE_ERROR))
                    self._status = ManagerStatus.DEGRADED
            except Exception as error:
                self._record_error("start", error)
                self._status = ManagerStatus.DEGRADED
            await self._publish()

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        async with self._lock:
            try:
                if self.device is not None:
                    await self.device.set_fan(False)
                    await self.device.set_led(False)
                    self._fan_enabled = False
                    self._led_enabled = False
                    await self.device.close()
                self._record_success("stop")
            except Exception as error:
                self._record_error("stop", error)
            finally:
                self._status = ManagerStatus.STOPPED
                await self._publish()

    async def health(self) -> dict:
        async with self._lock:
            return {"status": self._status.value, "error": self._error, "error_count": self._error_count,
                    "last_operation": self._last_operation, "last_operation_at": self._last_operation_at,
                    "last_success_at": self._last_success_at, "last_error_at": self._last_error_at}

    async def snapshot(self) -> dict:
        async with self._lock:
            return await self._state()

    async def execute(self, command: Command) -> dict:
        if not self.config.enabled:
            raise RuntimeError("fan manager is disabled")
        async with self._lock:
            payload = command.payload
            mode = payload.get("mode", self._mode)
            led_mode = payload.get("led_mode", self._led_mode)
            on_temperature_c = payload.get("on_temperature_c", self._on_temperature_c)
            off_temperature_c = payload.get("off_temperature_c", self._off_temperature_c)
            if mode not in ("off", "on", "auto"):
                raise ValueError("mode must be off, on, or auto")
            if led_mode not in ("off", "on", "follow"):
                raise ValueError("led_mode must be off, on, or follow")
            if isinstance(on_temperature_c, bool) or not isinstance(on_temperature_c, (int, float)) or not 0 <= on_temperature_c <= 120:
                raise ValueError("on_temperature_c must be between 0 and 120")
            if isinstance(off_temperature_c, bool) or not isinstance(off_temperature_c, (int, float)) or not 0 <= off_temperature_c <= 120:
                raise ValueError("off_temperature_c must be between 0 and 120")
            if off_temperature_c > on_temperature_c:
                raise ValueError("off_temperature_c must not exceed on_temperature_c")
            previous = self._mode, self._led_mode, self._on_temperature_c, self._off_temperature_c
            self._mode = mode
            self._led_mode = led_mode
            self._on_temperature_c = float(on_temperature_c)
            self._off_temperature_c = float(off_temperature_c)
            try:
                temperature_available = await self._refresh(command.type)
                if temperature_available:
                    self._record_success(command.type)
                    self._status = ManagerStatus.READY
                else:
                    self._record_error(command.type, RuntimeError(TEMPERATURE_UNAVAILABLE_ERROR))
                    self._status = ManagerStatus.DEGRADED
            except Exception as error:
                self._mode, self._led_mode, self._on_temperature_c, self._off_temperature_c = previous
                self._record_error(command.type, error)
                self._status = ManagerStatus.DEGRADED
                await self._publish()
                raise
            await self._publish()
            return await self._state()

    async def set_schedule_allowed(self, allowed: bool) -> dict:
        async with self._lock:
            if allowed == self._schedule_allowed:
                return await self._state()
            led_enabled = self._resolve_led_state(self._fan_enabled) and allowed
            try:
                if self.device is not None and led_enabled != self._led_enabled:
                    await self.device.set_led(led_enabled)
                self._schedule_allowed = allowed
                self._led_enabled = led_enabled
                self._record_success("schedule")
                self._status = ManagerStatus.READY
            except Exception as error:
                self._record_error("schedule", error)
                self._status = ManagerStatus.DEGRADED
                raise
            finally:
                await self._publish()
            return await self._state()

    async def _poll(self) -> None:
        while True:
            await asyncio.sleep(self.config.poll_interval_seconds)
            async with self._lock:
                try:
                    temperature_available = await self._refresh("poll")
                    if temperature_available and self._status == ManagerStatus.DEGRADED:
                        self._record_success("poll")
                        self._status = ManagerStatus.READY
                    elif not temperature_available:
                        self._record_error("poll", RuntimeError(TEMPERATURE_UNAVAILABLE_ERROR))
                        self._status = ManagerStatus.DEGRADED
                except Exception as error:
                    self._record_error("poll", error)
                    self._status = ManagerStatus.DEGRADED
                await self._publish()

    async def _refresh(self, operation: str, *, force: bool = False) -> bool:
        temperature_available = True
        if self._mode == "auto":
            try:
                self._temperature_c = await self._temperature_reader()
            except Exception:
                self._temperature_c = None
            temperature_available = self._temperature_c is not None
        fan_enabled = True if not temperature_available else self._resolve_fan_state()
        led_enabled = self._resolve_led_state(fan_enabled) and self._schedule_allowed
        if self.device is not None:
            previous_fan_enabled = self._fan_enabled
            previous_led_enabled = self._led_enabled
            try:
                if force or fan_enabled != previous_fan_enabled:
                    await self.device.set_fan(fan_enabled)
                if force or led_enabled != previous_led_enabled:
                    await self.device.set_led(led_enabled)
            except Exception:
                # Restore outputs because writes are not atomic across the two GPIO pins.
                with contextlib.suppress(Exception):
                    await self.device.set_fan(previous_fan_enabled)
                with contextlib.suppress(Exception):
                    await self.device.set_led(previous_led_enabled)
                raise
        self._fan_enabled = fan_enabled
        self._led_enabled = led_enabled
        return temperature_available

    def _resolve_fan_state(self) -> bool:
        if self._mode == "off":
            return False
        if self._mode == "on":
            return True
        temperature = self._temperature_c
        if temperature is None:
            return True
        if self._fan_enabled:
            return temperature > self._off_temperature_c
        return temperature >= self._on_temperature_c

    def _resolve_led_state(self, fan_enabled: bool) -> bool:
        if self._led_mode == "on":
            return True
        if self._led_mode == "follow":
            return fan_enabled
        return False

    async def _read_temperature(self) -> float | None:
        def read() -> float | None:
            try:
                value = Path(self.config.temperature_path).read_text().strip()
                return int(value) / 1000
            except (OSError, ValueError):
                return None
        return await asyncio.to_thread(read)

    def _record_success(self, operation: str) -> None:
        timestamp = utc_now()
        self._last_operation = operation
        self._last_operation_at = timestamp
        self._last_success_at = timestamp
        self._error = None

    def _record_error(self, operation: str, error: Exception) -> None:
        self._last_operation = operation
        self._last_operation_at = utc_now()
        self._last_error_at = self._last_operation_at
        self._error_count += 1
        self._error = str(error)

    async def _publish(self) -> None:
        await self.state.update(self.name, await self._state())

    async def _state(self) -> dict[str, Any]:
        return {"desired": {"enabled": self.config.enabled, "external": {"mode": self._mode,
                "on_temperature_c": self._on_temperature_c, "off_temperature_c": self._off_temperature_c},
                "led": {"mode": self._led_mode, "schedule_allowed": self._schedule_allowed,
                "schedule_suppressed": self._resolve_led_state(self._fan_enabled) and not self._schedule_allowed}}, "actual": {"temperature_celsius": self._temperature_c,
                "external": {"output_enabled": self._fan_enabled}, "led": {"output_enabled": self._led_enabled}},
                "operation": {"last": self._last_operation, "at": self._last_operation_at,
                "last_success_at": self._last_success_at, "last_error_at": self._last_error_at,
                "error": self._error, "error_count": self._error_count}, "collected_at": utc_now()}
