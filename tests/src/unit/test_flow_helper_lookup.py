"""Unit tests for the flow-helper entity → config entry lookup."""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.flow_helper_lookup import get_entry_id_for_flow_helper


def _make_client(ws_response: Any = None, raises: Exception | None = None) -> MagicMock:
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
            {
                "success": True,
                "result": {"platform": "utility_meter", "config_entry_id": "abc123"},
            }
        )
        entry_id, reason = await get_entry_id_for_flow_helper(
            client, "utility_meter", "sensor.peak"
        )
        assert entry_id == "abc123"
        assert reason == "ok"

    async def test_returns_none_for_bare_id_flow_helper(self) -> None:
        # Flow helpers require full entity_id — bare IDs cannot be safely
        # completed because helper_type often differs from entity domain
        # (e.g. utility_meter → sensor.*, switch_as_x → switch/light.*).
        client = _make_client()
        entry_id, reason = await get_entry_id_for_flow_helper(
            client, "template", "my_sensor"
        )
        assert entry_id is None
        assert reason == "bare_id_not_supported"
        client.send_websocket_message.assert_not_awaited()

    async def test_returns_none_for_unknown_helper_type(self) -> None:
        client = _make_client()
        entry_id, reason = await get_entry_id_for_flow_helper(
            client,
            "input_button",
            "my_button",  # SIMPLE, not FLOW
        )
        assert entry_id is None
        assert reason == "wrong_helper_type"
        client.send_websocket_message.assert_not_awaited()

    async def test_returns_none_when_entity_not_in_registry(self) -> None:
        client = _make_client(
            {"success": False, "error": "Entity not found", "error_code": "not_found"}
        )
        entry_id, reason = await get_entry_id_for_flow_helper(
            client, "template", "template.ghost"
        )
        assert entry_id is None
        assert reason == "not_in_registry"

    async def test_returns_none_when_entity_has_no_config_entry_id(self) -> None:
        # YAML-defined helper: entity exists but no config_entry_id
        client = _make_client(
            {
                "success": True,
                "result": {"platform": "template", "entity_id": "template.x"},
            }
        )
        entry_id, reason = await get_entry_id_for_flow_helper(
            client, "template", "template.x"
        )
        assert entry_id is None
        assert reason == "no_config_entry"

    async def test_websocket_exception_appends_to_warnings(self) -> None:
        client = _make_client(raises=ConnectionError("ws drop"))
        warnings: list[str] = []
        entry_id, reason = await get_entry_id_for_flow_helper(
            client, "utility_meter", "sensor.x", warnings=warnings
        )
        assert entry_id is None
        assert reason == "lookup_failed"
        assert len(warnings) == 1
        assert "entity_registry/get failed" in warnings[0]
        assert "sensor.x" in warnings[0]

    async def test_websocket_exception_without_warnings_is_silent(self) -> None:
        client = _make_client(raises=ConnectionError("ws drop"))
        entry_id, reason = await get_entry_id_for_flow_helper(
            client, "utility_meter", "sensor.x", warnings=None
        )
        assert entry_id is None
        assert reason == "lookup_failed"

    @pytest.mark.parametrize("payload", ["garbage", {}])
    async def test_unexpected_result_shape_returns_none(self, payload: Any) -> None:
        """A non-entry payload is a missing entity, not a foreign platform."""
        client = _make_client({"success": True, "result": payload})
        entry_id, reason = await get_entry_id_for_flow_helper(
            client, "template", "template.x"
        )
        assert entry_id is None
        assert reason == "not_in_registry"

    async def test_connection_error_propagates(self) -> None:
        # Auth/connection errors must reach the outer handler — they are
        # not "lookup_failed", they are infrastructure failures.
        client = _make_client(raises=HomeAssistantConnectionError("network down"))
        with pytest.raises(HomeAssistantConnectionError):
            await get_entry_id_for_flow_helper(client, "utility_meter", "sensor.x")

    async def test_auth_error_propagates(self) -> None:
        client = _make_client(raises=HomeAssistantAuthError("token expired"))
        with pytest.raises(HomeAssistantAuthError):
            await get_entry_id_for_flow_helper(client, "utility_meter", "sensor.x")


async def test_blocked_registry_read_is_not_reported_as_missing() -> None:
    """A proxy-blocked read is no evidence the entity is absent."""
    client = _make_client(
        {
            "success": False,
            "error": "WebSocket request blocked (403 Forbidden): denied",
            "error_code": None,
        }
    )
    with pytest.raises(ToolError) as exc_info:
        await get_entry_id_for_flow_helper(client, "template", "template.x")
    err = json.loads(str(exc_info.value))["error"]
    assert err["code"] == "SERVICE_CALL_FAILED"
    assert "403 Forbidden" in err["message"]
