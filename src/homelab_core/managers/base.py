from abc import ABC, abstractmethod
from enum import StrEnum


class ManagerStatus(StrEnum):
    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    STOPPED = "stopped"
    FAILED = "failed"


class Manager(ABC):
    name: str

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def health(self) -> dict: ...

    @abstractmethod
    async def snapshot(self) -> dict: ...
