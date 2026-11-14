"""ha_mcp_tools helper_schemas / helper_item / helper_write, and the server client.

The helpers live in Core's own storage collections on a real ``hass``; the
commands run through Core's WebSocket API (``component_ws``), and the server's
client (``component_core_client``) reads and writes them the way the helper
tools do.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.components import websocket_api as core_websocket_api
from homeassistant.components.input_select import STORAGE_FIELDS as SELECT_FIELDS
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import category_registry as cr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import label_registry as lr
from homeassistant.setup import async_setup_component
from pytest_homeassistant.typing import MockHAClientWebSocket

from custom_components.ha_mcp_tools import helper_collections as hc
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantCommandNotSent
from ha_mcp.tools import component_helper_collections as client_mod
from ha_mcp.tools.component_api import invalidate_caps
from ha_mcp.tools.config_helpers.schemas import SIMPLE_HELPER_TYPES

from ._component_core import ws_call, ws_result
from ._component_frames import pooled


async def _write(ws: MockHAClientWebSocket, **msg: Any) -> Any:
    return await ws_result(ws, hc.WS_HELPER_WRITE, **msg)


async def _item(ws: MockHAClientWebSocket, **msg: Any) -> Any:
    return await ws_result(ws, hc.WS_HELPER_ITEM, **msg)


@pytest.fixture
async def numbers(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "input_number", {})


async def _target(ws: MockHAClientWebSocket) -> Any:
    """``input_number.target``, stored through the component."""
    return await _write(
        ws,
        helper_type="input_number",
        action="create",
        data={"name": "Target", "min": 0, "max": 9},
    )


async def test_an_item_is_read_from_cores_collection_by_entity_or_id(
    component_ws: MockHAClientWebSocket,
    entity_registry: er.EntityRegistry,
    numbers: None,
) -> None:
    created = await _target(component_ws)
    entity_registry.async_get_or_create("sensor", "template", "target")

    by_entity = await _item(
        component_ws, helper_type="input_number", entity_id="input_number.target"
    )
    by_id = await _item(component_ws, helper_type="input_number", item_id="target")
    other_platform = await _item(
        component_ws, helper_type="input_number", entity_id="sensor.target"
    )

    assert by_entity == {
        "success": True,
        "item_id": "target",
        "entity_id": "input_number.target",
        "item": created["item"],
    }
    assert by_id["entity_id"] == "input_number.target"
    assert other_platform["error"]["code"] == "not_found"


async def test_a_type_that_is_not_set_up_is_unavailable(
    component_ws: MockHAClientWebSocket,
) -> None:
    item = await _item(component_ws, helper_type="counter", item_id="x")
    write = await _write(
        component_ws, helper_type="counter", action="create", data={"name": "X"}
    )

    assert item["error"]["code"] == write["error"]["code"] == "unavailable"


async def test_schemas_describe_each_set_up_type_and_name_the_rest(
    component_ws: MockHAClientWebSocket, numbers: None
) -> None:
    result = await ws_result(component_ws, hc.WS_HELPER_SCHEMAS)

    assert set(result["types"]) == {"input_number"}
    for action in ("create", "update"):
        names = {field["name"] for field in result["types"]["input_number"][action]}
        assert {"name", "min", "max"} <= names
    assert set(result["unavailable"]) == set(hc.SIMPLE_HELPER_TYPES) - {"input_number"}


async def test_a_field_cores_serializer_cannot_express_keeps_its_name(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """input_select's options carry a check Core's serializer cannot express;
    the type is still described, field by field, none dropped."""
    assert await async_setup_component(hass, "input_select", {})

    result = await ws_result(component_ws, hc.WS_HELPER_SCHEMAS)

    described = {field["name"] for field in result["types"]["input_select"]["create"]}
    assert described == {str(key) for key in SELECT_FIELDS}


def test_server_and_component_list_the_same_storage_helpers() -> None:
    """The server's fallback list routes the types the component serves; a
    type in only one of them is described by one path and refused by the other.
    test_helper_domains_match_frontend.py pins the frontend part to the bundle."""
    assert set(hc.SIMPLE_HELPER_TYPES) == SIMPLE_HELPER_TYPES
    own_page = {"zone", "person", "tag"}
    assert set(hc.FRONTEND_HELPER_DOMAINS) == SIMPLE_HELPER_TYPES - own_page


async def test_a_write_stores_the_item_then_applies_its_registry_fields(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    entity_registry: er.EntityRegistry,
    area_registry: ar.AreaRegistry,
    label_registry: lr.LabelRegistry,
    category_registry: cr.CategoryRegistry,
    numbers: None,
) -> None:
    kitchen = area_registry.async_create("Kitchen")
    label = label_registry.async_create("Heating")
    category = category_registry.async_create(name="Climate", scope="helpers")

    result = await _write(
        component_ws,
        helper_type="input_number",
        action="create",
        data={"name": "Target", "min": 0, "max": 9},
        registry={
            "area_id": kitchen.id,
            "labels": [label.label_id],
            "category": category.category_id,
            "icon": "",
        },
    )

    assert (result["success"], result["entity_id"]) == (True, "input_number.target")
    assert result["item"]["min"] == 0
    assert result["registry_applied"] == {
        "icon": None,
        "area_id": kitchen.id,
        "labels": [label.label_id],
        "category": category.category_id,
    }
    entry = entity_registry.async_get("input_number.target")
    assert entry is not None
    assert (entry.area_id, entry.labels) == (kitchen.id, {label.label_id})
    assert entry.categories == {"helpers": category.category_id}


async def test_clearing_the_helpers_category_keeps_the_other_scopes(
    component_ws: MockHAClientWebSocket,
    entity_registry: er.EntityRegistry,
    numbers: None,
) -> None:
    await _target(component_ws)
    entity_registry.async_update_entity(
        "input_number.target", categories={"automation": "a1", "helpers": "c1"}
    )

    result = await _write(
        component_ws,
        helper_type="input_number",
        action="update",
        item_id="target",
        data={"name": "Target", "min": 0, "max": 9},
        registry={"category": ""},
    )

    assert result["registry_applied"] == {"category": None}
    entry = entity_registry.async_get("input_number.target")
    assert entry is not None
    assert entry.categories == {"automation": "a1"}


async def test_every_schema_error_is_reported(
    component_ws: MockHAClientWebSocket, numbers: None
) -> None:
    """Like Core's own commands, every problem, not only the first."""
    result = await _write(
        component_ws,
        helper_type="input_number",
        action="create",
        data={"name": "X", "min": "low", "max": 9, "mode": "dial"},
    )

    message = result["error"]["message"]
    assert result["error"]["code"] == "invalid"
    assert "min" in message and "mode" in message


