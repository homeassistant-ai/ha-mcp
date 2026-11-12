"""ha_config_set_helper resolves an update's target as the removal and the
backups do (#2632): a bare id is a storage id, and only ``<type>.<name>`` is
an entity_id. A renamed entity can carry another helper's id as its name, so
an update never goes to the entity a bare id names, and the reply and the
registry fields name the helper's own entity or none.

The tool runs on the server's client against a real ``hass`` whose
input_boolean helpers, entity renames and registry entries are Core's own;
``component_core_client`` adds the component's in-process path.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools import component_helper_collections as chc
from ha_mcp.tools.tools_config_helpers import HelperConfigTools

from ._client_spies import sent_messages
from ._tool_errors import _error


@pytest.fixture
def mine() -> str:
    """A storage id no deleted registry entry can be restored for."""
    return f"porch_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def other() -> str:
    return f"hall_{uuid.uuid4().hex[:8]}"


async def _set_up(hass: HomeAssistant) -> None:
    # ``config`` serves the entity-registry commands.
    for domain in ("config", "input_boolean"):
        assert await async_setup_component(hass, domain, {})


async def _create(
    hass: HomeAssistant, client: HomeAssistantClient, *item_ids: str
) -> None:
    """Stored input_booleans, each with the storage id and entity of its name."""
    await _set_up(hass)
    for item_id in item_ids:
        created = await client.send_websocket_message(
            {"type": "input_boolean/create", "name": item_id}
        )
        assert created["success"], created
        assert created["result"]["id"] == item_id
    await hass.async_block_till_done()


async def _rename(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, *renames: tuple[str, str]
) -> None:
    """Rename input_boolean entities, ``(old object id, new object id)`` each."""
    for old, new in renames:
        entity_registry.async_update_entity(
            f"input_boolean.{old}", new_entity_id=f"input_boolean.{new}"
        )
    await hass.async_block_till_done()


async def _give_my_entity_id_to_other(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, mine: str, other: str
) -> None:
    """mine's entity was renamed to ``<mine>_2``; other's entity now holds
    ``input_boolean.<mine>``."""
    await _rename(hass, entity_registry, (mine, f"{mine}_2"), (other, mine))


async def _stored_name(client: HomeAssistantClient, item_id: str) -> str:
    listed = await client.send_websocket_message({"type": "input_boolean/list"})
    assert listed["success"], listed
    return str(next(i for i in listed["result"] if i["id"] == item_id)["name"])


def _updated(sent: Any) -> list[str]:
    return [
        call.args[0]["input_boolean_id"]
        for call in sent.call_args_list
        if call.args[0]["type"] == "input_boolean/update"
    ]


async def _update(client: HomeAssistantClient, **kwargs: Any) -> dict[str, Any]:
    result: dict[str, Any] = await HelperConfigTools(client).ha_config_set_helper(
        helper_type="input_boolean", **kwargs
    )
    return result


@pytest.mark.parametrize(
    ("target", "world", "found", "read_by_entity"),
    [
        ("stored id", "component", True, False),
        ("entity_id", "component", True, True),
        ("entity_id", "no component", False, False),
        ("entity_id", "no collection", False, False),
    ],
)
async def test_only_an_entity_id_the_component_misses_is_read_by_its_entity(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    mine: str,
    other: str,
    target: str,
    world: str,
    found: bool,
    read_by_entity: bool,
) -> None:
    """A failed read is not "no such item" (without the component, or with
    a component that has no input_boolean collection), so only the
    component's not_found for an entity_id leads to the entity read."""
    if world != "no component":
        from custom_components.ha_mcp_tools import websocket_api

        websocket_api.async_register_commands(hass)
    if world != "no collection":
        await _create(hass, core_client, mine, other)
        await _rename(hass, entity_registry, (other, f"{mine}_x"))
    helper_id = mine if target == "stored id" else f"input_boolean.{mine}_x"

    # ha_config_set_helper passes the helper_id as the entity_id too.
    with pytest.MonkeyPatch.context() as mp:
        spy = _spy_entity_reads(mp)
        item = await chc.read_stored_helper_item(
            core_client, "input_boolean", helper_id, helper_id
        )

    expected = mine if target == "stored id" else other
    assert (item["item_id"] if item else None) == (expected if found else None)
    assert [c.kwargs.get("entity_id") for c in spy.await_args_list] == (
        [helper_id] if read_by_entity else []
    )


def _spy_entity_reads(mp: pytest.MonkeyPatch) -> AsyncMock:
    """Spy on the component read by entity_id; it calls through to Core."""
    spy = AsyncMock(wraps=chc.read_helper_item)
    mp.setattr(chc, "read_helper_item", spy)
    return spy


