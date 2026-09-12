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
