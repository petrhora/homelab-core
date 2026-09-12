import asyncio
import threading
from typing import Sequence

from .interfaces import RgbDevice, RgbPixel


class Ws2812SpiDevice(RgbDevice):
    """WS2812 adapter using the Pi's SPI peripheral and GRB pixel ordering."""

    def __init__(self, path: str, speed_hz: int, pixel_count: int = 0) -> None:
        self.path = path
        self.speed_hz = speed_hz
        self._spi = None
        # Keep the largest frame sent so a later clear can blank LEDs omitted
        # by a reduced led_count setting.
        self._pixel_count = pixel_count
        self._io_lock = threading.Lock()
        self._reset_bytes = [0] * max(1, round(speed_hz * 80e-6 / 8))

    async def _open(self) -> None:
        if self._spi is not None:
            return
        try:
            import spidev
        except ImportError as error:
            raise RuntimeError("Install the 'hardware' extra to use WS2812 SPI") from error
        spi = await asyncio.to_thread(spidev.SpiDev)
        bus, device = self._parse_path(self.path)
        await asyncio.to_thread(spi.open, bus, device)
        spi.max_speed_hz = self.speed_hz
        spi.mode = 0
        self._spi = spi

    async def set_pixels(self, pixels: Sequence[RgbPixel]) -> None:
        await self._open()
        payload = self._encode_pixels(pixels)
        self._pixel_count = max(self._pixel_count, len(pixels))
        spi = self._spi

        def transfer() -> None:
            # Keep the lock in the worker thread. If the coroutine is
            # cancelled by a command timeout, the transfer still completes
            # before another operation can touch or close the SPI device.
            with self._io_lock:
                spi.xfer2([*self._reset_bytes, *payload, *self._reset_bytes])

        await asyncio.to_thread(transfer)
        await asyncio.sleep(0.00008)

    async def clear(self) -> None:
        await self.set_pixels([(0, 0, 0)] * self._pixel_count)

    async def close(self) -> None:
        if self._spi is not None:
            spi = self._spi

            def close_spi() -> None:
                with self._io_lock:
                    spi.close()

            await asyncio.to_thread(close_spi)
            if self._spi is spi:
                self._spi = None

    @staticmethod
    def _parse_path(path: str) -> tuple[int, int]:
        try:
            prefix, values = path.rsplit("/dev/spidev", 1)
            if prefix or "." not in values:
                raise ValueError
            bus, device = values.split(".", 1)
            return int(bus), int(device)
        except (ValueError, AttributeError):
            raise ValueError(f"Invalid SPI device path: {path}") from None

    @staticmethod
    def _encode_pixels(pixels: Sequence[RgbPixel]) -> list[int]:
        encoded: list[int] = []
        for red, green, blue in pixels:
            for component in (green, red, blue):
                value = 0
                for bit in range(7, -1, -1):
                    value = (value << 3) | (0b110 if component & (1 << bit) else 0b100)
                encoded.extend(value.to_bytes(3, "big"))
        return encoded