async def test_a_bare_id_the_component_misses_is_refused_unread(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    mine: str,
    other: str,
) -> None:
    """``input_boolean.<mine>`` is other's entity after mine's removal: the bare
    id must not reach it."""
    await _create(hass, component_core_client, other)
    await _rename(hass, entity_registry, (other, mine))

    with pytest.MonkeyPatch.context() as mp, pytest.raises(ToolError) as exc_info:
        spy = _spy_entity_reads(mp)
        await _update(component_core_client, helper_id=mine, name="Renamed")

    assert _error(exc_info)["code"] == "ENTITY_NOT_FOUND"
    assert spy.await_args_list == []
    assert await _stored_name(component_core_client, other) == other


async def test_a_bare_id_the_component_misses_hints_at_create(
    hass: HomeAssistant, component_core_client: HomeAssistantClient, mine: str
) -> None:
    """The component path keeps the create hint the WebSocket path gives."""
    await _set_up(hass)

    with pytest.raises(ToolError) as exc_info:
        await _update(component_core_client, helper_id=mine, name="Brand New Helper")

    error = _error(exc_info)
    assert error["code"] == "ENTITY_NOT_FOUND"
    assert any("Brand New Helper" in s for s in error["suggestions"])


async def test_a_refused_bare_id_says_which_helper_and_where_to_find_it(
    hass: HomeAssistant, core_client: HomeAssistantClient, mine: str
) -> None:
    """Without a name the refusal still names the target and the listing."""
    await _set_up(hass)

    with pytest.raises(ToolError) as exc_info:
        await chc.stored_helper_target(core_client, "input_boolean", mine)

    payload = json.loads(str(exc_info.value))
    assert (payload["target"], payload["helper_type"]) == (mine, "input_boolean")
    error = payload["error"]
    hints = [error.get("suggestion", ""), *error.get("suggestions", [])]
    assert any(
        "ha_config_list_helpers(helper_type='input_boolean')" in s for s in hints
    )


async def test_a_bare_id_no_helper_has_updates_nothing(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    mine: str,
    other: str,
) -> None:
    """Without the component: ``input_boolean.<mine>`` is other's entity, so
    the bare mine would update other."""
    await _create(hass, core_client, other)
    await _rename(hass, entity_registry, (other, mine))

    with sent_messages(core_client) as sent, pytest.raises(ToolError) as exc_info:
        await _update(core_client, helper_id=mine, name="A")

    error = json.loads(str(exc_info.value))
    assert error["error"]["code"] == "ENTITY_NOT_FOUND"
    assert "omit helper_id" in str(error)
    assert _updated(sent) == []
    assert await _stored_name(core_client, other) == other


async def test_registry_fields_of_an_update_go_to_the_helper_s_own_entity(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    mine: str,
    other: str,
) -> None:
    """``input_boolean.<mine>`` would give other the area mine was given."""
    await _create(hass, core_client, mine, other)
    await _give_my_entity_id_to_other(hass, entity_registry, mine, other)
    area_id = f"porch_area_{uuid.uuid4().hex[:8]}"
    created = await core_client.send_websocket_message(
        {"type": "config/area_registry/create", "name": area_id}
    )
    assert created["success"], created
    area_id = created["result"]["area_id"]

    with sent_messages(core_client) as sent:
        result = await _update(core_client, helper_id=mine, area_id=area_id)

    assert _updated(sent) == [mine]
    assert result["entity_id"] == f"input_boolean.{mine}_2"
    own = entity_registry.async_get(f"input_boolean.{mine}_2")
    others = entity_registry.async_get(f"input_boolean.{mine}")
    assert own is not None
    assert others is not None
    assert (own.area_id, others.area_id) == (area_id, None)


@pytest.mark.parametrize("with_component", [False, True], ids=["core", "component"])
async def test_an_updated_helper_without_an_entity_names_none(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    mine: str,
    with_component: bool,
) -> None:
    """mine's registry entry was removed: ``input_boolean.<mine>`` would name an
    entity mine does not have. Without the component the reply says why it
    names none and that it did not wait."""
    if with_component:
        from custom_components.ha_mcp_tools import websocket_api

        websocket_api.async_register_commands(hass)
    await _create(hass, core_client, mine)
    entity_registry.async_remove(f"input_boolean.{mine}")
    await hass.async_block_till_done()

    result = await _update(core_client, helper_id=mine, name="P", wait=True)

    assert result.get("entity_id") is None
    assert await _stored_name(core_client, mine) == "P"
    if not with_component:
        (warning,) = result["warnings"]
        assert "the input_boolean has no entity in the registry" in warning
        assert "not waited on" in warning