async def test_a_rejected_or_missing_item_is_reported_without_raising(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, numbers: None
) -> None:
    assert await async_setup_component(hass, "tag", {})
    tag = {"helper_type": "tag", "action": "create", "data": {"tag_id": "abc"}}
    await _write(component_ws, **tag)

    duplicate = await _write(component_ws, **tag)
    inverted = await _write(
        component_ws,
        helper_type="input_number",
        action="create",
        data={"name": "X", "min": 9, "max": 0},
    )
    missing = await _write(
        component_ws,
        helper_type="input_number",
        action="update",
        item_id="nope",
        data={"name": "X", "min": 0, "max": 9},
    )

    assert duplicate["error"]["code"] == "invalid"
    assert "already exists" in duplicate["error"]["message"]
    assert inverted["error"]["code"] == "invalid"
    assert missing["error"]["code"] == "not_found"


@pytest.mark.parametrize(
    ("action", "error"),
    [
        ("create", ValueError("listener rejected the entity")),
        ("update", ValueError("listener rejected the entity")),
        ("create", HomeAssistantError("listener rejected the entity")),
    ],
)
async def test_a_listener_failure_after_the_store_is_not_reported_as_invalid(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    numbers: None,
    action: str,
    error: Exception,
) -> None:
    """Core stores the item, then awaits its listeners; the item is stored,
    so the failure reaches the server as an unknown outcome, not a rejection."""
    await _target(component_ws)
    owner = hc.collection_owner(hass, "input_number")

    async def listener(change_type: str, item_id: str, config: Any) -> None:
        raise error

    owner.storage_collection.async_add_listener(listener)

    reply = await ws_call(
        component_ws,
        hc.WS_HELPER_WRITE,
        helper_type="input_number",
        action=action,
        item_id="target",
        data={
            "name": "Second" if action == "create" else "Target",
            "min": 1,
            "max": 9,
        },
    )

    assert reply["success"] is False
    assert "listener rejected the entity" in reply["error"]["message"]


