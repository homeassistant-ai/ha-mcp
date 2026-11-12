"""``_component_context`` fallbacks, on real Core and the real component.

The server reads a context from the component's live trace store only when the
component answered the trace-context command. These tests run that decision on
the real client: the component's own schema and trace store answer it, and Core
answers ``unknown_command`` when the component is absent. A component that
gives no answer at all is the one client replacement allowed
(``tests/AGENTS.md``).
"""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.tools import trace_lookup
from ha_mcp.tools.trace_lookup import _component_context


@pytest.fixture
def invalidated(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """A spy on the capability invalidation; the real one still runs."""
    spy = MagicMock(wraps=trace_lookup.invalidate_caps)
    monkeypatch.setattr(trace_lookup, "invalidate_caps", spy)
    return spy


class TestComponentContextFallbacks:
    """Every non-answer sends the caller to Core's own ``trace/contexts``."""

    async def test_a_component_that_gives_no_answer_uses_core_s_lookup(
        self,
        component_core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A dropped socket is not the component's authoritative "no match", so
        the caller must still try Core after asking the component."""
        monkeypatch.setattr(
            component_core_client,
            "send_websocket_message",
            AsyncMock(side_effect=HomeAssistantConnectionError("socket closed")),
        )

        assert await _component_context(component_core_client, "ctx-1") is None

        send = cast(AsyncMock, component_core_client.send_websocket_message)
        assert [call.args[0] for call in send.call_args_list] == [
            {"type": trace_lookup.TRACE_CONTEXT_COMMAND, "context_id": "ctx-1"}
        ]

    async def test_an_unknown_component_command_drops_the_stale_capability(
        self,
        core_client: HomeAssistantClient,
        invalidated: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A downgraded component must not keep the capability that routes to a
        command Core refuses."""
        monkeypatch.setattr(
            core_client,
            "send_websocket_message",
            AsyncMock(wraps=core_client.send_websocket_message),
        )
        monkeypatch.setattr(
            trace_lookup, "component_supports", lambda _caps, _capability: True
        )

        assert await _component_context(core_client, "ctx-1") is None

        send = cast(AsyncMock, core_client.send_websocket_message)
        assert [call.args[0] for call in send.call_args_list] == [
            {"type": trace_lookup.TRACE_CONTEXT_COMMAND, "context_id": "ctx-1"}
        ]
        invalidated.assert_called_once_with(core_client)

    async def test_a_rejected_domain_uses_core_s_lookup(
        self,
        component_core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A domain the component does not trace must not hide Core's own run."""
        monkeypatch.setattr(
            component_core_client,
            "send_websocket_message",
            AsyncMock(wraps=component_core_client.send_websocket_message),
        )

        assert (
            await _component_context(component_core_client, "ctx-1", domain="scene")
            is None
        )

        send = cast(AsyncMock, component_core_client.send_websocket_message)
        assert [call.args[0] for call in send.call_args_list] == [
            {
                "type": trace_lookup.TRACE_CONTEXT_COMMAND,
                "context_id": "ctx-1",
                "domain": "scene",
            }
        ]

    async def test_home_assistant_without_a_trace_store_uses_core_s_lookup(
        self,
        component_core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An install with no trace store has no component answer to trust."""
        monkeypatch.setattr(
            component_core_client,
            "send_websocket_message",
            AsyncMock(wraps=component_core_client.send_websocket_message),
        )

        assert await _component_context(component_core_client, "ctx-1") is None

        send = cast(AsyncMock, component_core_client.send_websocket_message)
        assert [call.args[0] for call in send.call_args_list] == [
            {"type": trace_lookup.TRACE_CONTEXT_COMMAND, "context_id": "ctx-1"}
        ]
