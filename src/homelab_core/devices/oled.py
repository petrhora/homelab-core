from abc import ABC, abstractmethod


class OledDevice(ABC):
    width = 128
    height = 64

    @abstractmethod
    async def display(self, frame: bytes) -> None: ...

    @abstractmethod
    async def clear(self) -> None: ...

    @abstractmethod
    async def set_enabled(self, enabled: bool) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...


class FakeOledDevice(OledDevice):
    def __init__(self) -> None:
        self.frames: list[bytes] = []
        self.enabled = False
        self.closed = False

    async def display(self, frame: bytes) -> None:
        self.frames.append(frame)
        self.enabled = True

    async def clear(self) -> None:
        self.frames.append(bytes(self.width * self.height // 8))

    async def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled

    async def close(self) -> None:
        self.closed = True