async def test_the_commands_are_registered_and_advertised(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    info = await ws_result(component_ws, "ha_mcp_tools/info")

    assert {hc.WS_HELPER_SCHEMAS, hc.WS_HELPER_ITEM, hc.WS_HELPER_WRITE} <= set(
        hass.data[core_websocket_api.DOMAIN]
    )
    assert {"helper_schemas", "helper_item", "helper_write"} <= set(
        info["capabilities"]
    )


@pytest.mark.parametrize(
    ("command", "extra"),
    [
        (hc.WS_HELPER_SCHEMAS, {}),
        (hc.WS_HELPER_ITEM, {"helper_type": "zone", "item_id": "home"}),
        (hc.WS_HELPER_WRITE, {"helper_type": "zone", "action": "create", "data": {}}),
    ],
)
@pytest.mark.parametrize("admin", [False, True])
async def test_the_commands_are_admin_only(
    component_ws: MockHAClientWebSocket,
    component_ws_read_only: MockHAClientWebSocket,
    command: str,
    extra: dict[str, Any],
    admin: bool,
) -> None:
    reply = await ws_call(
        component_ws if admin else component_ws_read_only, command, **extra
    )

    assert reply["success"] is admin
    if not admin:
        assert reply["error"]["code"] == "unauthorized"


# --- server client --------------------------------------------------------------
async def test_a_write_returns_the_components_result(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    assert await async_setup_component(hass, "input_boolean", {})

    result = await client_mod.write_helper_item(
        component_core_client, "input_boolean", "create", {"name": "Guest"}
    )

    assert result is not None
    assert (result["success"], result["entity_id"]) == (True, "input_boolean.guest")


async def test_a_write_falls_back_when_the_type_is_not_set_up(
    component_core_client: HomeAssistantClient,
) -> None:
    assert (
        await client_mod.write_helper_item(
            component_core_client, "input_boolean", "create", {"name": "Guest"}
        )
        is None
    )


async def test_a_write_that_was_never_sent_falls_back(
    component_core_client: HomeAssistantClient,
) -> None:
    await client_mod.fetch_helper_schemas(component_core_client)
    ws = await pooled(component_core_client)

    with patch.object(
        ws, "send_command", side_effect=HomeAssistantCommandNotSent("down")
    ):
        result = await client_mod.write_helper_item(
            component_core_client, "input_boolean", "create", {}
        )

    assert result is None


async def test_a_component_without_the_write_falls_back_and_is_probed_again(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    await client_mod.fetch_helper_schemas(component_core_client)
    del hass.data[core_websocket_api.DOMAIN][hc.WS_HELPER_WRITE]

    with patch.object(client_mod, "invalidate_caps", wraps=invalidate_caps) as drop:
        result = await client_mod.write_helper_item(
            component_core_client, "input_boolean", "create", {}
        )

    assert result is None
    drop.assert_called_once_with(component_core_client)


async def test_a_write_lost_after_dispatch_has_an_unknown_outcome(
    component_core_client: HomeAssistantClient,
) -> None:
    await client_mod.fetch_helper_schemas(component_core_client)
    ws = await pooled(component_core_client)

    with (
        patch.object(ws, "send_command", side_effect=TimeoutError("lost")),
        pytest.raises(ToolError, match="outcome is unknown"),
    ):
        await client_mod.write_helper_item(
            component_core_client, "input_boolean", "create", {}
        )


async def test_a_cancelled_write_stays_cancelled(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """The command is held unanswered while the caller is cancelled."""
    assert await async_setup_component(hass, "input_boolean", {})
    handler, schema = hass.data[core_websocket_api.DOMAIN][hc.WS_HELPER_WRITE]
    started, release = asyncio.Event(), asyncio.Event()

    def held(hass_: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
        started.set()

        async def run_when_released() -> None:
            await release.wait()
            handler(hass_, connection, msg)

        hass_.async_create_task(run_when_released())

    core_websocket_api.async_register_command(hass, hc.WS_HELPER_WRITE, held, schema)
    write = asyncio.create_task(
        client_mod.write_helper_item(
            component_core_client, "input_boolean", "create", {"name": "Guest"}
        )
    )
    await started.wait()
    write.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.gather(write)
    release.set()
    await hass.async_block_till_done()


async def test_a_rejected_write_carries_the_callers_error_context(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    assert await async_setup_component(hass, "input_number", {})

    with pytest.raises(ToolError, match="data_schema") as err:
        await client_mod.write_helper_item(
            component_core_client,
            "input_number",
            "create",
            {"name": "X", "min": 9, "max": 0},
            error_context={"data_schema": ["x"]},
        )

    assert "Failed to create input_number" in str(err.value)


async def test_the_schemas_are_read_once_and_cached(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    assert await async_setup_component(hass, "input_boolean", {})
    await client_mod.fetch_helper_schemas(component_core_client)
    ws = await pooled(component_core_client)

    with patch.object(ws, "send_command", wraps=ws.send_command) as send:
        first = await client_mod.fetch_helper_schemas(component_core_client)

    assert first is not None and "input_boolean" in first
    assert send.await_count == 0
