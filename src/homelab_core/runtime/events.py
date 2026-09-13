from dataclasses import dataclass, field
from typing import Any
import asyncio


@dataclass(frozen=True)
class Event:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)


class EventBusClosedError(RuntimeError):
    pass


class EventBus:
    def __init__(self, queue_size: int = 100) -> None:
        if queue_size <= 0:
            raise ValueError("queue_size must be positive")
        self._queue_size = queue_size
        self._subscribers: list[asyncio.Queue[Event]] = []
        self._closed = False

    def subscribe(self) -> asyncio.Queue[Event]:
        if self._closed:
            raise EventBusClosedError("event bus is closed")
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    def close(self) -> None:
        self._closed = True
        self._subscribers.clear()

    async def publish(self, event: Event) -> None:
        if self._closed:
            raise EventBusClosedError("event bus is closed")
        for queue in tuple(self._subscribers):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(event)
