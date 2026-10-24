"""
Unit tests for Group management tools.

Core owns group.set / group.remove validation; these tests pin that the tools
pass requests to Core over WebSocket, report Core's own rejection, and read
create/update and existence from the group's state.
"""

import json
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.backup_manager import _restore_group
from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantCommandNotSent,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.tools_groups import GroupTools

# Core's WebSocket ``call_service`` reply for a payload its schema rejects, as
# ``send_websocket_message`` returns it (probatio renders the path as "at").
_CORE_SLUG_REJECTION = {
    "success": False,
    "error": (
        "Command failed: invalid slug group.lights (try group_lights) at 'object_id'"
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
        "last_changed": "2026-10-23T10:00:00+00:00",
        "last_updated": "2026-10-23T10:00:00+00:00",
        "context": {"id": "ctx", "parent_id": None, "user_id": None},
    }


def _error(exc_info: pytest.ExceptionInfo[ToolError]) -> dict[str, Any]:
    error: dict[str, Any] = json.loads(str(exc_info.value))["error"]
    return error


@pytest.fixture
def states() -> dict[str, dict[str, Any]]:
    return {}


@pytest.fixture
def mock_client(states):
    """Client whose group services create and remove ``states`` the way Core does."""
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
    return client


@pytest.fixture
def tools(mock_client):
    return GroupTools(mock_client)


def _sent_call(mock_client) -> dict[str, Any]:
    return mock_client.send_websocket_message.call_args[0][0]


class TestListGroups:
    async def test_list_keeps_the_fields_core_sends(self, tools, states):
        states["group.lights"] = _group_state("lights", auto=True, members=["light.a"])
        states["group.lights"]["attributes"]["assumed_state"] = True
        states["light.bed_light"] = {
            "entity_id": "light.bed_light",
            "state": "off",
            "attributes": {"friendly_name": "Bed Light"},
        }

        result = await tools.ha_config_list_groups()

        assert result["count"] == 1
        group = result["groups"][0]
        assert group["entity_id"] == "group.lights"
        assert group["object_id"] == "lights"
        assert group["entity_ids"] == ["light.a"]
        assert group["assumed_state"] is True
        assert group["last_changed"] == "2026-10-23T10:00:00+00:00"
        # Core leaves an unset icon out; the entry still carries the key.
        assert group["icon"] is None

    async def test_list_reports_a_yaml_group_as_not_auto(self, tools, states):
        states["group.from_yaml"] = _group_state(
            "from_yaml", auto=False, members=["light.b"]
        )
        del states["group.from_yaml"]["attributes"]["friendly_name"]

        group = (await tools.ha_config_list_groups())["groups"][0]

        assert group["auto"] is False
        assert group["friendly_name"] is None

    async def test_list_groups_error_handling(self, tools, mock_client):
        mock_client.get_states = AsyncMock(side_effect=Exception("Connection failed"))

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_list_groups()

        assert json.loads(str(exc_info.value))["success"] is False


class TestSetGroup:
    async def test_set_is_sent_as_core_call_service_over_websocket(
        self, tools, mock_client
    ):
        await tools.ha_config_set_group(
            object_id="lights", entities=["light.a"], all_on=True
        )

        assert _sent_call(mock_client) == {
            "type": "call_service",
            "domain": "group",
            "service": "set",
            "service_data": {
                "object_id": "lights",
                "all": True,
                "entities": ["light.a"],
            },
        }

    async def test_create_with_name_is_reported_as_created(self, tools):
        # The docstring's create example passes name=; a create must still
        # read as created.
        result = await tools.ha_config_set_group(
            object_id="bedroom_lights",
            entities=["light.lamp", "light.ceiling"],
            name="Bedroom Lights",
        )

        assert result["message"] == "Successfully created group: group.bedroom_lights"
        assert "warnings" not in result

    async def test_entities_on_existing_group_is_reported_as_update(
        self, tools, states
    ):
        states["group.lights"] = _group_state("lights", auto=True, members=["light.a"])

        result = await tools.ha_config_set_group(
            object_id="lights", entities=["light.b"]
        )

        assert result["message"] == "Successfully updated group: group.lights"
        assert "warnings" not in result

    @pytest.mark.parametrize(
        "arguments",
        [
            {},
            {"entities": []},
            {"add_entities": []},
            {"entities": ["light.a"], "add_entities": ["light.b"]},
            {"object_id": "group.lights"},
        ],
        ids=[
            "no-entities",
            "empty-entities",
            "empty-add",
            "exclusive-lists",
            "dotted-object-id",
        ],
    )
    async def test_arguments_are_left_for_core_to_judge(
        self, tools, mock_client, arguments
    ):
        arguments = {"object_id": "lights", **arguments}

        await tools.ha_config_set_group(**arguments, wait=False)

        assert _sent_call(mock_client)["service_data"] == arguments

    async def test_core_schema_rejection_is_passed_through(self, tools, mock_client):
        mock_client.send_websocket_message = AsyncMock(
            return_value=_CORE_SLUG_REJECTION
        )

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_group(object_id="group.lights")

        error = _error(exc_info)
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "invalid slug group.lights (try group_lights)" in error["message"]
        assert "Command failed" not in error["message"]
        # Core's "try group_lights" names a different group.
        assert "Drop the 'group.' prefix: object_id='lights'" in error["suggestion"]

    async def test_rejection_of_another_field_carries_no_prefix_hint(
        self, tools, mock_client
    ):
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": False,
                "error": (
                    "Command failed: two or more values in the same group of "
                    "exclusion 'entities' at '<entities>'"
                ),
                "error_code": "invalid_format",
            }
        )

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_group(
                object_id="lights", entities=["light.a"], add_entities=["light.b"]
            )

        error = _error(exc_info)
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "prefix" not in error.get("suggestion", "")

    async def test_yaml_group_update_warns_reload_reverts_it(self, tools, states):
        states["group.from_yaml"] = _group_state(
            "from_yaml", auto=False, members=["light.a"]
        )

        result = await tools.ha_config_set_group(
            object_id="from_yaml", add_entities=["light.b"]
        )

        assert result["message"] == "Successfully updated group: group.from_yaml"
        assert any("YAML" in w for w in result["warnings"])

    async def test_remove_entities_on_unknown_group_warns_it_was_created_empty(
        self, tools
    ):
        result = await tools.ha_config_set_group(
            object_id="typo_lights", remove_entities=["light.a"]
        )

        assert result["message"] == "Successfully created group: group.typo_lights"
        assert any("without members" in w for w in result["warnings"])

    async def test_remove_entities_on_existing_group_is_an_update_without_warning(
        self, tools, mock_client, states
    ):
        states["group.lights"] = _group_state(
            "lights", auto=True, members=["light.a", "light.b"]
        )

        result = await tools.ha_config_set_group(
            object_id="lights", remove_entities=["light.a"]
        )

        assert result["message"] == "Successfully updated group: group.lights"
        assert "warnings" not in result
        assert _sent_call(mock_client)["service_data"]["remove_entities"] == ["light.a"]


