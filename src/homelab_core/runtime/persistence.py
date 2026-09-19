import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Any


class PersistenceStore:
    """Stores API-controlled desired state separately from live device state."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    async def load(self) -> dict[str, dict[str, Any]]:
        async with self._lock:
            return await asyncio.to_thread(self._load)

    async def update(self, manager: str, desired: dict[str, Any]) -> None:
        async with self._lock:
            values = await asyncio.to_thread(self._load)
            values[manager] = desired
            await asyncio.to_thread(self._save, values)

    async def clear(self) -> None:
        async with self._lock:
            try:
                await asyncio.to_thread(self.path.unlink)
            except FileNotFoundError:
                pass

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            with self.path.open(encoding="utf-8") as state_file:
                value = json.load(state_file)
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"unable to read persisted state {self.path}: {error}") from error
        if not isinstance(value, dict) or not all(isinstance(key, str) and isinstance(item, dict)
                                                   for key, item in value.items()):
            raise ValueError(f"persisted state {self.path} must contain an object of manager states")
        return value

    def _save(self, values: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as state_file:
                json.dump(values, state_file, indent=2, sort_keys=True)
                state_file.write("\n")
                state_file.flush()
                os.fsync(state_file.fileno())
            os.replace(temporary_name, self.path)
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
