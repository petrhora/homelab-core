import logging
from typing import Any, Sequence

from ..config import RgbConfig
from ..devices.interfaces import RgbDevice, RgbPixel
from ..devices.ws2812 import Ws2812SpiDevice
from ..runtime.commands import Command
from ..runtime.state import StateStore, utc_now
from .base import Manager, ManagerStatus


logger = logging.getLogger(__name__)


class RgbManager(Manager):
    name = "rgb"

    def __init__(self, config: RgbConfig, state: StateStore, device: RgbDevice | None = None) -> None:
        self.config = config
        self.state = state
        self.device = device or Ws2812SpiDevice(config.spi_path, config.spi_speed_hz, config.led_count)
        self._status = ManagerStatus.STARTING
        self._error: str | None = None
        self._enabled = config.enabled
        self._brightness = config.brightness
        self._color = self.parse_color(config.color)
        self._led_count = config.led_count
        self._actual: dict[str, Any] | None = None
        self._last_operation: str | None = None
        self._last_operation_at: str | None = None
        self._last_success_at: str | None = None
        self._last_error_at: str | None = None
        self._error_count = 0

    async def start(self) -> None:
        try:
            if self._enabled:
                self._actual = await self._apply()
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
        if "enabled" in payload:
            enabled = bool(payload["enabled"])
        if "color" in payload:
            color = self.parse_color(payload["color"])
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

        self._enabled = enabled
        self._color = color
        self._brightness = brightness
        self._led_count = led_count
        try:
            if self._enabled:
                self._actual = await self._apply()
            else:
                await self.device.clear()
                self._actual = self._off_state()
            self._record_success(command.type)
            self._status = ManagerStatus.READY
        except Exception as error:
            self._record_error(command.type, error)
            self._status = ManagerStatus.DEGRADED
            await self._publish()
            raise
        await self._publish()
        return await self._state()

    async def _apply(self) -> dict[str, Any]:
        scaled: RgbPixel = (
            round(self._color[0] * self._brightness),
            round(self._color[1] * self._brightness),
            round(self._color[2] * self._brightness),
        )
        await self.device.set_pixels([scaled] * self._led_count)
        return {"enabled": True, "color": "#%02X%02X%02X" % scaled, "led_count": self._led_count}

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
        return {"desired": {"enabled": self._enabled, "color": "#%02X%02X%02X" % self._color,
                             "brightness": self._brightness, "led_count": self._led_count},
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
