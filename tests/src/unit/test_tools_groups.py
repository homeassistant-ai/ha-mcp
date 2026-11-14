"""The group tools on real Home Assistant Core.

Core owns group.set / group.remove validation: these tests pin that the tools
pass requests to Core's ``group`` integration over WebSocket, report Core's own
rejection, and read create/update and existence from the group's state. Every
state, rejection and reply comes from Core.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant, State
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.backup_manager import get_backup_manager
from ha_mcp.client.rest_client import (
    HomeAssistantAuthError,
    HomeAssistantClient,
    HomeAssistantCommandNotSent,
    HomeAssistantConnectionError,
)
from ha_mcp.config import get_global_settings
from ha_mcp.tools.tools_groups import GroupTools

from ._client_spies import sent_messages
from ._tool_errors import error_of

YAML_GROUP = {"group": {"from_yaml": {"entities": ["light.a"]}}}


@pytest.fixture
async def tools(core_client: HomeAssistantClient) -> GroupTools:
    return GroupTools(core_client)


async def _setup_groups(
    hass: HomeAssistant, config: dict[str, Any] | None = None
) -> None:
    """Core's group integration, with the YAML groups in ``config``."""
    hass.states.async_set("light.a", "on")
    hass.states.async_set("light.b", "off")
    assert await async_setup_component(hass, "group", config or {})
    await hass.async_block_till_done()


async def _group_set(hass: HomeAssistant, object_id: str, **data: Any) -> None:
    """Create a group the way group.set does for any caller."""
    await hass.services.async_call(
        "group", "set", {"object_id": object_id, **data}, blocking=True
    )
    await hass.async_block_till_done()


def _members(hass: HomeAssistant, entity_id: str) -> list[str]:
    state = hass.states.get(entity_id)
    assert state is not None
    return list(state.attributes["entity_id"])


@contextmanager
def _state_read_fails(entity_id: str) -> Iterator[None]:
    """Core fails serializing ``entity_id``'s state, so its REST API answers 500."""
    original = State.__dict__["as_dict_json"]

    def as_dict_json(state: State) -> bytes:
        if state.entity_id == entity_id:
            raise RuntimeError("state serialization failed")
        serialized: bytes = original.__get__(state, State)
        return serialized

    with patch.object(State, "as_dict_json", property(as_dict_json)):
        yield


class TestListGroups:
    async def test_list_keeps_the_fields_core_sends(
        self, hass: HomeAssistant, tools: GroupTools
    ) -> None:
        await _setup_groups(hass)
        await _group_set(hass, "lights", entities=["light.a"])
        core_state = hass.states.get("group.lights")
        assert core_state is not None

        result = await tools.ha_config_list_groups()

        # light.a and light.b are states, not groups.
        assert result["count"] == 1
        group = result["groups"][0]
        assert group["entity_id"] == "group.lights"
        assert group["object_id"] == "lights"
        assert group["entity_ids"] == ["light.a"]
        assert group["auto"] is True
        assert group["order"] == core_state.attributes["order"]
        assert group["last_changed"] == core_state.last_changed.isoformat()
        # Core leaves an unset icon out; the entry still carries the key.
        assert group["icon"] is None

    async def test_list_reports_a_yaml_group_as_not_auto(
        self, hass: HomeAssistant, tools: GroupTools
    ) -> None:
        await _setup_groups(hass, YAML_GROUP)

        group = (await tools.ha_config_list_groups())["groups"][0]

        assert group["entity_id"] == "group.from_yaml"
        assert group["auto"] is False

    async def test_list_without_an_answer_is_a_tool_error(
        self,
        core_client: HomeAssistantClient,
        tools: GroupTools,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            core_client,
            "get_states",
            AsyncMock(side_effect=HomeAssistantConnectionError("Connection failed")),
        )

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_list_groups()

        assert json.loads(str(exc_info.value))["success"] is False


