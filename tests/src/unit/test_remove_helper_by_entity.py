"""ha_remove_helpers_integrations with an entity_id and no helper_type.

The entity registry identifies the helper, so an agent need not know which
helper type created an entity, and an entity of a non-helper integration is
refused instead of deleting that integration's config entry.
"""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools.tools_integrations import IntegrationTools

# What Core's GET /api/config/config_entries/flow_handlers?type=helper returns:
# its helper flows plus custom integrations of integration_type "helper".
_HELPER_FLOW_DOMAINS = ["group", "otp", "template", "utility_meter"]


def _client(registry_row: dict[str, Any]) -> MagicMock:
    client = MagicMock()
    client.get_entity_state = AsyncMock(return_value=None)
    client.delete_config_entry = AsyncMock(return_value={"require_restart": False})
    client._request = AsyncMock(return_value=_HELPER_FLOW_DOMAINS)
    client.ws_deletes = []

    async def send(message: dict[str, Any]) -> dict[str, Any]:
        if message["type"] == "config/entity_registry/get":
            return {"success": True, "result": registry_row}
        if message["type"] == "config/entity_registry/list":
            return {"success": True, "result": [registry_row]}
        if message["type"].endswith("/delete"):
            client.ws_deletes.append(message)
        return {"success": True, "result": {}}

    client.send_websocket_message = AsyncMock(side_effect=send)
    return client


async def _remove(client: MagicMock, entity_id: str) -> dict[str, Any]:
    result: dict[str, Any] = await IntegrationTools(
        client
    ).ha_remove_helpers_integrations(target=entity_id, confirm=True, wait=False)
    return result


async def test_entity_of_a_non_helper_integration_is_refused() -> None:
    """A Hue light named without helper_type must not take Hue with it."""
    client = _client(
        {
            "entity_id": "light.hue_lamp",
            "platform": "hue",
            "config_entry_id": "hue_entry",
        }
    )
    with pytest.raises(ToolError) as exc_info:
        await _remove(client, "light.hue_lamp")
    err = json.loads(str(exc_info.value))
    assert err["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    client.delete_config_entry.assert_not_awaited()
    assert client.ws_deletes == []


async def test_flow_helper_is_removed_without_naming_its_type() -> None:
    client = _client(
        {
            "entity_id": "sensor.energy_peak",
            "platform": "utility_meter",
            "config_entry_id": "um_entry",
        }
    )
    result = await _remove(client, "sensor.energy_peak")
    assert result["success"] is True
    client.delete_config_entry.assert_awaited_once_with("um_entry")


async def test_helper_that_only_core_lists_is_removed_by_its_entity() -> None:
    """otp (and custom-integration helpers) are helpers in Core's list but not
    flow types ha-mcp can create, so the entity alone must still remove them."""
    client = _client(
        {
            "entity_id": "sensor.my_otp",
            "platform": "otp",
            "config_entry_id": "otp_entry",
        }
    )
    result = await _remove(client, "sensor.my_otp")
    assert result["success"] is True
    client.delete_config_entry.assert_awaited_once_with("otp_entry")


async def test_storage_helper_is_removed_without_naming_its_type() -> None:
    client = _client(
        {
            "entity_id": "input_boolean.guest_mode",
            "platform": "input_boolean",
            "unique_id": "guest_mode",
            "config_entry_id": None,
        }
    )
    result = await _remove(client, "input_boolean.guest_mode")
    assert result["success"] is True
    assert client.ws_deletes == [
        {"type": "input_boolean/delete", "input_boolean_id": "guest_mode"}
    ]
    client.delete_config_entry.assert_not_awaited()
