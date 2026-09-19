import asyncio
import logging
from typing import Any

from ..config import AppConfig
from ..managers.base import Manager
from ..managers.fans import FanManager
from ..managers.system import SystemManager
from ..managers.rgb import RgbManager
from ..managers.power_button import PowerButtonManager
from ..managers.lighting import LightingManager
from ..managers.oled import OledManager
from .commands import CommandBus
from .events import EventBus
from .state import StateStore
from .persistence import PersistenceStore

logger = logging.getLogger(__name__)


class PersistenceError(RuntimeError):
    """Raised when a successful update cannot be durably persisted."""


class Supervisor:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.state = StateStore()
        self.persistence = PersistenceStore(config.persistence_path)
        self.commands = CommandBus()
        self.events = EventBus()
        self._hardware_stop_lock = asyncio.Lock()
        lighting = LightingManager(config.lighting, self.state, self.commands)
        schedule_allowed = lighting.allowed_now()
        rgb = RgbManager(config.rgb, self.state, schedule_allowed=schedule_allowed)
        fans = FanManager(config.fans, self.state, schedule_allowed=schedule_allowed)
        system = SystemManager(config.system, self.state)
        oled = OledManager(config.oled, self.state)
        self.managers: dict[str, Manager] = {
            "system": system,
            "rgb": rgb,
            "fans": fans,
            "lighting": lighting,
            "oled": oled,
            "power_button": PowerButtonManager(config.power_button, self.state, self.events,
                                                prepare_shutdown=self.prepare_power_shutdown),
        }
        self.commands.register("rgb.update", self.managers["rgb"].execute, resource_id="rgb-device")  # type: ignore[attr-defined]
        self.commands.register("fans.update", self.managers["fans"].execute, resource_id="fan-gpio")  # type: ignore[attr-defined]
        self.commands.register("rgb.schedule", lambda command: rgb.set_schedule_allowed(command.payload["allowed"]),
                               resource_id="rgb-device")
        self.commands.register("fans.schedule", lambda command: fans.set_schedule_allowed(command.payload["allowed"]),
                               resource_id="fan-gpio")
        self.commands.register("lighting.update", self.managers["lighting"].execute, resource_id="lighting")  # type: ignore[attr-defined]
        self.commands.register("oled.update", self.managers["oled"].execute, resource_id="oled-device")  # type: ignore[attr-defined]
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        started: list[Manager] = []
        try:
            for manager in self.managers.values():
                await manager.start()
                started.append(manager)
            await self._restore_persisted_state()
        except BaseException:
            for manager in reversed(started):
                try:
                    await manager.stop()
                except Exception as error:
                    logger.warning("Failed to roll back %s manager: %s", manager.name, error)
            raise

    async def stop(self) -> None:
        await self.commands.shutdown()
        await self._stop_hardware()
        self.events.close()

    async def prepare_power_shutdown(self, action: str) -> None:
        """Clear hardware before systemd powers off or reboots the host."""
        logger.info("Preparing hardware for system %s", action)
        await self.commands.shutdown()
        await self._stop_hardware(exclude={"power_button"})

    async def _stop_hardware(self, exclude: set[str] | None = None) -> None:
        excluded = exclude or set()
        async with self._hardware_stop_lock:
            for name, manager in reversed(list(self.managers.items())):
                if name in excluded:
                    continue
                try:
                    await manager.stop()
                except Exception as error:
                    logger.warning("Failed to stop %s manager: %s", manager.name, error)

    async def _restore_persisted_state(self) -> None:
        try:
            persisted = await self.persistence.load()
        except ValueError as error:
            logger.warning("Ignoring invalid persisted state: %s", error)
            return
        for manager_name, desired in persisted.items():
            command_type = f"{manager_name}.update"
            if command_type not in self.commands._handlers:
                logger.warning("Ignoring persisted state for unsupported manager %s", manager_name)
                continue
            payload = self._restore_payload(manager_name, desired)
            payload.pop("schedule_allowed", None)
            payload.pop("schedule_suppressed", None)
            try:
                await self.commands.dispatch(command_type, payload)
            except Exception as error:
                logger.warning("Could not restore persisted %s state: %s", manager_name, error)

    @staticmethod
    def _restore_payload(manager: str, desired: dict[str, Any]) -> dict[str, Any]:
        payload = dict(desired)
        if manager == "rgb" and "pattern" in payload:
            payload.pop("color", None)
        elif manager == "fans":
            external = payload.pop("external", {})
            led = payload.pop("led", {})
            if isinstance(external, dict):
                payload.update(external)
            if isinstance(led, dict) and "mode" in led:
                payload["led_mode"] = led["mode"]
        return payload

    async def persist_result(self, manager: str, result: dict[str, Any]) -> None:
        desired = result.get("desired")
        if not isinstance(desired, dict):
            return
        desired = dict(desired)
        desired.pop("schedule_allowed", None)
        desired.pop("schedule_suppressed", None)
        try:
            await self.persistence.update(manager, desired)
        except (OSError, ValueError) as error:
            logger.error("Could not persist %s state to %s: %s", manager, self.persistence.path, error)
            raise PersistenceError(f"Could not persist {manager} state to {self.persistence.path}: {error}") from error

    async def clear_persisted_state(self) -> None:
        await self.persistence.clear()

    async def health(self) -> dict:
        return {name: await manager.health() for name, manager in self.managers.items()}

    async def run(self) -> None:
        await self.start()
        await self._stop_event.wait()

    def request_stop(self) -> None:
        self._stop_event.set()
