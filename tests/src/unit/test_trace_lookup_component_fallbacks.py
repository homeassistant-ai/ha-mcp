"""``_component_context`` fallbacks, on real Core and the real component.

The server reads a context from the component's live trace store only when the
component answered the trace-context command. These tests run that decision on
the real client: the component's own schema and trace store answer it, and Core
answers ``unknown_command`` when the component is absent. A component that
gives no answer at all is the one client replacement allowed
(``tests/AGENTS.md``).
"""

from __future__ import annotations

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
        the caller must still try Core."""
        monkeypatch.setattr(
            component_core_client,
            "send_websocket_message",
            AsyncMock(side_effect=HomeAssistantConnectionError("socket closed")),
        )

        assert await _component_context(component_core_client, "ctx-1") is None

    async def test_an_unknown_component_command_drops_the_stale_capability(
        self,
        core_client: HomeAssistantClient,
        invalidated: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A downgraded component must not keep the capability that routes to a
        command Core refuses."""
        monkeypatch.setattr(
            trace_lookup, "component_supports", lambda _caps, _capability: True
        )

        assert await _component_context(core_client, "ctx-1") is None
        invalidated.assert_called_once_with(core_client)

    async def test_a_rejected_domain_uses_core_s_lookup(
        self, component_core_client: HomeAssistantClient
    ) -> None:
        """A domain the component does not trace must not hide Core's own run."""
        assert (
            await _component_context(component_core_client, "ctx-1", domain="scene")
            is None
        )

    async def test_home_assistant_without_a_trace_store_uses_core_s_lookup(
        self, component_core_client: HomeAssistantClient
    ) -> None:
        """An install with no trace store has no component answer to trust."""
        assert await _component_context(component_core_client, "ctx-1") is None