class TestServiceFailures:
    @pytest.mark.parametrize("tool_name", ["set", "remove"])
    async def test_state_read_failure_aborts_before_the_write(
        self, tools, mock_client, tool_name
    ):
        # A 500 must not read as "no such group": set would report a create
        # and remove a missing group.
        mock_client.get_entity_state = AsyncMock(
            side_effect=HomeAssistantAPIError("Server error", status_code=500)
        )
        tool = getattr(tools, f"ha_config_{tool_name}_group")

        with pytest.raises(ToolError) as exc_info:
            await tool(object_id="lights")

        assert _error(exc_info)["code"] == "SERVICE_CALL_FAILED"
        mock_client.send_websocket_message.assert_not_called()

    async def test_lost_reply_says_the_write_may_have_landed(self, tools, mock_client):
        mock_client.send_websocket_message = AsyncMock(
            side_effect=HomeAssistantConnectionError("connection closed")
        )

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_group(object_id="lights")

        payload = json.loads(str(exc_info.value))
        assert payload["error"]["code"] == "SERVICE_CALL_FAILED"
        assert payload["write_outcome_unknown"] is True
        assert "ha_config_list_groups" in payload["error"]["suggestion"]

    async def test_unsent_command_is_not_reported_as_possibly_applied(
        self, tools, mock_client
    ):
        mock_client.send_websocket_message = AsyncMock(
            side_effect=HomeAssistantCommandNotSent("not authenticated")
        )

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_group(object_id="lights")

        payload = json.loads(str(exc_info.value))
        assert payload["error"]["code"] == "CONNECTION_FAILED"
        assert "write_outcome_unknown" not in payload

    async def test_non_schema_error_carries_no_prefix_hint(self, tools, mock_client):
        # The prefix hint answers Core's slug rejection, not any failure of a
        # call whose object_id happens to start with "group.".
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": False,
                "error": "Command failed: Something broke",
                "error_code": "home_assistant_error",
            }
        )

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_group(object_id="group.lights")

        error = _error(exc_info)
        assert error["code"] == "SERVICE_CALL_FAILED"
        assert "prefix" not in error.get("suggestion", "")

    async def test_missing_group_service_is_not_reported_as_missing_group(
        self, tools, mock_client, states
    ):
        # Core answers not_found when the group integration is not loaded.
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

        error = _error(exc_info)
        assert error["code"] == "SERVICE_NOT_FOUND"
        assert "Service group.remove not found." in error["message"]
        assert "Command failed" not in error["message"]


class TestRemoveGroup:
    async def test_remove_existing_group(self, tools, mock_client, states):
        states["group.test_group"] = _group_state(
            "test_group", auto=True, members=["light.a"]
        )

        result = await tools.ha_config_remove_group(object_id="test_group")

        assert result["success"] is True
        assert result["entity_id"] == "group.test_group"
        # verify_entity_removed runs unpatched and reports any failure as a
        # warning, so no warnings means the removal was confirmed.
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

        error = _error(exc_info)
        assert error["code"] == "ENTITY_NOT_FOUND"
        assert "without the 'group.' prefix" in error["suggestion"]
        mock_client.send_websocket_message.assert_not_called()

    async def test_yaml_group_removal_warns_it_returns(self, tools, states):
        states["group.from_yaml"] = _group_state(
            "from_yaml", auto=False, members=["light.a"]
        )

        result = await tools.ha_config_remove_group(object_id="from_yaml")

        assert any("YAML" in w for w in result["warnings"])


class TestGroupBackupRestore:
    async def test_restore_clears_members_added_after_the_snapshot(self):
        # Core's cv.entity_ids accepts [], and group.set with entities=[]
        # empties an existing group; skipping the key would keep members a
        # later edit added.
        with patch("ha_mcp.backup_manager._ws_send", new_callable=AsyncMock) as ws_send:
            await _restore_group(
                MagicMock(),
                "group.lights",
                {"object_id": "lights", "name": "Lights", "entities": [], "icon": None},
            )

        ws_send.assert_awaited_once_with(
            ANY,
            {
                "type": "call_service",
                "domain": "group",
                "service": "set",
                "service_data": {
                    "object_id": "lights",
                    "name": "Lights",
                    "entities": [],
                },
            },
        )
