"""E2E coverage for ha_call_service's ws_command escape hatch (issue #1839).

``ws_command`` lets ha_call_service send a raw one-shot Home Assistant
WebSocket command that is NOT a registered service (motivating case:
``repairs/ignore_issue`` to dismiss a Repairs issue). See
``ServiceTools._call_ws_command`` in ``src/ha_mcp/tools/tools_service.py``.

This module proves the escape hatch reaches a real Home Assistant instance
end-to-end (round-trip against ``repairs/list_issues``, a safe one-shot read)
and that the validation refusals fire against the live server, not just in
unit tests.

NOTE on full-cycle coverage: a "create a repair issue -> ignore it via
ws_command -> verify it's dismissed" test is intentionally NOT included here.
Repairs are created server-side by integrations calling
``homeassistant.helpers.issue_registry.async_create_issue`` — there is no
simple, on-demand way to create one without coupling to unrelated internal
state (the ha_mcp_tools component itself calls ``async_create_issue``
internally under specific conditions, but that's not a general-purpose
lever), and the e2e test container config
(``tests/initial_test_state/configuration.yaml``) does not load any
integration that files a repair by default (confirmed against
``test_get_system_health_with_repairs`` in
``workflows/system/test_system_tools.py``, which only asserts the repairs
list shape, not a non-zero count, for the same reason). Fabricating a repair
purely for this test (e.g. importing ``issue_registry`` and calling
``async_create_issue`` directly against the test container) would be testing
a hand-rolled fixture, not the feature — and ``repairs/ignore_issue`` shares
the exact ``send_websocket_message`` dispatch path already exercised by the
``repairs/list_issues`` round-trip below and covered param-wise (mutual
exclusion, empty command, streaming/two-phase refusal, ha_mcp_tools
refusal, backend-failure propagation) by
``tests/src/unit/test_call_service_ws_command.py``.
"""

import logging

import pytest

from ...utilities.assertions import (
    MCPAssertions,
    extract_error_message,
    safe_call_tool,
)

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
@pytest.mark.services
class TestWsCommandRoundTrip:
    """Positive path: a real one-shot WS command reaches Home Assistant."""

    async def test_repairs_list_issues_round_trips(self, mcp_client):
        """repairs/list_issues is a safe, read-only one-shot WS command.

        Proves the ws_command escape hatch actually dispatches to Home
        Assistant and returns its result, not just that validation passes.
        """
        logger.info("Testing ha_call_service ws_command=repairs/list_issues")

        async with MCPAssertions(mcp_client) as mcp:
            data = await mcp.call_tool_success(
                "ha_call_service",
                {"ws_command": "repairs/list_issues"},
            )

        assert data.get("ws_command") == "repairs/list_issues"
        result = data.get("result")
        assert isinstance(result, dict), f"Expected a dict result, got: {result!r}"
        assert "issues" in result, f"Expected 'issues' key in result: {result!r}"
        assert isinstance(result["issues"], list), (
            f"'issues' should be a list (possibly empty): {result!r}"
        )

        logger.info(
            f"repairs/list_issues round-trip succeeded: "
            f"{len(result['issues'])} issue(s) found"
        )


@pytest.mark.asyncio
@pytest.mark.services
class TestWsCommandRefusals:
    """Validation refusals fire against the live server."""

    async def test_subscribe_events_refused_as_streaming(self, mcp_client):
        """Subscription commands are rejected — ha_call_service only supports
        one-shot request/response WS commands."""
        result = await safe_call_tool(
            mcp_client,
            "ha_call_service",
            {"ws_command": "subscribe_events"},
        )

        error_msg = extract_error_message(result)
        assert "streaming or two-phase" in error_msg, (
            f"Expected streaming/two-phase refusal; got: {error_msg!r}"
        )

    async def test_ha_mcp_tools_prefix_refused(self, mcp_client):
        """The reserved ha_mcp_tools/* WebSocket namespace is refused,
        mirroring the domain refusal for the service-call path."""
        result = await safe_call_tool(
            mcp_client,
            "ha_call_service",
            {"ws_command": "ha_mcp_tools/overview"},
        )

        error_msg = extract_error_message(result)
        assert "ha_mcp_tools" in error_msg, (
            f"Expected ha_mcp_tools refusal; got: {error_msg!r}"
        )

    async def test_ws_command_with_domain_refused_as_not_both(self, mcp_client):
        """Passing ws_command together with domain/service is rejected —
        the two modes are mutually exclusive."""
        result = await safe_call_tool(
            mcp_client,
            "ha_call_service",
            {
                "ws_command": "repairs/ignore_issue",
                "domain": "light",
                "data": {"domain": "sun", "issue_id": "nonexistent", "ignore": True},
            },
        )

        error_msg = extract_error_message(result)
        assert "not both" in error_msg, (
            f"Expected mutual-exclusion refusal; got: {error_msg!r}"
        )

    async def test_call_service_invoker_refused(self, mcp_client):
        """ws_command='call_service' is refused rather than forwarded —
        proves the service-invocation bypass (which would skip the
        ha_mcp_tools domain guard) is closed against a real server, not
        just in the unit-level mock."""
        result = await safe_call_tool(
            mcp_client,
            "ha_call_service",
            {
                "ws_command": "call_service",
                "data": {"domain": "ha_mcp_tools", "service": "get_caller_token"},
            },
        )

        error_msg = extract_error_message(result)
        assert "invokes Home Assistant services" in error_msg, (
            f"Expected a services/safeguards refusal; got: {error_msg!r}"
        )
        assert "safeguards" in error_msg, (
            f"Expected the refusal to mention safeguards; got: {error_msg!r}"
        )

    async def test_blocked_write_command_refused(self, mcp_client):
        """Config-write WS commands (e.g. lovelace/config/save) are refused —
        they bypass a dedicated tool's backups and conflict checks, so
        ha_call_service rejects them before dispatch against a real server."""
        result = await safe_call_tool(
            mcp_client,
            "ha_call_service",
            {
                "ws_command": "lovelace/config/save",
                "data": {"config": {"views": []}},
            },
        )

        error_msg = extract_error_message(result)
        assert "dedicated tool guards with backups and conflict checks" in error_msg, (
            f"Expected a blocked-write refusal; got: {error_msg!r}"
        )

    async def test_reserved_envelope_key_in_data_refused(self, mcp_client):
        """data={"type": ...} is refused rather than silently overriding the
        validated ws_command — proves the type-override bypass is closed
        against a real server, not just in the unit-level mock."""
        result = await safe_call_tool(
            mcp_client,
            "ha_call_service",
            {
                "ws_command": "repairs/list_issues",
                "data": {"type": "subscribe_events"},
            },
        )

        error_msg = extract_error_message(result)
        assert "reserved" in error_msg, (
            f"Expected a reserved-envelope-key refusal; got: {error_msg!r}"
        )
