"""ha_config_set_helper on real Home Assistant Core: a storage helper's ``config``
sent to Core as written, Core-sourced schemas, component-routed writes, and the
component-aware catalog."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import category_registry as cr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp import Client, FastMCP
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.component_helper_collections import (
    collection_payload as client_payload,
)
from ha_mcp.tools.config_helpers import schemas as hc_schemas
from ha_mcp.tools.config_helpers import update as hc_update
from ha_mcp.tools.tools_config_helpers import (
    HelperConfigTools,
    register_config_helper_tools,
)

from ._client_spies import sent_messages

pytestmark = pytest.mark.asyncio


@pytest.fixture(params=[False, True], ids=["core", "component"])
async def tools(
    request: pytest.FixtureRequest,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
) -> AsyncIterator[HelperConfigTools]:
    if request.param:
        from custom_components.ha_mcp_tools import websocket_api

        websocket_api.async_register_commands(hass)
    yield HelperConfigTools(core_client)
    await hass.async_block_till_done()


async def _set_up(hass: HomeAssistant, helper_type: str) -> None:
    # ``config`` serves the entity-registry reads of the tool.
    for domain in ("config", helper_type):
        assert await async_setup_component(hass, domain, {})


def _name() -> str:
    """A fresh name, so no registry entry of an earlier helper is restored."""
    return f"Helper {uuid.uuid4().hex[:8]}"


async def _stored(client: HomeAssistantClient, helper_type: str, item_id: str) -> Any:
    listed = await client.send_websocket_message({"type": f"{helper_type}/list"})
    assert listed["success"], listed
    return next(i for i in listed["result"] if i["id"] == item_id)


async def _create(
    client: HomeAssistantClient, helper_type: str, fields: dict[str, Any]
) -> str:
    created = await client.send_websocket_message(
        {"type": f"{helper_type}/create", "name": _name(), **fields}
    )
    assert created["success"], created
    return str(created["result"]["id"])


async def _create_tag(client: HomeAssistantClient, **fields: Any) -> None:
    created = await client.send_websocket_message(
        {"type": "tag/create", "tag_id": "abc-1", **fields}
    )
    assert created["success"], created


async def _registered_tool(client: HomeAssistantClient) -> tuple[FastMCP, Any]:
    mcp = FastMCP("t")
    register_config_helper_tools(mcp, client)
    return mcp, await mcp.get_tool("ha_config_set_helper")


async def _call(mcp: FastMCP, **arguments: Any) -> Any:
    async with Client(mcp) as client:
        return await client.call_tool("ha_config_set_helper", arguments)


async def test_a_storage_field_is_not_a_tool_parameter(
    core_client: HomeAssistantClient,
) -> None:
    """A helper's own fields are Core's to name: they go in ``config``, and
    the tool declares none of them."""
    _, tool = await _registered_tool(core_client)
    properties = tool.parameters["properties"]
    assert {"helper_type", "name", "icon", "config", "wait"} <= properties.keys()
    assert not {"min", "min_value", "options", "has_date"} & properties.keys()
    mcp, _ = await _registered_tool(core_client)
    with pytest.raises(ToolError, match="min_value"):
        await _call(mcp, helper_type="input_number", name="T", min_value=1)


async def test_config_accepts_core_names_and_name(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await _set_up(hass, "counter")
    name = _name()
    result = await HelperConfigTools(core_client).ha_config_set_helper(
        helper_type="counter",
        action="create",
        config={"name": name, "minimum": 0, "maximum": "5", "icon": "mdi:run"},
    )
    # As written: Core coerces the string, not the tool.
    stored = await _stored(core_client, "counter", result["data"]["id"])
    assert stored["minimum"] == 0
    assert stored["maximum"] == 5
    assert stored["name"] == name
    state = hass.states.get(result["entity_id"])
    assert state is not None
    assert state.attributes["icon"] == "mdi:run"


async def test_core_value_types_pass_through(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Core takes seconds for a timer and a fractional number initial."""
    for helper_type in ("timer", "input_number", "input_boolean"):
        await _set_up(hass, helper_type)
    tools = HelperConfigTools(core_client)
    timer = await tools.ha_config_set_helper(
        helper_type="timer", name=_name(), action="create", config={"duration": 300}
    )
    number = await tools.ha_config_set_helper(
        helper_type="input_number",
        name=_name(),
        action="create",
        config={"initial": 2.5, "min": 0, "max": 5},
    )
    boolean = await tools.ha_config_set_helper(
        helper_type="input_boolean",
        name=_name(),
        action="create",
        config={"initial": True},
    )
    stored_timer = await _stored(core_client, "timer", timer["data"]["id"])
    assert stored_timer["duration"] == "0:05:00"
    stored_number = await _stored(core_client, "input_number", number["data"]["id"])
    assert stored_number["initial"] == 2.5
    stored_boolean = await _stored(core_client, "input_boolean", boolean["data"]["id"])
    assert stored_boolean["initial"] is True


