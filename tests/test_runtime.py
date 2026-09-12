import asyncio

import pytest

from homelab_core.config import AppConfig, load_config
from homelab_core.runtime.commands import CommandBus
from homelab_core.runtime.events import Event, EventBus
from homelab_core.runtime.state import StateStore


def test_default_config(tmp_path):
    config = load_config(tmp_path / "missing.toml")
    assert config.api.host == "127.0.0.1"
    assert config.api.port == 8000


def test_unknown_config_is_rejected(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[api]\nunsafe = true\n")
    with pytest.raises(Exception):
        load_config(path)


@pytest.mark.asyncio
async def test_state_is_copied():
    state = StateStore()
    value = {"nested": {"ok": True}}
    await state.update("test", value)
    value["nested"]["ok"] = False
    assert (await state.snapshot())["test"]["nested"]["ok"] is True


@pytest.mark.asyncio
async def test_event_bus_publishes():
    bus = EventBus()
    queue = bus.subscribe()
    await bus.publish(Event("test", {"value": 1}))
    assert (await queue.get()).payload["value"] == 1


@pytest.mark.asyncio
async def test_commands_are_serialized():
    bus = CommandBus()
    active = 0
    maximum = 0

    async def handler(command):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1

    bus.register("test", handler)
    await asyncio.gather(*(bus.dispatch("test", {}) for _ in range(5)))
    assert maximum == 1