class TestSetGroup:
    @pytest.mark.parametrize(("all_on", "expected"), [(True, "off"), (False, "on")])
    async def test_all_mode_reaches_core(
        self, hass: HomeAssistant, tools: GroupTools, all_on: bool, expected: str
    ) -> None:
        """light.a is on and light.b off: only an all-mode group reads off."""
        await _setup_groups(hass)

        await tools.ha_config_set_group(
            object_id="lights", entities=["light.a", "light.b"], all_on=all_on
        )
        await hass.async_block_till_done()

        state = hass.states.get("group.lights")
        assert state is not None
        assert state.state == expected
        assert list(state.attributes["entity_id"]) == ["light.a", "light.b"]

    async def test_create_with_name_is_reported_as_created(
        self, hass: HomeAssistant, tools: GroupTools
    ) -> None:
        # The docstring's create example passes name=; a create must still
        # read as created.
        await _setup_groups(hass)

        result = await tools.ha_config_set_group(
            object_id="bedroom_lights",
            entities=["light.a", "light.b"],
            name="Bedroom Lights",
        )

        assert result["message"] == "Successfully created group: group.bedroom_lights"
        assert "warnings" not in result
        state = hass.states.get("group.bedroom_lights")
        assert state is not None
        assert state.name == "Bedroom Lights"

    async def test_entities_on_existing_group_is_reported_as_update(
        self, hass: HomeAssistant, tools: GroupTools
    ) -> None:
        await _setup_groups(hass)
        await _group_set(hass, "lights", entities=["light.a"])

        result = await tools.ha_config_set_group(
            object_id="lights", entities=["light.b"]
        )

        assert result["message"] == "Successfully updated group: group.lights"
        assert "warnings" not in result
        assert _members(hass, "group.lights") == ["light.b"]

    @pytest.mark.parametrize(
        "arguments",
        [{}, {"entities": []}, {"add_entities": []}],
        ids=["no-entities", "empty-entities", "empty-add"],
    )
    async def test_arguments_are_left_for_core_to_judge(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        tools: GroupTools,
        arguments: dict[str, Any],
    ) -> None:
        """Core accepts these and creates an empty group; the tool refuses none.

        The arguments Core rejects are the two rejection tests below.
        """
        await _setup_groups(hass)
        arguments = {"object_id": "lights", **arguments}

        with sent_messages(core_client) as sent:
            result = await tools.ha_config_set_group(**arguments, wait=False)
        await hass.async_block_till_done()

        assert result["success"] is True
        assert sent.await_args is not None
        assert sent.await_args.args[0]["service_data"] == arguments
        assert _members(hass, "group.lights") == []

    async def test_core_schema_rejection_is_passed_through(
        self, hass: HomeAssistant, tools: GroupTools
    ) -> None:
        await _setup_groups(hass)

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_group(object_id="group.lights")

        error = error_of(exc_info)
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "invalid slug group.lights (try group_lights)" in error["message"]
        assert "Command failed" not in error["message"]
        assert json.loads(str(exc_info.value))["ha_error_code"] == "invalid_format"
        # Core's "try group_lights" names a different group.
        assert "Drop the 'group.' prefix: object_id='lights'" in error["suggestion"]
        assert hass.states.get("group.group_lights") is None

    async def test_rejection_of_another_field_carries_no_prefix_hint(
        self, hass: HomeAssistant, tools: GroupTools
    ) -> None:
        await _setup_groups(hass)

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_group(
                object_id="lights", entities=["light.a"], add_entities=["light.b"]
            )

        error = error_of(exc_info)
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "exclusion" in error["message"]
        assert "prefix" not in error.get("suggestion", "")
        assert hass.states.get("group.lights") is None

    async def test_yaml_group_update_warns_reload_reverts_it(
        self, hass: HomeAssistant, tools: GroupTools
    ) -> None:
        await _setup_groups(hass, YAML_GROUP)

        result = await tools.ha_config_set_group(
            object_id="from_yaml", add_entities=["light.b"]
        )

        assert result["message"] == "Successfully updated group: group.from_yaml"
        assert any("YAML" in w for w in result["warnings"])
        # Core merges add_entities through a set, so the order varies.
        assert sorted(_members(hass, "group.from_yaml")) == ["light.a", "light.b"]

    async def test_remove_entities_on_unknown_group_warns_it_was_created_empty(
        self, hass: HomeAssistant, tools: GroupTools
    ) -> None:
        await _setup_groups(hass)

        result = await tools.ha_config_set_group(
            object_id="typo_lights", remove_entities=["light.a"]
        )

        assert result["message"] == "Successfully created group: group.typo_lights"
        assert any("without members" in w for w in result["warnings"])
        assert _members(hass, "group.typo_lights") == []

    async def test_remove_entities_on_existing_group_is_an_update_without_warning(
        self, hass: HomeAssistant, tools: GroupTools
    ) -> None:
        await _setup_groups(hass)
        await _group_set(hass, "lights", entities=["light.a", "light.b"])

        result = await tools.ha_config_set_group(
            object_id="lights", remove_entities=["light.a"]
        )

        assert result["message"] == "Successfully updated group: group.lights"
        assert "warnings" not in result
        assert _members(hass, "group.lights") == ["light.b"]