async def test_unknown_config_keys_reach_core_for_its_suggestion(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Core rejects a key its schema lacks and names the closest one, so the
    tool sends it rather than replacing that with its own generic error."""
    await _set_up(hass, "input_number")
    mcp, _ = await _registered_tool(core_client)
    with pytest.raises(ToolError, match="minn"):
        await _call(mcp, helper_type="input_number", name="T", config={"minn": 1})


@pytest.mark.parametrize(
    ("helper_type", "key"),
    [("input_boolean", "type"), ("input_number", "input_number_id"), ("tag", "id")],
)
async def test_config_cannot_choose_the_command_or_the_item(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    helper_type: str,
    key: str,
) -> None:
    """``type`` and the item ids address the WebSocket request; a config key
    with that name would send another command or write another item."""
    await _set_up(hass, helper_type)
    mcp, _ = await _registered_tool(core_client)
    with sent_messages(core_client) as sent, pytest.raises(ToolError, match=key):
        await _call(mcp, helper_type=helper_type, name="T", config={key: "x"})
    sent_types = [c.args[0]["type"] for c in sent.call_args_list]
    assert f"{helper_type}/create" not in sent_types


async def test_one_field_passed_twice_with_different_values_is_rejected(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await _set_up(hass, "input_number")
    mcp, _ = await _registered_tool(core_client)
    base = {"helper_type": "input_number", "name": "T", "action": "create"}
    with (
        sent_messages(core_client) as sent,
        pytest.raises(ToolError, match="both as a parameter and in config"),
    ):
        await _call(mcp, **base, config={"name": "U", "min": 1, "max": 5})
    sent_types = [c.args[0]["type"] for c in sent.call_args_list]
    assert "input_number/create" not in sent_types
    # Agreeing values are fine. Core also needs ``max`` for an input_number.
    created = await HelperConfigTools(core_client).ha_config_set_helper(
        helper_type="input_number",
        name="T",
        action="create",
        config={"name": "T", "min": 1, "max": 5},
    )
    stored = await _stored(core_client, "input_number", created["data"]["id"])
    assert stored["min"] == 1


async def test_error_context_lists_cores_fields_when_served(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    await _set_up(hass, "input_number")
    mcp, _ = await _registered_tool(component_core_client)
    with pytest.raises(ToolError) as exc:
        await _call(
            mcp,
            helper_type="input_number",
            name=_name(),
            action="create",
            config={"min": "x"},
        )
    body = json.loads(str(exc.value))
    assert any(field["name"] == "min" for field in body["data_schema"])


async def test_error_context_has_no_schema_without_component(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await _set_up(hass, "input_number")
    mcp, _ = await _registered_tool(core_client)
    with pytest.raises(ToolError) as exc:
        await _call(
            mcp,
            helper_type="input_number",
            name=_name(),
            action="create",
            config={"min": "x"},
        )
    assert "data_schema" not in str(exc.value)


async def test_create_routes_through_component(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    area_registry: ar.AreaRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    await _set_up(hass, "input_number")
    area = area_registry.async_create("Kitchen")
    tools = HelperConfigTools(component_core_client)
    with sent_messages(component_core_client) as sent:
        result = await tools.ha_config_set_helper(
            helper_type="input_number",
            name=_name(),
            action="create",
            area_id=area.id,
            config={"min": 1.0, "max": 9.0},
        )
    sent_types = [c.args[0]["type"] for c in sent.call_args_list]
    assert "input_number/create" not in sent_types
    entry = entity_registry.async_get(result["entity_id"])
    assert entry is not None
    assert entry.area_id == area.id
    stored = await _stored(component_core_client, "input_number", result["data"]["id"])
    assert stored["min"] == 1.0
    assert stored["max"] == 9.0


async def test_schedule_update_keeps_unpassed_days(
    hass: HomeAssistant, core_client: HomeAssistantClient, tools: HelperConfigTools
) -> None:
    await _set_up(hass, "schedule")
    created = await tools.ha_config_set_helper(
        helper_type="schedule",
        name=_name(),
        config={"monday": [{"from": "07:00:00", "to": "08:00:00"}]},
    )
    item_id = created["data"]["id"]
    await tools.ha_config_set_helper(
        helper_type="schedule",
        helper_id=item_id,
        config={"tuesday": [{"from": "09:00", "to": "10:00"}]},
    )
    stored = await _stored(core_client, "schedule", item_id)
    assert stored["monday"] == [{"from": "07:00:00", "to": "08:00:00"}]
    assert stored["tuesday"][0]["from"].startswith("09:00")
    # Removing the schedule cancels the timer for its next block.
    removed = await core_client.send_websocket_message(
        {"type": "schedule/delete", "schedule_id": item_id}
    )
    assert removed["success"], removed


async def test_websocket_update_of_a_person_reads_its_storage_items(
    hass: HomeAssistant, core_client: HomeAssistantClient, tools: HelperConfigTools
) -> None:
    """person/list nests the editable items under "storage"."""
    await _set_up(hass, "person")
    created = await tools.ha_config_set_helper(
        helper_type="person",
        name=_name(),
        config={"device_trackers": ["device_tracker.a"]},
    )
    item_id = created["data"]["id"]
    await tools.ha_config_set_helper(
        helper_type="person", helper_id=item_id, name="Pat"
    )
    listed = await core_client.send_websocket_message({"type": "person/list"})
    stored = next(i for i in listed["result"]["storage"] if i["id"] == item_id)
    assert stored["device_trackers"] == ["device_tracker.a"]
    assert stored["name"] == "Pat"


async def test_zone_update_without_icon_keeps_a_stored_icon(
    hass: HomeAssistant, core_client: HomeAssistantClient, tools: HelperConfigTools
) -> None:
    await _set_up(hass, "zone")
    # Another zone with an icon comes first, so the lookup must match by id.
    await tools.ha_config_set_helper(
        helper_type="zone",
        name="Other",
        icon="mdi:map",
        config={"latitude": 3.0, "longitude": 4.0, "radius": 100.0},
    )
    created = await tools.ha_config_set_helper(
        helper_type="zone",
        name=_name(),
        icon="mdi:school",
        config={"latitude": 1.0, "longitude": 2.0, "radius": 100.0},
    )
    item_id = created["data"]["id"]
    await tools.ha_config_set_helper(
        helper_type="zone", helper_id=item_id, config={"radius": 50.0}
    )
    stored = await _stored(core_client, "zone", item_id)
    assert stored["icon"] == "mdi:school"
    assert stored["radius"] == 50.0


async def test_collection_payload_keeps_tag_id_on_create() -> None:
    create = {"type": "tag/create", "name": "T", "tag_id": "abc"}
    update = {"type": "tag/update", "tag_id": "abc", "name": "T2"}
    assert client_payload("tag", create) == {"name": "T", "tag_id": "abc"}
    assert client_payload("tag", update) == {"name": "T2"}


async def test_update_routes_through_component_with_merged_payload(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    category_registry: cr.CategoryRegistry,
) -> None:
    await _set_up(hass, "input_number")
    item_id = await _create(
        component_core_client, "input_number", {"min": 0.0, "max": 50.0, "step": 5.0}
    )
    category = category_registry.async_create(scope="helpers", name="Cat1")
    tools = HelperConfigTools(component_core_client)
    with sent_messages(component_core_client) as sent:
        result = await tools.ha_config_set_helper(
            helper_type="input_number",
            helper_id=item_id,
            config={"max": 80.0},
            category=category.category_id,
        )
    sent_types = [c.args[0]["type"] for c in sent.call_args_list]
    assert "input_number/update" not in sent_types
    stored = await _stored(component_core_client, "input_number", item_id)
    assert stored["min"] == 0
    assert stored["max"] == 80
    assert stored["step"] == 5
    entry = entity_registry.async_get(result["entity_id"])
    assert entry is not None
    assert entry.categories == {"helpers": category.category_id}


async def test_tag_update_routes_through_component(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    area_registry: ar.AreaRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    await _set_up(hass, "tag")
    await _create_tag(component_core_client, description="old")
    area = area_registry.async_create("Kitchen")
    await HelperConfigTools(component_core_client).ha_config_set_helper(
        helper_type="tag", helper_id="abc-1", name="T2", area_id=area.id
    )
    stored = await _stored(component_core_client, "tag", "abc-1")
    assert stored["description"] == "old"
    assert stored["name"] == "T2"
    entity_id = entity_registry.async_get_entity_id("tag", "tag", "abc-1")
    assert entity_id is not None
    entry = entity_registry.async_get(entity_id)
    assert entry is not None
    assert entry.area_id == area.id


async def test_empty_category_clears_via_the_component(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    tools: HelperConfigTools,
    category_registry: cr.CategoryRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    await _set_up(hass, "input_boolean")
    item_id = await _create(core_client, "input_boolean", {})
    entity_id = entity_registry.async_get_entity_id(
        "input_boolean", "input_boolean", item_id
    )
    assert entity_id is not None
    category = category_registry.async_create(scope="helpers", name="Cat")
    entity_registry.async_update_entity(
        entity_id, categories={"helpers": category.category_id}
    )
    await tools.ha_config_set_helper(
        helper_type="input_boolean", helper_id=item_id, category=""
    )
    entry = entity_registry.async_get(entity_id)
    assert entry is not None
    assert "helpers" not in (entry.categories or {})


async def test_blank_clears_and_quote_only_is_rejected(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
) -> None:
    await _set_up(hass, "input_boolean")
    mcp, _ = await _registered_tool(core_client)
    base = {"helper_type": "input_boolean", "name": "B", "action": "create"}
    created = await HelperConfigTools(core_client).ha_config_set_helper(
        helper_type="input_boolean",
        name=_name(),
        action="create",
        area_id="  ",
        icon=" ",
        category="",
    )
    entry = entity_registry.async_get(created["entity_id"])
    assert entry is not None
    assert entry.area_id is None
    assert entry.icon is None
    with pytest.raises(ToolError, match="only quote characters"):
        await _call(mcp, **base, category='""')
    created = await HelperConfigTools(core_client).ha_config_set_helper(
        helper_type="input_boolean",
        name=_name(),
        action="create",
        config={"icon": " "},
    )
    entry = entity_registry.async_get(created["entity_id"])
    assert entry is not None
    assert entry.icon is None


async def test_empty_category_clears_the_scope(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    category_registry: cr.CategoryRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    from ha_mcp.tools.config_write_helpers import apply_entity_category

    await _set_up(hass, "input_boolean")
    item_id = await _create(core_client, "input_boolean", {})
    entity_id = entity_registry.async_get_entity_id(
        "input_boolean", "input_boolean", item_id
    )
    assert entity_id is not None
    category = category_registry.async_create(scope="helpers", name="Cat")
    entity_registry.async_update_entity(
        entity_id, categories={"helpers": category.category_id}
    )
    result: dict[str, Any] = {}
    await apply_entity_category(core_client, entity_id, "", "helpers", result)
    entry = entity_registry.async_get(entity_id)
    assert entry is not None
    assert "helpers" not in (entry.categories or {})
    assert result == {"category": None}


async def test_websocket_create_failure_code(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Core's own invalid_format rejection is reported as an invalid parameter."""
    await _set_up(hass, "input_boolean")
    mcp, _ = await _registered_tool(core_client)
    with pytest.raises(ToolError, match="VALIDATION_INVALID_PARAMETER"):
        await _call(
            mcp,
            helper_type="input_boolean",
            name="B",
            action="create",
            config={"initial": "not-a-bool"},
        )


async def test_websocket_tag_create_reports_the_real_entity(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
) -> None:
    await _set_up(hass, "tag")
    result = await HelperConfigTools(core_client).ha_config_set_helper(
        helper_type="tag",
        name="Front door",
        action="create",
        config={"tag_id": "abc-1"},
    )
    assert result["entity_id"] == entity_registry.async_get_entity_id(
        "tag", "tag", "abc-1"
    )


@pytest.mark.parametrize("helper_id", ["tag.front_door", "abc-1"])
async def test_websocket_tag_update_resolves_the_tag_id(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    helper_id: str,
) -> None:
    await _set_up(hass, "tag")
    await _create_tag(core_client, name="Front door")
    result = await HelperConfigTools(core_client).ha_config_set_helper(
        helper_type="tag", helper_id=helper_id, name="Front"
    )
    stored = await _stored(core_client, "tag", "abc-1")
    assert stored["name"] == "Front"
    assert result["entity_id"] == entity_registry.async_get_entity_id(
        "tag", "tag", "abc-1"
    )


async def test_websocket_tag_update_writes_the_icon_to_the_registry(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
) -> None:
    """A tag has no icon field, so Core would reject one: it goes to the registry."""
    await _set_up(hass, "tag")
    await _create_tag(core_client, name="Front door")
    await HelperConfigTools(core_client).ha_config_set_helper(
        helper_type="tag", helper_id="abc-1", icon="mdi:nfc"
    )
    entity_id = entity_registry.async_get_entity_id("tag", "tag", "abc-1")
    assert entity_id is not None
    entry = entity_registry.async_get(entity_id)
    assert entry is not None
    assert entry.icon == "mdi:nfc"
    assert "icon" not in await _stored(core_client, "tag", "abc-1")


async def test_websocket_tag_update_clears_the_registry_icon(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
) -> None:
    """A cleared icon is left out of tag/update and clears the registry icon."""
    await _set_up(hass, "tag")
    await _create_tag(core_client, name="Front door")
    entity_id = entity_registry.async_get_entity_id("tag", "tag", "abc-1")
    assert entity_id is not None
    entity_registry.async_update_entity(entity_id, icon="mdi:x")
    await HelperConfigTools(core_client).ha_config_set_helper(
        helper_type="tag", helper_id="abc-1", icon=""
    )
    entry = entity_registry.async_get(entity_id)
    assert entry is not None
    assert entry.icon is None


async def test_websocket_tag_update_applies_registry_fields(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    area_registry: ar.AreaRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    await _set_up(hass, "tag")
    await _create_tag(core_client, name="Front door")
    area = area_registry.async_create("Kitchen")
    await HelperConfigTools(core_client).ha_config_set_helper(
        helper_type="tag", helper_id="abc-1", area_id=area.id
    )
    entity_id = entity_registry.async_get_entity_id("tag", "tag", "abc-1")
    assert entity_id is not None
    entry = entity_registry.async_get(entity_id)
    assert entry is not None
    assert entry.area_id == area.id


async def test_component_tag_update_reports_the_component_entity(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
) -> None:
    await _set_up(hass, "tag")
    await _create_tag(component_core_client, name="Front door")
    result = await HelperConfigTools(component_core_client).ha_config_set_helper(
        helper_type="tag", helper_id="abc-1", name="F"
    )
    assert result["entity_id"] == entity_registry.async_get_entity_id(
        "tag", "tag", "abc-1"
    )
    stored = await _stored(component_core_client, "tag", "abc-1")
    assert stored["name"] == "F"


async def test_write_falls_back_to_websocket_when_component_unavailable(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
) -> None:
    await _set_up(hass, "input_boolean")
    with sent_messages(core_client) as sent:
        result = await HelperConfigTools(core_client).ha_config_set_helper(
            helper_type="input_boolean", name=_name(), action="create"
        )
    sent_types = [c.args[0]["type"] for c in sent.call_args_list]
    assert sent_types.count("input_boolean/create") == 1
    entry = entity_registry.async_get(result["entity_id"])
    assert entry is not None
    assert entry.unique_id == result["data"]["id"]


async def test_a_long_choice_list_is_counted_not_spelled_out_in_the_description() -> (
    None
):
    """Core's device_class field has dozens of choices; spelled out, they
    would sit in every tool listing. describe lists them."""
    from ha_mcp.transforms.component_helpers import render_core_keys

    choices = [[f"choice_{n}", f"Choice {n}"] for n in range(40)]
    rendered = render_core_keys(
        {"input_number": {"create": [{"name": "device_class", "options": choices}]}}
    )

    assert "choice_7" not in rendered
    assert "40" in rendered
    assert "describe=True" in rendered


class TestCatalogTransform:
    async def test_registration_advertises_core_fields_and_keeps_wait(
        self, hass: HomeAssistant, component_core_client: HomeAssistantClient
    ) -> None:
        # The component describes a type only once its domain is loaded.
        for helper_type in hc_schemas.SIMPLE_HELPER_TYPES:
            assert await async_setup_component(hass, helper_type, {})
        # The helper tools' registration installs the transform.
        _, tool = await _registered_tool(component_core_client)
        properties = tool.parameters["properties"]
        assert "wait" in properties  # flow helpers and fallback writes use it
        description = properties["config"]["description"]
        assert hc_schemas.SIMPLE_CONFIG_KEYS_DESCRIPTION not in description
        assert "input_number:" in description
        assert "min (float, required)" in description

    async def test_static_contract_without_component(
        self, core_client: HomeAssistantClient
    ) -> None:
        _, tool = await _registered_tool(core_client)
        properties = tool.parameters["properties"]
        assert "wait" in properties
        assert (
            hc_schemas.SIMPLE_CONFIG_KEYS_DESCRIPTION
            in properties["config"]["description"]
        )


async def test_a_tag_update_is_not_waited_on(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tag is not waited on after an update, as after its create."""
    await _set_up(hass, "tag")
    await _create_tag(core_client, name="Front door")
    waited = AsyncMock(wraps=hc_update.wait_for_entity_registered)
    monkeypatch.setattr(hc_update, "wait_for_entity_registered", waited)
    await HelperConfigTools(core_client).ha_config_set_helper(
        helper_type="tag", helper_id="abc-1", name="F", wait=True
    )
    waited.assert_not_awaited()
