"""Context lookups fall back to Core when the component has no answer.

The server reads a context from the component's live trace store only when the
component answered the trace-context command. These tests resolve a context on
the real client: the component's own schema and trace store answer it, and Core
answers ``unknown_command`` when the component is absent. A component that
gives no answer at all is the one client replacement allowed
(``tests/AGENTS.md``).
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.components.automation.trace import AutomationTrace
from homeassistant.components.trace import async_store_trace
from homeassistant.core import Context, HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.tools import trace_lookup
from ha_mcp.tools.trace_lookup import resolve_context_trace

COMPONENT_LOOKUP = {
    "type": trace_lookup.TRACE_CONTEXT_COMMAND,
    "context_id": "ctx-1",
    "domain": "automation",
    "item_id": "uid_1",
}
CORE_LOOKUP = {"type": "trace/contexts", "domain": "automation", "item_id": "uid_1"}


@pytest.fixture
def invalidated(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """A spy on the capability invalidation; the real one still runs."""
    spy = MagicMock(wraps=trace_lookup.invalidate_caps)
    monkeypatch.setattr(trace_lookup, "invalidate_caps", spy)
    return spy


async def _store_run(hass: HomeAssistant) -> str:
    """Core stores one run of automation ``uid_1`` with context ``ctx-1``."""
    assert await async_setup_component(hass, "trace", {})
    run = AutomationTrace("uid_1", {"alias": "Lookup"}, None, Context(id="ctx-1"))
    run.finished()
    async_store_trace(hass, run, stored_traces=5)
    return run.run_id


def _sent(client: HomeAssistantClient) -> list[dict[str, Any]]:
    send = cast(AsyncMock, client.send_websocket_message)
    return [call.args[0] for call in send.call_args_list]


async def test_a_component_that_gives_no_answer_still_tries_core(
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dropped socket is not the component's authoritative "no match", so
    the lookup goes on to Core after asking the component."""
    monkeypatch.setattr(
        component_core_client,
        "send_websocket_message",
        AsyncMock(side_effect=HomeAssistantConnectionError("socket closed")),
    )

    with pytest.raises(HomeAssistantConnectionError):
        await resolve_context_trace(
            component_core_client, "automation", "uid_1", "ctx-1"
        )

    assert _sent(component_core_client) == [COMPONENT_LOOKUP, CORE_LOOKUP]


async def test_an_unknown_component_command_falls_back_and_drops_the_capability(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    invalidated: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A downgraded component must not keep the capability that routes to a
    command Core refuses, and Core's own store still answers the lookup."""
    run_id = await _store_run(hass)
    monkeypatch.setattr(
        core_client,
        "send_websocket_message",
        AsyncMock(wraps=core_client.send_websocket_message),
    )
    monkeypatch.setattr(
        trace_lookup, "component_supports", lambda _caps, _capability: True
    )

    match = await resolve_context_trace(core_client, "automation", "uid_1", "ctx-1")

    assert match.run_id == run_id
    assert _sent(core_client) == [COMPONENT_LOOKUP, CORE_LOOKUP]
    invalidated.assert_called_once_with(core_client)


async def test_home_assistant_without_a_trace_store_reports_core_s_answer(
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An install with no trace store has no component answer to trust; the
    lookup reports what Core says instead."""
    monkeypatch.setattr(
        component_core_client,
        "send_websocket_message",
        AsyncMock(wraps=component_core_client.send_websocket_message),
    )

    with pytest.raises(ToolError):
        await resolve_context_trace(
            component_core_client, "automation", "uid_1", "ctx-1"
        )

    assert _sent(component_core_client) == [COMPONENT_LOOKUP, CORE_LOOKUP]
