from dataclasses import dataclass
from typing import Any, Awaitable, Callable
import asyncio
import uuid


@dataclass(frozen=True)
class Command:
    type: str
    payload: dict[str, Any]
    request_id: str
    timeout: float


class CommandBus:
    def __init__(self) -> None:
        self._handlers: dict[str, Callable[[Command], Awaitable[Any]]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def register(self, command_type: str, handler: Callable[[Command], Awaitable[Any]]) -> None:
        self._handlers[command_type] = handler

    async def dispatch(self, command_type: str, payload: dict[str, Any], timeout: float = 10.0) -> Any:
        if command_type not in self._handlers:
            raise ValueError(f"Unknown command: {command_type}")
        command = Command(command_type, payload, str(uuid.uuid4()), timeout)
        lock = self._locks.setdefault(command_type, asyncio.Lock())
        async with lock:
            return await asyncio.wait_for(self._handlers[command_type](command), timeout)
