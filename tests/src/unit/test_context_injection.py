"""Unit tests for FastMCP Context injection in long-running tools.

Each tool is verified twice:
- legacy path: called with ``ctx=None`` (or omitted) — must work unchanged
- progress path: called with a fake ``Context`` whose ``report_progress`` and
  ``info`` are AsyncMock — those must be awaited at least once
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp.tools.device_control import DeviceControlTools
from ha_mcp.tools.smart_search import SmartSearchTools
from ha_mcp.tools.tools_hacs import HacsTools
from ha_mcp.tools.tools_history import HistoryTools
from ha_mcp.tools.tools_traces import TraceTools


def _make_ctx() -> MagicMock:
    """Build a fake FastMCP Context with the awaitable surface we use."""
    ctx = MagicMock()
    ctx.report_progress = AsyncMock()
    ctx.info = AsyncMock()
    ctx.debug = AsyncMock()
    ctx.warning = AsyncMock()
    ctx.error = AsyncMock()
    return ctx


def _mock_ha_client() -> MagicMock:
    """Minimal mock HomeAssistantClient sufficient for these unit paths."""
    client = MagicMock()
    client.base_url = "http://homeassistant.local"
    client.token = "test_token"
    client.verify_ssl = True
    return client


# ---------------------------------------------------------------------------
# smart_search.deep_search (the engine behind ha_deep_search)
# ---------------------------------------------------------------------------


@pytest.fixture
def smart_search_tools() -> SmartSearchTools:
    client = _mock_ha_client()
    # No entities → all phases short-circuit cleanly without further mocking.
    client.get_states = AsyncMock(return_value=[])
    # Helper phase issues input_*/list WebSocket calls; succeed with empty results.
    client.send_websocket_message = AsyncMock(return_value={"success": True, "result": []})
    return SmartSearchTools(client=client)


@pytest.mark.asyncio
async def test_deep_search_works_without_ctx(smart_search_tools: SmartSearchTools) -> None:
    """Legacy callers passing no ctx still get a normal result dict."""
    result = await smart_search_tools.deep_search(
        "anything", search_types=["helper"], limit=5
    )
    assert result["success"] is True
    assert result["query"] == "anything"
    assert "helpers" in result


@pytest.mark.asyncio
async def test_deep_search_emits_progress_with_ctx(
    smart_search_tools: SmartSearchTools,
) -> None:
    """With a Context supplied, progress + info events are awaited."""
    ctx = _make_ctx()
    result = await smart_search_tools.deep_search(
        "anything", search_types=["helper"], limit=5, ctx=ctx
    )
    assert result["success"] is True
    ctx.info.assert_awaited()
    # At minimum: initial progress + post-fetch + post-helper-phase
    assert ctx.report_progress.await_count >= 3
    # Each progress call should carry a message string
    for call in ctx.report_progress.await_args_list:
        assert "message" in call.kwargs
        assert isinstance(call.kwargs["message"], str)


# ---------------------------------------------------------------------------
# tools_history.HistoryTools.ha_get_history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ha_get_history_works_without_ctx() -> None:
    """ha_get_history runs end-to-end with ctx omitted."""
    client = _mock_ha_client()
    history_tool = HistoryTools(client).ha_get_history

    fake_ws = AsyncMock()
    fake_ws.disconnect = AsyncMock()
    fake_result = {"success": True, "source": "history", "entities": []}

    with (
        patch(
            "ha_mcp.tools.tools_history.get_connected_ws_client",
            new=AsyncMock(return_value=(fake_ws, None)),
        ),
        patch(
            "ha_mcp.tools.tools_history._fetch_history",
            new=AsyncMock(return_value=fake_result),
        ),
    ):
        result = await history_tool(entity_ids="sensor.test")

    assert result is fake_result
    fake_ws.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_ha_get_history_emits_progress_with_ctx() -> None:
    """ha_get_history emits at least the connect / query / done events."""
    client = _mock_ha_client()
    history_tool = HistoryTools(client).ha_get_history
    ctx = _make_ctx()

    fake_ws = AsyncMock()
    fake_ws.disconnect = AsyncMock()
    fake_result = {"success": True, "source": "history", "entities": []}

    with (
        patch(
            "ha_mcp.tools.tools_history.get_connected_ws_client",
            new=AsyncMock(return_value=(fake_ws, None)),
        ),
        patch(
            "ha_mcp.tools.tools_history._fetch_history",
            new=AsyncMock(return_value=fake_result),
        ),
    ):
        result = await history_tool(entity_ids="sensor.test", ctx=ctx)

    assert result is fake_result
    ctx.info.assert_awaited()
    # Expected events: connect (0), query (1), done (3)
    assert ctx.report_progress.await_count >= 3


# ---------------------------------------------------------------------------
# tools_traces.TraceTools.ha_get_automation_traces
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ha_get_automation_traces_works_without_ctx() -> None:
    """ha_get_automation_traces is callable without ctx."""
    client = _mock_ha_client()
    client.get_entity_state = AsyncMock(
        return_value={"state": "on", "attributes": {"id": "abc"}}
    )
    trace_tool = TraceTools(client).ha_get_automation_traces

    fake_ws = AsyncMock()
    fake_ws.disconnect = AsyncMock()
    fake_ws.send_command = AsyncMock(return_value={"success": True, "result": []})

    with (
        patch(
            "ha_mcp.tools.tools_traces.get_connected_ws_client",
            new=AsyncMock(return_value=(fake_ws, None)),
        ),
        patch(
            "ha_mcp.tools.tools_traces._resolve_trace_item_id",
            new=AsyncMock(return_value="abc"),
        ),
    ):
        result = await trace_tool(automation_id="automation.demo")

    assert result["success"] is True
    assert result["trace_count"] == 0


@pytest.mark.asyncio
async def test_ha_get_automation_traces_emits_progress_with_ctx() -> None:
    """ha_get_automation_traces emits info + at least 3 progress events."""
    client = _mock_ha_client()
    client.get_entity_state = AsyncMock(
        return_value={"state": "on", "attributes": {"id": "abc"}}
    )
    trace_tool = TraceTools(client).ha_get_automation_traces
    ctx = _make_ctx()

    fake_ws = AsyncMock()
    fake_ws.disconnect = AsyncMock()
    # Return a non-empty trace list so we follow the standard "list" branch
    # and skip the diagnostics gather, keeping the progress-event count
    # deterministic at the expected 3.
    fake_ws.send_command = AsyncMock(
        return_value={
            "success": True,
            "result": [
                {"run_id": "1.0", "timestamp": "2025-01-01T00:00:00Z", "state": "stopped"}
            ],
        }
    )

    with (
        patch(
            "ha_mcp.tools.tools_traces.get_connected_ws_client",
            new=AsyncMock(return_value=(fake_ws, None)),
        ),
        patch(
            "ha_mcp.tools.tools_traces._resolve_trace_item_id",
            new=AsyncMock(return_value="abc"),
        ),
    ):
        result = await trace_tool(automation_id="automation.demo", ctx=ctx)

    assert result["success"] is True
    ctx.info.assert_awaited()
    assert ctx.report_progress.await_count >= 3


# ---------------------------------------------------------------------------
# tools_hacs.HacsTools.ha_hacs_search
# ---------------------------------------------------------------------------


async def _identity_timezone(_client: Any, data: dict[str, Any]) -> dict[str, Any]:
    """Stand-in for add_timezone_metadata that doesn't hit the HA client."""
    return data