class TestServiceFailures:
    @pytest.mark.parametrize("tool_name", ["set", "remove"])
    async def test_state_read_failure_aborts_before_the_write(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        tools: GroupTools,
        tool_name: str,
    ) -> None:
        # A 500 must not read as "no such group": set would report a create
        # and remove a missing group.
        await _setup_groups(hass)
        await _group_set(hass, "lights", entities=["light.a"])
        tool = getattr(tools, f"ha_config_{tool_name}_group")

        with (
            _state_read_fails("group.lights"),
            sent_messages(core_client) as sent,
            pytest.raises(ToolError) as exc_info,
        ):
            await tool(object_id="lights")

        assert error_of(exc_info)["code"] == "SERVICE_CALL_FAILED"
        sent.assert_not_called()
        assert _members(hass, "group.lights") == ["light.a"]

    async def test_lost_reply_says_the_write_may_have_landed(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        tools: GroupTools,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await _setup_groups(hass)
        monkeypatch.setattr(
            core_client,
            "send_websocket_message",
            AsyncMock(side_effect=HomeAssistantConnectionError("connection closed")),
        )

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_group(object_id="lights")

        payload = json.loads(str(exc_info.value))
        assert payload["error"]["code"] == "SERVICE_CALL_FAILED"
        assert payload["write_outcome_unknown"] is True
        assert "ha_config_list_groups" in payload["error"]["suggestion"]

    @pytest.mark.parametrize(
        ("cause", "code"),
        [
            (None, "CONNECTION_FAILED"),
            (HomeAssistantAuthError("authentication failed"), "AUTH_INVALID_TOKEN"),
        ],
        ids=["not_authenticated", "token_rejected_on_connect"],
    )
    async def test_unsent_command_is_not_reported_as_possibly_applied(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        tools: GroupTools,
        cause: BaseException | None,
        code: str,
    ) -> None:
        await _setup_groups(hass)

        with (
            patch.object(
                core_client,
                "send_websocket_message",
                AsyncMock(side_effect=HomeAssistantCommandNotSent("not authenticated")),
            ) as unsent,
            pytest.raises(ToolError) as exc_info,
        ):
            unsent.side_effect.__cause__ = cause
            await tools.ha_config_set_group(object_id="lights")

        payload = json.loads(str(exc_info.value))
        assert payload["error"]["code"] == code
        assert "write_outcome_unknown" not in payload

    async def test_non_schema_error_carries_no_prefix_hint(
        self, tools: GroupTools
    ) -> None:
        # The prefix hint answers Core's slug rejection, not any failure of a
        # call whose object_id happens to start with "group.": without the
        # group integration Core refuses the unknown action before its schema.
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_group(object_id="group.lights")

        error = error_of(exc_info)
        assert error["code"] == "SERVICE_NOT_FOUND"
        assert "prefix" not in error.get("suggestion", "")

    async def test_missing_group_service_is_not_reported_as_missing_group(
        self, hass: HomeAssistant, tools: GroupTools
    ) -> None:
        # Core answers not_found when the group integration is not loaded.
        hass.states.async_set("group.lights", "off", {"entity_id": []})

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_remove_group(object_id="lights")

        error = error_of(exc_info)
        assert error["code"] == "SERVICE_NOT_FOUND"
        assert "group.remove not found" in error["message"]
        assert "Command failed" not in error["message"]


class TestRemoveGroup:
    async def test_remove_existing_group(
        self, hass: HomeAssistant, tools: GroupTools
    ) -> None:
        await _setup_groups(hass)
        await _group_set(hass, "test_group", entities=["light.a"])

        result = await tools.ha_config_remove_group(object_id="test_group")

        assert result["success"] is True
        assert result["entity_id"] == "group.test_group"
        # verify_entity_removed reports any failure as a warning, so no
        # warnings means the removal was confirmed.
        assert "warnings" not in result
        assert hass.states.get("group.test_group") is None

    async def test_missing_group_is_not_reported_as_removed(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        tools: GroupTools,
    ) -> None:
        # Core's group.remove only logs a warning for a missing group.
        await _setup_groups(hass)

        with sent_messages(core_client) as sent, pytest.raises(ToolError) as exc_info:
            await tools.ha_config_remove_group(object_id="no_such_group")

        error = error_of(exc_info)
        assert error["code"] == "ENTITY_NOT_FOUND"
        assert "without the 'group.' prefix" in error["suggestion"]
        sent.assert_not_called()

    async def test_yaml_group_removal_warns_it_returns(
        self, hass: HomeAssistant, tools: GroupTools
    ) -> None:
        await _setup_groups(hass, YAML_GROUP)

        result = await tools.ha_config_remove_group(object_id="from_yaml")

        assert any("YAML" in w for w in result["warnings"])
        assert hass.states.get("group.from_yaml") is None


class TestGroupBackupRestore:
    async def test_restore_clears_members_added_after_the_snapshot(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        # Core's cv.entity_ids accepts [], and group.set with entities=[]
        # empties an existing group; skipping the key would keep members a
        # later edit added.
        await _setup_groups(hass)
        await _group_set(hass, "lights", entities=["light.a"])
        manager = get_backup_manager(core_client, get_global_settings())
        handler = manager.handler_for("group")
        assert handler is not None

        await handler.restore(
            core_client,
            "group.lights",
            {"object_id": "lights", "name": "Lights", "entities": [], "icon": None},
        )
        await hass.async_block_till_done()

        assert _members(hass, "group.lights") == []
        state = hass.states.get("group.lights")
        assert state is not None
        assert state.name == "Lights"
