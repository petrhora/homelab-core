from dataclasses import dataclass
from typing import Any, Awaitable, Callable
import asyncio
import uuid


class CommandTimeoutError(TimeoutError):
    """The caller timed out while the command continues to own its resource."""

    def __init__(self, command: str, resource_id: str, timeout: float) -> None:
        super().__init__(f"Command {command} on {resource_id} timed out after {timeout:g}s")
        self.command = command
        self.resource_id = resource_id
        self.timeout = timeout


class CommandBusClosedError(RuntimeError):
    """The command bus no longer accepts work because shutdown has started."""


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
        self._operations: set[asyncio.Task[Any]] = set()
        self._active_operations: set[asyncio.Task[Any]] = set()
        self._closed = False

    def register(self, command_type: str, handler: Callable[[Command], Awaitable[Any]],
                 resource_id: str | None = None) -> None:
        self._handlers[command_type] = (handler, resource_id or command_type)

    async def dispatch(self, command_type: str, payload: dict[str, Any], timeout: float = 10.0) -> Any:
        if self._closed:
            raise CommandBusClosedError("Command bus is shutting down")
        if command_type not in self._handlers:
            raise ValueError(f"Unknown command: {command_type}")
        handler, resource_id = self._handlers[command_type]
        command = Command(command_type, resource_id, payload, str(uuid.uuid4()), timeout)
        lock = self._locks.setdefault(resource_id, asyncio.Lock())

        async def run_locked() -> Any:
            async with lock:
                if self._closed:
                    raise CommandBusClosedError("Command bus is shutting down")
                operation = asyncio.current_task()
                if operation is not None:
                    self._active_operations.add(operation)
                try:
                    return await handler(command)
                finally:
                    if operation is not None:
                        self._active_operations.discard(operation)

        # Do not cancel a handler that may be part-way through hardware I/O.
        # The resource lock remains held until this task has reconciled state.
        operation = asyncio.create_task(run_locked())
        self._operations.add(operation)
        operation.add_done_callback(self._complete_operation)
        try:
            return await asyncio.wait_for(asyncio.shield(operation), timeout)
        except asyncio.TimeoutError as error:
            raise CommandTimeoutError(command_type, resource_id, timeout) from error

    async def shutdown(self) -> None:
        """Stop admitting work and wait for in-progress hardware operations."""
        self._closed = True
        for operation in self._operations - self._active_operations:
            operation.cancel()
        if self._operations:
            await asyncio.gather(*self._operations, return_exceptions=True)

    def _complete_operation(self, operation: asyncio.Task[Any]) -> None:
        self._operations.discard(operation)
        self._active_operations.discard(operation)
        _consume_task_result(operation)


def _consume_task_result(task: asyncio.Task[Any]) -> None:
    if not task.cancelled():
        task.exception()