@pytest.mark.asyncio
async def test_ha_hacs_search_works_without_ctx() -> None:
    client = _mock_ha_client()
    hacs_tool = HacsTools(client).ha_hacs_search

    ws = AsyncMock()
    ws.send_command = AsyncMock(return_value={"success": True, "result": []})

    with (
        patch(
            "ha_mcp.tools.tools_hacs._assert_hacs_available",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "ha_mcp.client.websocket_client.get_websocket_client",
            new=AsyncMock(return_value=ws),
        ),
        patch(
            "ha_mcp.tools.tools_hacs.add_timezone_metadata",
            new=_identity_timezone,
        ),
    ):
        result = await hacs_tool(query="anything")

    assert result["success"] is True
    assert result["total_matches"] == 0


@pytest.mark.asyncio
async def test_ha_hacs_search_emits_progress_with_ctx() -> None:
    client = _mock_ha_client()
    hacs_tool = HacsTools(client).ha_hacs_search
    ctx = _make_ctx()

    ws = AsyncMock()
    ws.send_command = AsyncMock(return_value={"success": True, "result": []})

    with (
        patch(
            "ha_mcp.tools.tools_hacs._assert_hacs_available",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "ha_mcp.client.websocket_client.get_websocket_client",
            new=AsyncMock(return_value=ws),
        ),
        patch(
            "ha_mcp.tools.tools_hacs.add_timezone_metadata",
            new=_identity_timezone,
        ),
    ):
        result = await hacs_tool(query="anything", ctx=ctx)

    assert result["success"] is True
    ctx.info.assert_awaited()
    # Expected: availability check (0), fetch list (1), filter (2), matched (3)
    assert ctx.report_progress.await_count >= 4


