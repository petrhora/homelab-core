from dataclasses import dataclass
from typing import Any, Awaitable, Callable
import asyncio
import uuid


@dataclass(frozen=True)
class Command:
    type: str
    resource_id: str
    payload: dict[str, Any]
    request_id: str
    timeout: float


class CommandBus:
    def __init__(self) -> None:
        self._handlers: dict[str, tuple[Callable[[Command], Awaitable[Any]], str]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def register(self, command_type: str, handler: Callable[[Command], Awaitable[Any]],
                 resource_id: str | None = None) -> None:
        self._handlers[command_type] = (handler, resource_id or command_type)

    async def dispatch(self, command_type: str, payload: dict[str, Any], timeout: float = 10.0) -> Any:
        if command_type not in self._handlers:
            raise ValueError(f"Unknown command: {command_type}")
        handler, resource_id = self._handlers[command_type]
        command = Command(command_type, resource_id, payload, str(uuid.uuid4()), timeout)
        lock = self._locks.setdefault(resource_id, asyncio.Lock())

        async def run_locked() -> Any:
            async with lock:
                return await handler(command)

        return await asyncio.wait_for(run_locked(), timeout)
