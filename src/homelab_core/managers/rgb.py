import asyncio
import contextlib
import logging
import time
from typing import Any

from ..config import RgbConfig
from ..devices.interfaces import RgbDevice, RgbPixel
from ..rgb_patterns import Pattern, adapt_retained_pattern, apply_brightness, normalize_pattern, pattern_to_payload
from ..devices.ws2812 import Ws2812SpiDevice
from ..runtime.commands import Command
from ..runtime.state import StateStore, utc_now
from .base import Manager, ManagerStatus


logger = logging.getLogger(__name__)

ANIMATION_FRAME_INTERVAL_SECONDS = 1 / 30
ANIMATION_RETRY_INITIAL_SECONDS = 0.1
ANIMATION_RETRY_MAX_SECONDS = 5.0


class RgbManager(Manager):
    name = "rgb"

    def __init__(self, config: RgbConfig, state: StateStore, device: RgbDevice | None = None,
                 schedule_allowed: bool = True) -> None:
        self.config = config
        self.state = state
        self.device = device or Ws2812SpiDevice(config.spi_path, config.spi_speed_hz, config.led_count)
        self._status = ManagerStatus.STARTING
        self._error: str | None = None
        self._enabled = config.enabled
        self._schedule_allowed = schedule_allowed
        self._brightness = config.brightness
        self._color = self.parse_color(config.color)
        self._led_count = config.led_count
        self._pattern: Pattern = normalize_pattern(None, self._led_count, self._color)
        self._animation_task: asyncio.Task[None] | None = None
        self._pattern_started_at = time.monotonic()
        self._actual: dict[str, Any] | None = None
        self._last_operation: str | None = None
        self._last_operation_at: str | None = None
        self._last_success_at: str | None = None
        self._last_error_at: str | None = None
        self._error_count = 0

    async def start(self) -> None:
        try:
            if self._enabled and self._schedule_allowed:
                self._actual, _ = await self._apply(self._pattern, self._led_count, self._brightness)
                self._start_animation()
            else:
                await self.device.clear()
                self._actual = self._off_state()
            self._record_success("start")
            self._status = ManagerStatus.READY
        except Exception as error:
            self._record_error("start", error)
            self._status = ManagerStatus.DEGRADED
        await self._publish()

    async def stop(self) -> None:
        await self._stop_animation()
        try:
            try:
                await self.device.clear()
            except Exception as error:
                self._record_error("stop", error)
                logger.warning("Failed to clear RGB device during shutdown: %s", error)
            else:
                self._actual = self._off_state()
            try:
                await self.device.close()
            except Exception as error:
                self._record_error("stop", error)
                logger.warning("Failed to close RGB device during shutdown: %s", error)
            else:
                if self._last_operation != "stop":
                    self._record_success("stop")
        finally:
            self._status = ManagerStatus.STOPPED
            await self._publish()

    async def health(self) -> dict:
        return {"status": self._status.value, "error": self._error, "error_count": self._error_count,
                "last_operation": self._last_operation, "last_operation_at": self._last_operation_at,
                "last_success_at": self._last_success_at, "last_error_at": self._last_error_at}

    async def snapshot(self) -> dict:
        return await self._state()

    async def execute(self, command: Command) -> dict:
        payload = command.payload
        enabled = self._enabled
        color = self._color
        brightness = self._brightness
        led_count = self._led_count
        pattern_payload: dict[str, Any] | None = pattern_to_payload(self._pattern)
        if "enabled" in payload:
            enabled = bool(payload["enabled"])
        if "color" in payload:
            color = self.parse_color(payload["color"])
            pattern_payload = {"type": "solid", "color": payload["color"]}
        if "brightness" in payload:
            value = payload["brightness"]
            if not isinstance(value, (int, float)) or not 0 <= value <= 1:
                raise ValueError("brightness must be between 0 and 1")
            brightness = float(value)
        if "led_count" in payload:
            count = payload["led_count"]
            if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 1024:
                raise ValueError("led_count must be between 1 and 1024")
            led_count = count
        if payload.get("clear"):
            color = (0, 0, 0)
            pattern_payload = {"type": "solid", "color": "#000000"}

        if "pattern" in payload:
            if "color" in payload or payload.get("clear"):
                raise ValueError("pattern cannot be combined with color or clear")
            pattern_payload = payload["pattern"]
        elif led_count != self._led_count:
            pattern_payload = adapt_retained_pattern(pattern_payload, self._led_count, led_count)
        candidate_pattern = normalize_pattern(pattern_payload, led_count, color)

        previous_enabled = self._enabled
        committed = False
        try:
            await self._stop_animation()
            if enabled and self._schedule_allowed:
                actual, interrupted = await self._apply(candidate_pattern, led_count, brightness,
                                                        complete_on_cancel=True)
            else:
                interrupted = await self._run_device_operation(self.device.clear(), complete_on_cancel=True)
                actual = {"enabled": False, "color": "#000000", "led_count": led_count,
                          "pattern": pattern_to_payload(candidate_pattern)}
            self._enabled = enabled
            self._color = color
            self._brightness = brightness
            self._led_count = led_count
            self._pattern = candidate_pattern
            self._pattern_started_at = time.monotonic()
            self._actual = actual
            committed = True
            if self._enabled and self._schedule_allowed:
                self._start_animation()
            self._record_success(command.type)
            self._status = ManagerStatus.READY
            await self._publish()
            if interrupted:
                raise asyncio.CancelledError
        except Exception as error:
            if not committed and previous_enabled:
                self._start_animation()
            self._record_error(command.type, error)
            self._status = ManagerStatus.DEGRADED
            await self._publish()
            raise
        except asyncio.CancelledError:
            if not committed and previous_enabled:
                self._start_animation()
            await self._publish()
            raise
        return await self._state()

    async def set_schedule_allowed(self, allowed: bool) -> dict:
        if allowed == self._schedule_allowed:
            return await self._state()
        previous_allowed = self._schedule_allowed
        try:
            await self._stop_animation()
            if self._enabled and allowed:
                self._actual, _ = await self._apply(self._pattern, self._led_count, self._brightness)
                self._start_animation()
            else:
                await self.device.clear()
                self._actual = self._off_state()
            self._schedule_allowed = allowed
            self._record_success("schedule")
            self._status = ManagerStatus.READY
        except Exception as error:
            if previous_allowed and self._enabled:
                self._start_animation()
            self._record_error("schedule", error)
            self._status = ManagerStatus.DEGRADED
            raise
        finally:
            await self._publish()
        return await self._state()

    async def _apply(self, pattern: Pattern, led_count: int, brightness: float,
                     *, complete_on_cancel: bool = False) -> tuple[dict[str, Any], bool]:
        frame = apply_brightness(pattern.render(led_count, 0), brightness)
        interrupted = await self._run_device_operation(self.device.set_pixels(frame), complete_on_cancel=complete_on_cancel)
        return ({"enabled": True, "color": "#%02X%02X%02X" % frame[0], "led_count": led_count,
                 "pattern": pattern_to_payload(pattern)}, interrupted)

    async def _run_device_operation(self, operation: Any, *, complete_on_cancel: bool = False) -> bool:
        """Finish hardware I/O before propagating cancellation to the caller."""
        task = asyncio.create_task(operation)
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # A timed-out command must not leave the physical strip halfway
            # through a frame while the manager moves on to another command.
            await task
            if complete_on_cancel:
                return True
            raise
        return False

    def _start_animation(self) -> None:
        if self._pattern.type != "solid":
            self._animation_task = asyncio.create_task(self._animate())

    async def _stop_animation(self) -> None:
        if self._animation_task is None:
            return
        task = self._animation_task
        self._animation_task = None
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _animate(self) -> None:
        retry_delay = ANIMATION_RETRY_INITIAL_SECONDS
        try:
            while True:
                elapsed = time.monotonic() - self._pattern_started_at
                frame = apply_brightness(self._pattern.render(self._led_count, elapsed), self._brightness)
                try:
                    await self._run_device_operation(self.device.set_pixels(frame))
                except Exception as error:
                    self._record_error("animation", error)
                    self._status = ManagerStatus.DEGRADED
                    await self._publish()
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, ANIMATION_RETRY_MAX_SECONDS)
                    continue

                if self._status == ManagerStatus.DEGRADED:
                    self._record_success("animation")
                    self._status = ManagerStatus.READY
                    await self._publish()
                retry_delay = ANIMATION_RETRY_INITIAL_SECONDS
                await asyncio.sleep(ANIMATION_FRAME_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise

    def _off_state(self) -> dict[str, Any]:
        return {"enabled": False, "color": "#000000", "led_count": self._led_count}

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
        return {"desired": {"enabled": self._enabled, "schedule_allowed": self._schedule_allowed,
                              "schedule_suppressed": self._enabled and not self._schedule_allowed,
                              "color": "#%02X%02X%02X" % self._color,
                             "brightness": self._brightness, "led_count": self._led_count,
                             "pattern": pattern_to_payload(self._pattern)},
                "actual": self._actual, "operation": {"last": self._last_operation,
                "at": self._last_operation_at, "last_success_at": self._last_success_at,
                "last_error_at": self._last_error_at, "error": self._error,
                "error_count": self._error_count}, "collected_at": utc_now()}

    @staticmethod
    def parse_color(value: str) -> RgbPixel:
        if not isinstance(value, str) or len(value) != 7 or not value.startswith("#"):
            raise ValueError("color must be in #RRGGBB format")
        try:
            return tuple(int(value[index:index + 2], 16) for index in (1, 3, 5))  # type: ignore[return-value]
        except ValueError:
            raise ValueError("color must be in #RRGGBB format") from None
