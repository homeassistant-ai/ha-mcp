"""
Unit tests for Group management tools.

Core owns group.set / group.remove validation; these tests pin that the tools
pass requests to Core over WebSocket, report Core's own rejection, and read
create/update and existence from the group's state instead of guessing.
"""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.backup_manager import _restore_group
from ha_mcp.client.rest_client import HomeAssistantAPIError
from ha_mcp.tools.tools_groups import GroupTools

# Core's WebSocket ``call_service`` reply for a payload its schema rejects, as
# ``send_websocket_message`` returns it.
_CORE_SLUG_REJECTION = {
    "success": False,
    "error": (
        "Command failed: invalid slug Living Room (try living_room) "
        "for dictionary value @ data['object_id']"
    ),
    "error_code": "invalid_format",
}


def _group_state(object_id: str, *, auto: bool, members: list[str]) -> dict[str, Any]:
    """A group state as Core writes it: ``auto`` only on group.set groups."""
    attributes: dict[str, Any] = {
        "entity_id": members,
        "order": 0,
        "friendly_name": object_id,
    }
    if auto:
        attributes["auto"] = True
    return {
        "entity_id": f"group.{object_id}",
        "state": "off",
        "attributes": attributes,
    }


@pytest.fixture
def states() -> dict[str, dict[str, Any]]:
    return {}


@pytest.fixture
def mock_client(states):
    """Client whose group services change ``states`` the way Core does."""
    client = MagicMock()

    async def get_entity_state(entity_id: str) -> dict[str, Any]:
        if entity_id not in states:
            raise HomeAssistantAPIError("Not found", status_code=404)
        return states[entity_id]

    async def send_websocket_message(message: dict[str, Any]) -> dict[str, Any]:
        data = message["service_data"]
        entity_id = f"group.{data['object_id']}"
        if message["service"] == "set" and entity_id not in states:
            states[entity_id] = _group_state(
                data["object_id"], auto=True, members=data.get("entities", [])
            )
        elif message["service"] == "remove":
            states.pop(entity_id, None)
        return {"success": True, "result": {"context": {"id": "ctx"}}}

    client.get_entity_state = AsyncMock(side_effect=get_entity_state)
    client.send_websocket_message = AsyncMock(side_effect=send_websocket_message)
    client.get_states = AsyncMock(side_effect=lambda: list(states.values()))
    client.call_service = AsyncMock(return_value=[])
    return client


@pytest.fixture
def tools(mock_client):
    return GroupTools(mock_client)


def _sent_call(mock_client) -> dict[str, Any]:
    return mock_client.send_websocket_message.call_args[0][0]


class TestListGroups:
    async def test_list_passes_core_attributes_through(self, tools, states):
        states["group.service_made"] = _group_state(
            "service_made", auto=True, members=["light.a"]
        )
        states["group.from_yaml"] = _group_state(
            "from_yaml", auto=False, members=["light.b"]
        )
        states["light.bed_light"] = {
            "entity_id": "light.bed_light",
            "state": "off",
            "attributes": {"friendly_name": "Bed Light"},
        }

        result = await tools.ha_config_list_groups()

        assert result["success"] is True
        assert result["count"] == 2
        by_id = {g["object_id"]: g for g in result["groups"]}
        assert by_id["service_made"]["auto"] is True
        assert by_id["from_yaml"]["auto"] is False
        assert by_id["service_made"]["entity_ids"] == ["light.a"]
        # Core never exposes a group's all/any mode, so the tool must not
        # report one.
        assert all("all" not in g for g in result["groups"])

    async def test_list_groups_error_handling(self, mock_client):
        mock_client.get_states = AsyncMock(side_effect=Exception("Connection failed"))

        with pytest.raises(ToolError) as exc_info:
            await GroupTools(mock_client).ha_config_list_groups()

        assert json.loads(str(exc_info.value))["success"] is False


