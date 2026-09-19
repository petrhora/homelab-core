from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import AsyncIterator


@dataclass(frozen=True)
class ButtonInputEvent:
    pressed: bool


class PowerButtonDevice:
    async def open(self) -> None:
        raise NotImplementedError

    async def events(self) -> AsyncIterator[ButtonInputEvent]:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


class EvdevPowerButtonDevice(PowerButtonDevice):
    def __init__(self, name: str, path: str | None = None) -> None:
        self.name = name
        self.path = path
        self._device = None

    async def open(self) -> None:
        try:
            from evdev import InputDevice, list_devices
        except ImportError as error:
            raise RuntimeError("Install the 'hardware' extra to use the power button") from error

        paths = [self.path] if self.path else list_devices()
        for candidate in paths:
            if candidate is None:
                continue
            device = InputDevice(candidate)
            if device.name == self.name:
                try:
                    device.grab()
                except Exception:
                    device.close()
                    raise
                self._device = device
                return
            device.close()
        raise RuntimeError(f"Power button device {self.name!r} was not found")

    async def events(self) -> AsyncIterator[ButtonInputEvent]:
        if self._device is None:
            raise RuntimeError("Power button device is not open")
        from evdev import ecodes

        async for event in self._device.async_read_loop():
            if event.type == ecodes.EV_KEY and event.code == ecodes.KEY_POWER:
                if event.value in (0, 1):
                    yield ButtonInputEvent(pressed=event.value == 1)

    async def close(self) -> None:
        if self._device is not None:
            try:
                self._device.ungrab()
            finally:
                self._device.close()
                self._device = None


class FakePowerButtonDevice(PowerButtonDevice):
    def __init__(self) -> None:
        self.events_queue: asyncio.Queue[ButtonInputEvent | None] = asyncio.Queue()
        self.opened = False
        self.closed = False

    async def open(self) -> None:
        self.opened = True

    async def events(self) -> AsyncIterator[ButtonInputEvent]:
        while True:
            event = await self.events_queue.get()
            if event is None:
                return
            yield event

    async def close(self) -> None:
        self.closed = True
        await self.events_queue.put(None)

    async def press(self) -> None:
        await self.events_queue.put(ButtonInputEvent(True))

    async def release(self) -> None:
        await self.events_queue.put(ButtonInputEvent(False))
