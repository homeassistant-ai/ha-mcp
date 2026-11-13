"""Create, update and duplicate names in ``ha_config_set_helper`` (#1150).

Bug 11: passing ``name`` and ``helper_id`` together is the rename pattern; it
must update the helper, not be refused or turned into a create.

Bug 12: Core's ``<type>/create`` numbers a duplicate name (``_2``), so a
create whose name an existing helper already has is refused before anything
is sent, pointing at the existing helper's id.

The tool runs on the server's own client against a real ``hass`` without the
component, so the duplicate check reads Core's helper lists and config entries.
The component's check is covered in ``test_storage_helper_native_real_core.py``.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.config_helpers import flow
from ha_mcp.tools.tools_config_helpers import HelperConfigTools

from ._client_spies import sent_messages
from ._tool_errors import error_of


@pytest.fixture
async def tools(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> HelperConfigTools:
    # ``config`` serves the registry and config-entry commands.
    for domain in ("config", "input_boolean"):
        assert await async_setup_component(hass, domain, {})
    return HelperConfigTools(core_client)


def _name(base: str) -> str:
    """A fresh name: Core restores a removed helper's registry entry by id."""
    return f"{base} {uuid.uuid4().hex[:8]}"


async def _stored(client: HomeAssistantClient) -> dict[str, str]:
    """Core's stored input_booleans, id to name."""
    listed = await client.send_websocket_message({"type": "input_boolean/list"})
    assert listed["success"], listed
    return {item["id"]: item["name"] for item in listed["result"]}


async def test_name_with_helper_id_renames_the_helper(
    hass: HomeAssistant, core_client: HomeAssistantClient, tools: HelperConfigTools
) -> None:
    """Bug 11: the rename pattern updates the helper it names."""
    created = await tools.ha_config_set_helper(
        helper_type="input_boolean", name=_name("Old Name"), wait=False
    )
    (helper_id,) = await _stored(core_client)
    new_name = _name("New Name")

    result = await tools.ha_config_set_helper(
        helper_type="input_boolean", name=new_name, helper_id=helper_id, wait=False
    )

    assert result["success"] is True
    assert result["action"] == "update"
    assert await _stored(core_client) == {helper_id: new_name}
    assert hass.states.async_entity_ids("input_boolean") == [created["entity_id"]]


async def test_create_with_an_existing_name_points_at_that_helper(
    hass: HomeAssistant, core_client: HomeAssistantClient, tools: HelperConfigTools
) -> None:
    """Bug 12: Core would create a numbered duplicate instead."""
    name = _name("Kitchen Light")
    await tools.ha_config_set_helper(helper_type="input_boolean", name=name, wait=False)
    (existing_id,) = await _stored(core_client)

    with sent_messages(core_client) as sent, pytest.raises(ToolError) as exc_info:
        await tools.ha_config_set_helper(
            helper_type="input_boolean", name=name, wait=False
        )

    error = error_of(exc_info)
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "input_boolean/create" not in [
        call.args[0]["type"] for call in sent.await_args_list
    ]
    # The suggestion steers the caller to an update of the existing helper.
    assert any(f"helper_id={existing_id!r}" in s for s in error["suggestions"])
    assert len(hass.states.async_entity_ids("input_boolean")) == 1


async def test_create_with_a_new_name_beside_another_helper_is_not_refused(
    hass: HomeAssistant, core_client: HomeAssistantClient, tools: HelperConfigTools
) -> None:
    await tools.ha_config_set_helper(
        helper_type="input_boolean", name=_name("Kitchen Light"), wait=False
    )

    result = await tools.ha_config_set_helper(
        helper_type="input_boolean", name=_name("Bedroom Light"), wait=False
    )

    assert result["action"] == "create"
    assert len(hass.states.async_entity_ids("input_boolean")) == 2


async def test_empty_name_is_refused_as_missing_not_as_a_duplicate(
    hass: HomeAssistant, tools: HelperConfigTools
) -> None:
    await tools.ha_config_set_helper(
        helper_type="input_boolean", name=_name("Kitchen Light"), wait=False
    )

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_config_set_helper(helper_type="input_boolean", name="")

    message = str(exc_info.value)
    assert "VALIDATION_INVALID_PARAMETER" in message
    assert "already exists" not in message
    assert len(hass.states.async_entity_ids("input_boolean")) == 1


async def test_flow_helper_with_an_existing_title_is_refused_before_a_flow_opens(
    hass: HomeAssistant, tools: HelperConfigTools
) -> None:
    title = _name("Room Temp")
    template = {"helper_type": "template", "menu_choice": "sensor", "wait": False}
    await tools.ha_config_set_helper(
        **template, config={"name": title, "state": "{{ 1 }}"}
    )
    (entry,) = hass.config_entries.async_entries("template")

    with (
        patch.object(flow, "advance_flow", wraps=flow.advance_flow) as advance,
        pytest.raises(ToolError) as exc_info,
    ):
        await tools.ha_config_set_helper(
            **template, config={"name": title, "state": "{{ 2 }}"}
        )

    error = error_of(exc_info)
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert entry.entry_id in error["message"]
    advance.assert_not_awaited()
    assert hass.config_entries.flow.async_progress() == []
    assert len(hass.config_entries.async_entries("template")) == 1


async def test_flow_helper_with_a_new_title_is_created(
    hass: HomeAssistant, tools: HelperConfigTools
) -> None:
    template = {"helper_type": "template", "menu_choice": "sensor", "wait": False}
    await tools.ha_config_set_helper(
        **template, config={"name": _name("Other Sensor"), "state": "{{ 1 }}"}
    )
    title = _name("Brand New Sensor")

    result = await tools.ha_config_set_helper(
        **template, config={"name": title, "state": "{{ 2 }}"}
    )

    assert result["success"] is True
    assert result["action"] == "create"
    (entry,) = [
        e for e in hass.config_entries.async_entries("template") if e.title == title
    ]
    assert result["entry_id"] == entry.entry_id