# ---------------------------------------------------------------------------
# device_control.DeviceControlTools.bulk_device_control (sequential)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_device_control_works_without_ctx() -> None:
    """bulk_device_control runs without ctx in sequential mode."""
    client = _mock_ha_client()
    tools = DeviceControlTools(client=client)

    async def fake_control(**kwargs: Any) -> dict[str, Any]:
        return {
            "command_sent": True,
            "operation_id": f"op-{kwargs['entity_id']}",
            "entity_id": kwargs["entity_id"],
            "action": kwargs["action"],
        }

    tools.control_device_smart = AsyncMock(side_effect=fake_control)  # type: ignore[method-assign]

    result = await tools.bulk_device_control(
        operations=[
            {"entity_id": "light.a", "action": "on"},
            {"entity_id": "light.b", "action": "off"},
        ],
        parallel=False,
    )

    assert result["successful_commands"] == 2
    assert len(result["operation_ids"]) == 2


@pytest.mark.asyncio
async def test_bulk_device_control_emits_progress_with_ctx_sequential() -> None:
    """Sequential mode emits one progress event per dispatched op + framing."""
    client = _mock_ha_client()
    tools = DeviceControlTools(client=client)
    ctx = _make_ctx()

    async def fake_control(**kwargs: Any) -> dict[str, Any]:
        return {
            "command_sent": True,
            "operation_id": f"op-{kwargs['entity_id']}",
            "entity_id": kwargs["entity_id"],
            "action": kwargs["action"],
        }

    tools.control_device_smart = AsyncMock(side_effect=fake_control)  # type: ignore[method-assign]

    result = await tools.bulk_device_control(
        operations=[
            {"entity_id": "light.a", "action": "on"},
            {"entity_id": "light.b", "action": "off"},
        ],
        parallel=False,
        ctx=ctx,
    )

    assert result["successful_commands"] == 2
    ctx.info.assert_awaited()
    # Initial dispatch event + 2 per-op events + final completion event = 4
    assert ctx.report_progress.await_count >= 4


@pytest.mark.asyncio
async def test_bulk_device_control_parallel_emits_dispatch_only() -> None:
    """Parallel mode emits framing events but no per-op progress mid-flight."""
    client = _mock_ha_client()
    tools = DeviceControlTools(client=client)
    ctx = _make_ctx()

    async def fake_control(**kwargs: Any) -> dict[str, Any]:
        return {
            "command_sent": True,
            "operation_id": f"op-{kwargs['entity_id']}",
            "entity_id": kwargs["entity_id"],
            "action": kwargs["action"],
        }

    tools.control_device_smart = AsyncMock(side_effect=fake_control)  # type: ignore[method-assign]

    await tools.bulk_device_control(
        operations=[
            {"entity_id": "light.a", "action": "on"},
            {"entity_id": "light.b", "action": "off"},
        ],
        parallel=True,
        ctx=ctx,
    )

    ctx.info.assert_awaited()
    # Parallel: dispatching (0) + completion event = 2 framing events.
    assert ctx.report_progress.await_count == 2