class TestSetGroup:
    async def test_create_is_reported_from_core_state_not_arguments(
        self, tools, mock_client
    ):
        # The docstring's own create example passes name=, which the old
        # argument-based guess reported as "updated".
        result = await tools.ha_config_set_group(
            object_id="bedroom_lights",
            entities=["light.lamp", "light.ceiling"],
            name="Bedroom Lights",
        )

        assert result["message"] == "Successfully created group: group.bedroom_lights"
        assert _sent_call(mock_client) == {
            "type": "call_service",
            "domain": "group",
            "service": "set",
            "service_data": {
                "object_id": "bedroom_lights",
                "name": "Bedroom Lights",
                "entities": ["light.lamp", "light.ceiling"],
            },
        }

    async def test_entities_on_existing_group_is_reported_as_update(
        self, tools, states
    ):
        states["group.lights"] = _group_state("lights", auto=True, members=["light.a"])

        result = await tools.ha_config_set_group(
            object_id="lights", entities=["light.b"]
        )

        assert result["message"] == "Successfully updated group: group.lights"
        assert "warnings" not in result

    async def test_create_without_entities_reaches_core(self, tools, mock_client):
        # Core creates a group without members and names it after object_id.
        result = await tools.ha_config_set_group(object_id="empty_group")

        assert result["message"] == "Successfully created group: group.empty_group"
        assert _sent_call(mock_client)["service_data"] == {"object_id": "empty_group"}

    @pytest.mark.parametrize(
        "arguments",
        [
            {"entities": []},
            {"add_entities": []},
            {"entities": ["light.a"], "add_entities": ["light.b"]},
            {"object_id": "group.lights"},
        ],
        ids=["empty-entities", "empty-add", "exclusive-lists", "dotted-object-id"],
    )
    async def test_core_decides_validity(self, tools, mock_client, arguments):
        arguments = {"object_id": "lights", **arguments}

        await tools.ha_config_set_group(**arguments, wait=False)

        assert _sent_call(mock_client)["service_data"] == arguments

    async def test_core_schema_rejection_is_passed_through(self, tools, mock_client):
        mock_client.send_websocket_message = AsyncMock(
            return_value=_CORE_SLUG_REJECTION
        )

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_group(object_id="Living Room")

        error = json.loads(str(exc_info.value))["error"]
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "invalid slug Living Room (try living_room)" in error["message"]

    async def test_yaml_group_update_warns_reload_reverts_it(self, tools, states):
        states["group.from_yaml"] = _group_state(
            "from_yaml", auto=False, members=["light.a"]
        )

        result = await tools.ha_config_set_group(
            object_id="from_yaml", add_entities=["light.b"]
        )

        assert result["message"] == "Successfully updated group: group.from_yaml"
        assert any("YAML" in w for w in result["warnings"])

    async def test_all_on_is_sent_as_core_all(self, tools, mock_client):
        await tools.ha_config_set_group(
            object_id="test_group", entities=["light.lamp1"], all_on=True
        )

        assert _sent_call(mock_client)["service_data"]["all"] is True

    async def test_transport_failure_is_structured(self, tools, mock_client):
        mock_client.send_websocket_message = AsyncMock(
            side_effect=Exception("Service failed")
        )

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_group(object_id="test_group")

        assert json.loads(str(exc_info.value))["success"] is False


class TestRemoveGroup:
    async def test_remove_existing_group(self, tools, mock_client, states):
        states["group.test_group"] = _group_state(
            "test_group", auto=True, members=["light.a"]
        )

        result = await tools.ha_config_remove_group(object_id="test_group")

        assert result["success"] is True
        assert result["entity_id"] == "group.test_group"
        # The real removal check ran clean: a bug in it would only show up as
        # a warning, since any failed check after a delete is one.
        assert "warnings" not in result
        assert _sent_call(mock_client) == {
            "type": "call_service",
            "domain": "group",
            "service": "remove",
            "service_data": {"object_id": "test_group"},
        }

    async def test_missing_group_is_not_reported_as_removed(self, tools, mock_client):
        # Core's group.remove only logs a warning for a missing group.
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_remove_group(object_id="no_such_group")

        error = json.loads(str(exc_info.value))["error"]
        assert error["code"] == "ENTITY_NOT_FOUND"
        mock_client.send_websocket_message.assert_not_called()

    async def test_yaml_group_removal_warns_it_returns(self, tools, states):
        states["group.from_yaml"] = _group_state(
            "from_yaml", auto=False, members=["light.a"]
        )

        result = await tools.ha_config_remove_group(object_id="from_yaml")

        assert any("YAML" in w for w in result["warnings"])

    async def test_core_rejection_is_passed_through(self, tools, mock_client, states):
        states["group.lights"] = _group_state("lights", auto=True, members=[])
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": False,
                "error": "Command failed: Service group.remove not found.",
                "error_code": "not_found",
            }
        )

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_remove_group(object_id="lights")

        error = json.loads(str(exc_info.value))["error"]
        assert error["code"] == "RESOURCE_NOT_FOUND"
        assert "Service group.remove not found." in error["message"]


class TestGroupBackupRestore:
    async def test_restore_clears_members_added_after_the_snapshot(self):
        # Core's cv.entity_ids accepts [], and group.set with entities=[]
        # empties an existing group; skipping the key would keep members a
        # later edit added.
        client = MagicMock()
        client._request = AsyncMock(return_value=[])

        await _restore_group(
            client,
            "group.lights",
            {"object_id": "lights", "name": "Lights", "entities": [], "icon": None},
        )

        client._request.assert_awaited_once_with(
            "POST",
            "services/group/set",
            json={"object_id": "lights", "name": "Lights", "entities": []},
        )
