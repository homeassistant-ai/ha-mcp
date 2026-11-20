"""An automation on a real ``hass`` for the ``enabled`` / ``run_actions`` tests.

``morning_tools`` gives the automation tools on the server's own client, with
Core holding ``automation.morning`` (config id ``morning-id``) in
automations.yaml. Its action fires ``morning_ran`` with the config's
``version``, so a test sees which version of the actions ran.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant, ServiceCall

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.config import Settings
from ha_mcp.tools import config_write_helpers, tools_config_automations, ws_waiters
from ha_mcp.tools.tools_config_automations import AutomationConfigTools

from ._automation_core import load_automations_file
from ._command_faults import pooled_socket
from ._config_writes import automation_tools
from ._receipt_faults import on_receipt
from ._ws_transport import abort_transport

ENTITY_ID = "automation.morning"
CONFIG_ID = "morning-id"
RAN = "morning_ran"


def morning(version: int = 1, **changes: Any) -> dict[str, Any]:
    """The automation's config (without its id) whose actions are ``version``."""
    return {
        "alias": "Morning",
        "triggers": [{"trigger": "event", "event_type": "wake_up"}],
        "actions": [{"event": RAN, "event_data": {"version": version}}],
        **changes,
    }


async def morning_tools(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    *also: dict[str, Any],
) -> AutomationConfigTools:
    """The automation tools, with Core running the morning automation (on)
    and the automations in ``also``."""
    tools = await automation_tools(hass, client, monkeypatch)
    await load_automations_file(
        hass, "automations.yaml", [{"id": CONFIG_ID, **morning()}, *also]
    )
    assert state_of(hass) == "on"
    return tools


def state_of(hass: HomeAssistant, entity_id: str = ENTITY_ID) -> str:
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    return state.state


async def turn_off_in_core(hass: HomeAssistant) -> None:
    await hass.services.async_call(
        "automation", "turn_off", {"entity_id": ENTITY_ID}, blocking=True
    )


async def set_automation(
    tools: AutomationConfigTools, **arguments: Any
) -> dict[str, Any]:
    """``ha_config_set_automation`` without the skill files attached."""
    arguments.setdefault("MandatoryBPS", False)
    result: dict[str, Any] = await tools.ha_config_set_automation(**arguments)
    return result


async def config_hash(tools: AutomationConfigTools, entity_id: str) -> str:
    """The hash ``ha_config_get_automation`` reports for the stored config."""
    current = await tools.ha_config_get_automation(entity_id=entity_id)
    return str(current["config_hash"])


@asynccontextmanager
async def slow_reload(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[None]:
    """Core runs the reload a write schedules only once the block ends, as on a
    slow install; the tool's wait for the reload is cut to a moment meanwhile."""
    monkeypatch.setattr(
        tools_config_automations,
        "config_reload_waiter",
        functools.partial(config_write_helpers.config_reload_waiter, timeout=0.05),
    )
    reload = hass.services.async_services_for_domain("automation")["reload"]
    release = asyncio.Event()

    async def held(call: ServiceCall) -> None:
        await release.wait()
        ran = hass.async_run_hass_job(reload.job, call)
        if ran is not None:
            await asyncio.gather(ran)

    hass.services.async_register("automation", "reload", held, reload.schema)
    try:
        yield
    finally:
        hass.services.async_register(
            "automation", "reload", reload.job.target, reload.schema
        )
        release.set()
        await hass.async_block_till_done()


@asynccontextmanager
async def unwatched_reload(
    hass: HomeAssistant, client: HomeAssistantClient
) -> AsyncIterator[None]:
    """The client's socket drops as Core receives the subscription to the
    reload event: the write goes on over REST with nothing watching the reload."""
    socket = await pooled_socket(client)
    with on_receipt(
        hass,
        "subscribe_events",
        lambda msg: abort_transport(socket),
        when=lambda msg: msg.get("event_type") == "automation_reloaded",
    ):
        yield


@contextmanager
def registration_polls() -> Iterator[AsyncMock]:
    """Spy on the tool's poll for an automation's entity by config id, cut to
    a moment; the real poll still reads Core."""
    poll = functools.partial(
        ws_waiters.wait_for_automation_entity_by_unique_id, timeout=0.1
    )
    with patch.object(
        tools_config_automations,
        "wait_for_automation_entity_by_unique_id",
        AsyncMock(wraps=poll),
    ) as spy:
        yield spy


def snapshots(settings: Settings) -> list[str]:
    """The auto-backup snapshots written so far."""
    directory = Path(settings.auto_backup_dir)
    return sorted(path.name for path in directory.rglob("*.yaml"))
