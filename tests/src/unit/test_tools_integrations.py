"""
Unit tests for src/ha_mcp/tools/tools_integrations.py module-level helpers.

Phase 2 of #1007: Adds _get_entry_id_for_flow_helper, the lookup helper that
resolves a flow-helper entity_id to its config_entry_id via the
config/entity_registry/get WebSocket API.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from ha_mcp.tools.tools_integrations import _get_entry_id_for_flow_helper


def _make_client(
    ws_response: Any = None, raises: Exception | None = None
) -> MagicMock:
    """Build a mock client whose send_websocket_message returns / raises."""
    client = MagicMock()
    if raises is not None:
        client.send_websocket_message = AsyncMock(side_effect=raises)
    else:
        client.send_websocket_message = AsyncMock(return_value=ws_response)
    return client


class TestGetEntryIdForFlowHelper:
    """Unit tests for the flow-helper entry_id lookup."""

    async def test_returns_entry_id_for_full_entity_id(self) -> None:
        client = _make_client(
            {"success": True, "result": {"config_entry_id": "abc123"}}
        )
        result = await _get_entry_id_for_flow_helper(
            client, "utility_meter", "sensor.peak"
        )
        assert result == "abc123"

    async def test_completes_bare_id_with_helper_type(self) -> None:
        client = _make_client(
            {"success": True, "result": {"config_entry_id": "def456"}}
        )
        result = await _get_entry_id_for_flow_helper(
            client, "template", "my_sensor"
        )
        assert result == "def456"
        sent = client.send_websocket_message.await_args.args[0]
        assert sent["entity_id"] == "template.my_sensor"

    async def test_returns_none_for_unknown_helper_type(self) -> None:
        client = _make_client()
        result = await _get_entry_id_for_flow_helper(
            client, "input_button", "my_button"  # SIMPLE, not FLOW
        )
        assert result is None
        client.send_websocket_message.assert_not_awaited()

    async def test_returns_none_when_entity_not_in_registry(self) -> None:
        client = _make_client({"success": False, "error": "not_found"})
        result = await _get_entry_id_for_flow_helper(
            client, "template", "template.ghost"
        )
        assert result is None

    async def test_returns_none_when_entity_has_no_config_entry_id(self) -> None:
        # YAML-defined helper: entity exists but no config_entry_id
        client = _make_client(
            {"success": True, "result": {"entity_id": "template.x"}}
        )
        result = await _get_entry_id_for_flow_helper(
            client, "template", "template.x"
        )
        assert result is None

    async def test_websocket_exception_appends_to_warnings(self) -> None:
        client = _make_client(raises=ConnectionError("ws drop"))
        warnings: list[str] = []
        result = await _get_entry_id_for_flow_helper(
            client, "utility_meter", "sensor.x", warnings=warnings
        )
        assert result is None
        assert len(warnings) == 1
        assert "entity_registry/get failed" in warnings[0]
        assert "sensor.x" in warnings[0]

    async def test_websocket_exception_without_warnings_is_silent(self) -> None:
        client = _make_client(raises=ConnectionError("ws drop"))
        result = await _get_entry_id_for_flow_helper(
            client, "utility_meter", "sensor.x", warnings=None
        )
        assert result is None

    async def test_unexpected_result_shape_returns_none(self) -> None:
        # success but result is not a dict
        client = _make_client({"success": True, "result": "garbage"})
        result = await _get_entry_id_for_flow_helper(
            client, "template", "template.x"
        )
        assert result is None
