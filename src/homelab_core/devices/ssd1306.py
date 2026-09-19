import asyncio
from typing import Any, Callable, TypeVar

from .oled import OledDevice


Result = TypeVar("Result")


class Ssd1306I2cDevice(OledDevice):
    """Minimal SSD1306 128x64 adapter based on the official PM Auto protocol."""

    def __init__(self, bus: int = 1, address: str = "auto", rotation: int = 0) -> None:
        self.bus = bus
        self.address = address
        self.rotation = rotation
        self._bus: Any | None = None
        self._address: int | None = None
        self._lifecycle_lock = asyncio.Lock()

    async def display(self, frame: bytes) -> None:
        expected = self.width * self.height // 8
        if len(frame) != expected:
            raise ValueError(f"OLED frame must contain {expected} bytes")
        async with self._lifecycle_lock:
            await self._ensure_open()

            def write() -> None:
                assert self._bus is not None and self._address is not None
                self._write_command(0x21)
                self._write_command(0)
                self._write_command(self.width - 1)
                self._write_command(0x22)
                self._write_command(0)
                self._write_command(self.height // 8 - 1)
                for offset in range(0, len(frame), 16):
                    self._bus.write_i2c_block_data(self._address, 0x40, list(frame[offset:offset + 16]))

            await self._run_io(write)

    async def clear(self) -> None:
        await self.display(bytes(self.width * self.height // 8))

    async def set_enabled(self, enabled: bool) -> None:
        async with self._lifecycle_lock:
            await self._ensure_open()
            await self._run_io(lambda: self._write_command(0xAF if enabled else 0xAE))

    async def close(self) -> None:
        async with self._lifecycle_lock:
            if self._bus is None:
                return
            bus = self._bus
            try:
                await self._run_io(bus.close)
            finally:
                self._bus = None
                self._address = None

    async def _ensure_open(self) -> None:
        if self._bus is not None:
            return
        try:
            from smbus2 import SMBus
        except ImportError as error:
            raise RuntimeError("Install the 'hardware' extra to use the OLED") from error

        def open_and_initialize() -> None:
            opened_bus = SMBus(self.bus)
            candidates = [int(self.address, 16)] if self.address != "auto" else [0x3C, 0x3D]
            try:
                for address in candidates:
                    try:
                        opened_bus.write_quick(address)
                    except OSError:
                        continue
                    self._initialize(opened_bus, address)
                    self._bus = opened_bus
                    self._address = address
                    return
                raise RuntimeError("SSD1306 OLED not detected at 0x3C or 0x3D")
            except BaseException:
                opened_bus.close()
                raise

        await self._run_io(open_and_initialize)

    async def _run_io(self, operation: Callable[[], Result]) -> Result:
        task = asyncio.create_task(asyncio.to_thread(operation))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # A thread cannot be cancelled safely mid-I2C transaction. Complete
            # it before releasing the lifecycle lock to prevent bus races.
            await asyncio.shield(task)
            raise

    @staticmethod
    def _initialize(bus: Any, address: int) -> None:
        for command in (0xAE, 0xD5, 0x80, 0xA8, 0x3F, 0xD3, 0x00, 0x40,
                        0x8D, 0x14, 0x20, 0x00, 0xA1, 0xC8, 0xDA, 0x12,
                        0x81, 0xCF, 0xD9, 0xF1, 0xDB, 0x40, 0xA4, 0xA6, 0xAF):
            bus.write_byte_data(address, 0x00, command)

    def _write_command(self, command: int) -> None:
        assert self._bus is not None and self._address is not None
        self._bus.write_byte_data(self._address, 0x00, command)
