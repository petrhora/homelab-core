import asyncio
import contextlib
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any, Callable
from zoneinfo import ZoneInfo

from ..config import LightingConfig, LightingWindowConfig
from ..runtime.commands import Command, CommandBus
from ..runtime.state import StateStore, utc_now
from .base import Manager, ManagerStatus


WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
RETRY_SECONDS = 1


class LightingManager(Manager):
    name = "lighting"

    def __init__(self, config: LightingConfig, state: StateStore, commands: CommandBus,
                 now: Callable[[tzinfo], datetime] | None = None) -> None:
        self.config = config
        self.state = state
        self.commands = commands
        self._now = now or (lambda timezone: datetime.now(timezone))
        self._allowed: bool | None = None
        self._next_transition: datetime | None = None
        self._task: asyncio.Task[None] | None = None
        self._changed = asyncio.Event()
        self._lock = asyncio.Lock()
        self._status = ManagerStatus.STARTING
        self._error: str | None = None

    async def start(self) -> None:
        try:
            await self._reconcile()
            self._status = ManagerStatus.READY
            self._error = None
        except Exception as error:
            self._status = ManagerStatus.DEGRADED
            self._error = str(error)
        self._task = asyncio.create_task(self._run(), name="lighting-schedule")
        await self._publish()

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._status = ManagerStatus.STOPPED
        await self._publish()

    async def health(self) -> dict:
        return {"status": self._status.value, "error": self._error}

    async def snapshot(self) -> dict:
        return await self._state()

    async def execute(self, command: Command) -> dict:
        try:
            async with self._lock:
                values = self.config.model_dump()
                values.update(command.payload)
                previous_config = self.config
                previous_allowed = self._allowed
                previous_next_transition = self._next_transition
                self.config = LightingConfig.model_validate(values)
                try:
                    await self._reconcile_locked()
                except Exception:
                    self.config = previous_config
                    self._allowed = previous_allowed
                    self._next_transition = previous_next_transition
                    if previous_allowed is not None:
                        # Both outputs may have changed before a later schedule command failed.
                        with contextlib.suppress(Exception):
                            await self.commands.dispatch("rgb.schedule", {"allowed": previous_allowed})
                        with contextlib.suppress(Exception):
                            await self.commands.dispatch("fans.schedule", {"allowed": previous_allowed})
                    raise
                self._status = ManagerStatus.READY
                self._error = None
        except Exception as error:
            self._status = ManagerStatus.DEGRADED
            self._error = str(error)
            raise
        finally:
            self._changed.set()
            await self._publish()
        return await self._state()

    async def _run(self) -> None:
        while True:
            self._changed.clear()
            try:
                await self._reconcile()
                self._status = ManagerStatus.READY
                self._error = None
                seconds = (3600 if self._next_transition is None else
                           max(self._next_transition.timestamp() - self._current_time().timestamp(), 0))
            except Exception as error:
                self._status = ManagerStatus.DEGRADED
                self._error = str(error)
                seconds = RETRY_SECONDS
            await self._publish()
            try:
                await asyncio.wait_for(self._changed.wait(), seconds)
            except TimeoutError:
                pass

    async def _reconcile(self) -> None:
        async with self._lock:
            await self._reconcile_locked()

    async def _reconcile_locked(self) -> None:
        now = self._current_time()
        allowed = self._is_allowed(now)
        self._next_transition = self._find_next_transition(now)
        if allowed == self._allowed:
            return
        await self.commands.dispatch("rgb.schedule", {"allowed": allowed})
        await self.commands.dispatch("fans.schedule", {"allowed": allowed})
        self._allowed = allowed

    def allowed_now(self) -> bool:
        return self._is_allowed(self._current_time())

    def _timezone(self) -> tzinfo:
        return ZoneInfo(self.config.timezone) if self.config.timezone else datetime.now().astimezone().tzinfo  # type: ignore[return-value]

    def _current_time(self) -> datetime:
        return self._now(self._timezone())

    def _is_allowed(self, now: datetime) -> bool:
        if not self.config.enabled:
            return True
        timezone = now.tzinfo
        if timezone is None:
            raise ValueError("lighting schedule requires a timezone-aware datetime")
        for day_offset in (0, -1):
            window_date = now.date() + timedelta(days=day_offset)
            for window in self.config.windows:
                if WEEKDAYS[window_date.weekday()] not in window.days:
                    continue
                start, end = _parse_time(window.start), _parse_time(window.end)
                starts_at = _local_datetime(window_date, start, timezone)
                ends_at = _local_datetime(window_date + timedelta(days=start > end), end, timezone)
                if starts_at.timestamp() <= now.timestamp() < ends_at.timestamp():
                    return True
        return False

    def _find_next_transition(self, now: datetime) -> datetime | None:
        if not self.config.enabled or not self.config.windows:
            return None
        timezone = now.tzinfo
        if timezone is None:
            raise ValueError("lighting schedule requires a timezone-aware datetime")
        transitions: list[datetime] = []
        for day_offset in range(8):
            date = now.date() + timedelta(days=day_offset)
            for window in self.config.windows:
                if WEEKDAYS[date.weekday()] not in window.days:
                    continue
                start = _local_datetime(date, _parse_time(window.start), timezone)
                end_date = date + timedelta(days=1) if _parse_time(window.end) < _parse_time(window.start) else date
                end = _local_datetime(end_date, _parse_time(window.end), timezone)
                transitions.extend(transition for transition in (start, end) if transition.timestamp() > now.timestamp())
        return min(transitions, default=None)

    async def _publish(self) -> None:
        await self.state.update(self.name, await self._state())

    async def _state(self) -> dict[str, Any]:
        return {"desired": self.config.model_dump(), "actual": {"allowed": self._allowed,
                "next_transition": self._next_transition.isoformat() if self._next_transition else None},
                "collected_at": utc_now()}


def _parse_time(value: str) -> time:
    return time.fromisoformat(value)


def _local_datetime(value_date: date, value: time, timezone: tzinfo) -> datetime:
    naive = datetime.combine(value_date, value)
    candidates = []
    for fold in (0, 1):
        candidate = naive.replace(tzinfo=timezone, fold=fold)
        if datetime.fromtimestamp(candidate.timestamp(), timezone).replace(tzinfo=None) == naive:
            candidates.append(candidate)
    if candidates:
        return min(candidates, key=datetime.timestamp)
    # A skipped DST wall time begins at the first valid time after the gap.
    for minute in range(1, 181):
        shifted = naive + timedelta(minutes=minute)
        for fold in (0, 1):
            candidate = shifted.replace(tzinfo=timezone, fold=fold)
            if datetime.fromtimestamp(candidate.timestamp(), timezone).replace(tzinfo=None) == shifted:
                return candidate
    raise ValueError("unable to resolve local schedule time")
