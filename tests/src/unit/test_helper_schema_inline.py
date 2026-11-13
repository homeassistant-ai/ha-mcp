"""The context ``ha_config_set_helper`` attaches to a name-collision refusal.

A storage helper's refusal carries Core's field list for the type
(``data_schema``) when the component serves it, so an LLM can self-correct
without a separate discovery round-trip; without the component no field list
is made up. A flow helper's refusal names the existing config entry; its
fields come back with the flow step itself.

The tool runs on the server's own client against a real ``hass``.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_config_helpers import HelperConfigTools


@pytest.fixture
def name() -> str:
    """A storage id no deleted registry entry can be restored for."""
    return f"My Selector {uuid.uuid4().hex[:8]}"


async def _refused_duplicate(
    hass: HomeAssistant, client: HomeAssistantClient, **create: Any
) -> dict[str, Any]:
    """Create a helper, then create it again; return the refusal's body."""
    # ``config`` serves the registry commands.
    assert await async_setup_component(hass, "config", {})
    tools = HelperConfigTools(client)
    await tools.ha_config_set_helper(**create)
    await hass.async_block_till_done()
    with pytest.raises(ToolError) as exc_info:
        await tools.ha_config_set_helper(**create)

    body: dict[str, Any] = json.loads(str(exc_info.value))
    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    return body


async def _stored_id(client: HomeAssistantClient, name: str) -> str:
    listed = await client.send_websocket_message({"type": "input_select/list"})
    (item_id,) = [item["id"] for item in listed["result"] if item["name"] == name]
    assert isinstance(item_id, str)
    return item_id


async def test_a_storage_collision_carries_cores_field_list(
    hass: HomeAssistant, component_core_client: HomeAssistantClient, name: str
) -> None:
    assert await async_setup_component(hass, "input_select", {})

    body = await _refused_duplicate(
        hass,
        component_core_client,
        helper_type="input_select",
        name=name,
        config={"options": ["a", "b"]},
    )

    schemas = await component_core_client.send_websocket_message(
        {"type": "ha_mcp_tools/helper_schemas"}
    )
    cores_fields = schemas["result"]["types"]["input_select"]["create"]
    assert cores_fields
    assert body["data_schema"] == cores_fields
    assert body["helper_type"] == "input_select"
    assert body["name"] == name
    assert body["existing_helper_id"] == await _stored_id(component_core_client, name)


async def test_without_the_component_no_field_list_is_made_up(
    hass: HomeAssistant, core_client: HomeAssistantClient, name: str
) -> None:
    assert await async_setup_component(hass, "input_select", {})

    body = await _refused_duplicate(
        hass,
        core_client,
        helper_type="input_select",
        name=name,
        config={"options": ["a", "b"]},
    )

    assert "data_schema" not in body
    assert body["helper_type"] == "input_select"
    assert body["existing_helper_id"] == await _stored_id(core_client, name)


@pytest.mark.parametrize("with_component", [True, False])
async def test_a_flow_collision_names_the_existing_config_entry(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    with_component: bool,
) -> None:
    """The component finds the entry with Core's slugify; without it the
    tool reads Core's config entries for one with the same title."""
    if with_component:
        from custom_components.ha_mcp_tools import websocket_api

        websocket_api.async_register_commands(hass)
    title = f"My Template {uuid.uuid4().hex[:8]}"

    body = await _refused_duplicate(
        hass,
        core_client,
        helper_type="template",
        menu_choice="sensor",
        config={"name": title, "state": "{{ 1 }}"},
    )

    (entry,) = hass.config_entries.async_entries("template")
    assert entry.title == title
    assert body["existing_helper_id"] == entry.entry_id
    assert body["helper_type"] == "template"
    assert body["name"] == title
    assert "data_schema" not in body
