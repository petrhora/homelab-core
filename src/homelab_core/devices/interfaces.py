from abc import ABC, abstractmethod
from typing import Sequence


RgbPixel = tuple[int, int, int]


class RgbDevice(ABC):
    @abstractmethod
    async def set_pixels(self, pixels: Sequence[RgbPixel]) -> None: ...

    @abstractmethod
    async def clear(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...


class FakeRgbDevice(RgbDevice):
    def __init__(self) -> None:
        self.pixels: list[RgbPixel] = []
        self.closed = False

    async def set_pixels(self, pixels: Sequence[RgbPixel]) -> None:
        self.pixels = list(pixels)

    async def clear(self) -> None:
        self.pixels = [(0, 0, 0)] * len(self.pixels)

    async def close(self) -> None:
        self.closed = True


class FanDevice(ABC):
    @abstractmethod
    async def set_fan(self, enabled: bool) -> None: ...

    @abstractmethod
    async def set_led(self, enabled: bool) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...


class FakeFanDevice(FanDevice):
    def __init__(self) -> None:
        self.fan_enabled = False
        self.led_enabled = False
        self.closed = False

    async def set_fan(self, enabled: bool) -> None:
        self.fan_enabled = enabled

    async def set_led(self, enabled: bool) -> None:
        self.led_enabled = enabled

    async def close(self) -> None:
        self.closed = True


# Keep hardware interfaces discoverable from the original interface module.
from .oled import FakeOledDevice, OledDevice  # noqa: E402,F401
