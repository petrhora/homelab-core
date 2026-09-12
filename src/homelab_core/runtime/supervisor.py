import asyncio
import logging

from ..config import AppConfig
from ..managers.base import Manager
from ..managers.system import SystemManager
from .state import StateStore

logger = logging.getLogger(__name__)


class Supervisor:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.state = StateStore()
        self.managers: dict[str, Manager] = {"system": SystemManager(config.system, self.state)}
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        for manager in self.managers.values():
            await manager.start()

    async def stop(self) -> None:
        for manager in reversed(list(self.managers.values())):
            await manager.stop()

    async def health(self) -> dict:
        return {name: await manager.health() for name, manager in self.managers.items()}

    async def run(self) -> None:
        await self.start()
        await self._stop_event.wait()

    def request_stop(self) -> None:
        self._stop_event.set()
