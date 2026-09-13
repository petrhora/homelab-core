import asyncio
import logging

from ..config import AppConfig
from ..managers.base import Manager
from ..managers.fans import FanManager
from ..managers.system import SystemManager
from ..managers.rgb import RgbManager
from .commands import CommandBus
from .state import StateStore

logger = logging.getLogger(__name__)


class Supervisor:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.state = StateStore()
        self.commands = CommandBus()
        self.managers: dict[str, Manager] = {
            "system": SystemManager(config.system, self.state),
            "rgb": RgbManager(config.rgb, self.state),
            "fans": FanManager(config.fans, self.state),
        }
        self.commands.register("rgb.update", self.managers["rgb"].execute, resource_id="rgb-device")  # type: ignore[attr-defined]
        self.commands.register("fans.update", self.managers["fans"].execute, resource_id="fan-gpio")  # type: ignore[attr-defined]
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        started: list[Manager] = []
        try:
            for manager in self.managers.values():
                await manager.start()
                started.append(manager)
        except BaseException:
            for manager in reversed(started):
                try:
                    await manager.stop()
                except Exception as error:
                    logger.warning("Failed to roll back %s manager: %s", manager.name, error)
            raise

    async def stop(self) -> None:
        await self.commands.shutdown()
        for manager in reversed(list(self.managers.values())):
            try:
                await manager.stop()
            except Exception as error:
                logger.warning("Failed to stop %s manager: %s", manager.name, error)

    async def health(self) -> dict:
        return {name: await manager.health() for name, manager in self.managers.items()}

    async def run(self) -> None:
        await self.start()
        await self._stop_event.wait()

    def request_stop(self) -> None:
        self._stop_event.set()
