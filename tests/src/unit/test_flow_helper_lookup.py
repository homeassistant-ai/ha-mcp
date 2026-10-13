"""Unit tests for the flow-helper entity → config entry lookup."""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.flow_helper_lookup import get_entry_id_for_flow_helper


def _make_client(ws_response: Any = None, raises: Exception | None = None) -> MagicMock:
    """Build a mock client whose send_websocket_message returns / raises.

    The entity has no state unless a test gives it one.
    """
    client = MagicMock()
    client.get_entity_state = AsyncMock(return_value=None)
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

    @pytest.mark.parametrize(
        "reply",
        [
            "garbage",
            {"success": True, "result": "garbage"},
            {"success": True, "result": {}},
        ],
        ids=["reply_not_a_dict", "result_not_a_dict", "result_empty"],
    )
    async def test_reply_without_an_entry_is_a_failed_read(self, reply: Any) -> None:
        """A reply that carries no entry proves neither that the entity is
        missing nor that it belongs elsewhere; it is a failed read, and
        nothing is deleted on it."""
        client = _make_client(reply)
        with pytest.raises(ToolError) as exc_info:
            await get_entry_id_for_flow_helper(client, "template", "template.x")
        err = json.loads(str(exc_info.value))["error"]
        assert err["code"] == "SERVICE_CALL_FAILED"

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


_NOT_FOUND = {"success": False, "error": "Entity not found", "error_code": "not_found"}


async def test_entity_with_a_state_but_no_registry_entry_is_not_missing() -> None:
    """Core keeps no registry entry for an entity without a unique_id (a YAML
    template sensor without one, zone.home); it exists, so it must not read
    as missing."""
    client = _make_client(_NOT_FOUND)
    client.get_entity_state = AsyncMock(return_value={"state": "zoning"})
    entry_id, reason = await get_entry_id_for_flow_helper(
        client, "template", "zone.home"
    )
    assert (entry_id, reason) == (None, "not_registry_managed")


async def test_state_read_404_confirms_the_entity_is_missing() -> None:
    client = _make_client(_NOT_FOUND)
    client.get_entity_state = AsyncMock(
        side_effect=HomeAssistantAPIError("not found", status_code=404)
    )
    entry_id, reason = await get_entry_id_for_flow_helper(
        client, "template", "template.ghost"
    )
    assert (entry_id, reason) == (None, "not_in_registry")


async def test_failed_state_read_is_not_evidence_of_absence() -> None:
    client = _make_client(_NOT_FOUND)
    client.get_entity_state = AsyncMock(
        side_effect=HomeAssistantAPIError("server error", status_code=500)
    )
    with pytest.raises(HomeAssistantAPIError):
        await get_entry_id_for_flow_helper(client, "template", "template.x")


@pytest.mark.parametrize(
    ("platform", "retry_hint"),
    [("sun", "ha_remove_entity()"), ("input_boolean", "helper_type='input_boolean'")],
    ids=["not_a_helper", "other_helper"],
)
async def test_wrong_helper_type_refusal_names_the_owning_integration(
    platform: str, retry_hint: str
) -> None:
    """The refusal must say which integration owns the entity, so omitting
    helper_type is suggested only where it can work."""
    client = _make_client(
        {
            "success": True,
            "result": {"platform": platform, "config_entry_id": "e"},
        }
    )
    with pytest.raises(ToolError) as exc_info:
        await get_entry_id_for_flow_helper(client, "group", "sensor.x")
    response = json.loads(str(exc_info.value))
    err = response["error"]
    assert err["code"] == "VALIDATION_INVALID_PARAMETER"
    assert f"'{platform}'" in err["message"]
    assert response["platform"] == platform
    assert retry_hint in err["suggestion"]
