from datetime import datetime, timezone
from typing import Any
import asyncio
import copy


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateStore:
    def __init__(self) -> None:
        self._state: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    async def update(self, manager: str, value: dict[str, Any]) -> None:
        async with self._lock:
            self._state[manager] = copy.deepcopy(value)

    async def snapshot(self) -> dict[str, Any]:
        async with self._lock:
            return copy.deepcopy(self._state)
