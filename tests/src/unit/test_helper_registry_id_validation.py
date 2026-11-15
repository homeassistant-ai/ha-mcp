"""ha_config_set_helper refuses area / label / category IDs Core does not have.

Core's registry updates accept any string and answer with success, so an
unknown ID would persist as a dangling reference (issue #1150) or vanish
unnoticed. The tool checks each ID against Core's registry first, and refuses
the write when that registry cannot be read (issue #2159), naming an auth
failure as such. Core, its registries and its WebSocket API are real; only the
socket drops or the token is revoked.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import category_registry as cr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import label_registry as lr
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_config_helpers import HelperConfigTools

from ._command_faults import dropped_on, pooled_socket
from ._helper_tool_core import create_item, fresh_name, set_up
from ._receipt_faults import revoke
from ._tool_errors import error_body, error_of

KINDS = ["area", "labels", "category"]


def add_entry(hass: HomeAssistant, kind: str) -> str:
    """Create one ``kind`` entry in Core's registry and return its id."""
    if kind == "area":
        return ar.async_get(hass).async_create("Kitchen").id
    if kind == "labels":
        return lr.async_get(hass).async_create("Automation").label_id
    return cr.async_get(hass).async_create(scope="helpers", name="Lighting").category_id


def reference(kind: str, value: str) -> dict[str, Any]:
    """The tool argument that assigns ``value`` as a ``kind`` reference."""
    if kind == "area":
        return {"area_id": value}
    if kind == "labels":
        return {"labels": [value]}
    return {"category": value}


async def create_helper(client: HomeAssistantClient, **kwargs: Any) -> Any:
    return await HelperConfigTools(client).ha_config_set_helper(
        helper_type="input_boolean", name=fresh_name(), **kwargs
    )


@pytest.mark.parametrize("kind", KINDS)
async def test_an_unknown_id_is_refused_with_the_ids_that_exist(
    hass: HomeAssistant, core_client: HomeAssistantClient, kind: str
) -> None:
    await set_up(hass, "input_boolean")
    existing = add_entry(hass, kind)

    with pytest.raises(ToolError) as exc:
        await create_helper(core_client, **reference(kind, "phantom_id_xyz"))

    assert error_of(exc)["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "phantom_id_xyz" in str(exc.value)
    assert existing in str(exc.value)
    assert hass.states.async_entity_ids("input_boolean") == []


async def test_an_unknown_area_is_refused_when_no_area_exists(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await set_up(hass, "input_boolean")

    with pytest.raises(ToolError) as exc:
        await create_helper(core_client, area_id="phantom_area")

    assert error_of(exc)["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "phantom_area" in str(exc.value)


async def test_existing_area_label_and_category_are_applied_to_the_new_helper(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
) -> None:
    await set_up(hass, "input_boolean")
    area_id, label_id, category_id = (add_entry(hass, kind) for kind in KINDS)

    result = await create_helper(
        core_client, area_id=area_id, labels=[label_id], category=category_id
    )

    assert result["success"] is True
    entity_id = entity_registry.async_get_entity_id(
        "input_boolean", "input_boolean", result["data"]["id"]
    )
    assert entity_id is not None
    entry = entity_registry.async_get(entity_id)
    assert entry is not None
    assert entry.area_id == area_id
    assert entry.labels == {label_id}
    assert entry.categories == {"helpers": category_id}


async def test_an_empty_area_id_clears_instead_of_being_refused(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """``area_id=""`` is the documented clear, not an unknown area."""
    await set_up(hass, "input_boolean")

    result = await create_helper(core_client, area_id="")

    assert result["success"] is True


async def test_an_empty_label_inside_the_list_is_refused(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Only the empty list clears; an empty element is an unknown label."""
    await set_up(hass, "input_boolean")
    label_id = add_entry(hass, "labels")

    with pytest.raises(ToolError) as exc:
        await create_helper(core_client, labels=[label_id, ""])

    assert error_of(exc)["code"] == "VALIDATION_INVALID_PARAMETER"
    assert error_body(exc)["unknown_labels"] == [""]


@pytest.mark.parametrize("kind", KINDS)
async def test_a_registry_core_cannot_list_refuses_the_write(
    hass: HomeAssistant, core_client: HomeAssistantClient, kind: str
) -> None:
    """Without ``config`` loaded Core answers each registry list with an error."""
    assert await async_setup_component(hass, "input_boolean", {})

    with pytest.raises(ToolError) as exc:
        await create_helper(core_client, **reference(kind, "any_id"))

    assert error_of(exc)["code"] == "CONNECTION_FAILED"
    assert hass.states.async_entity_ids("input_boolean") == []


async def test_a_dropped_registry_read_refuses_the_write(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await set_up(hass, "input_boolean")
    area_id = add_entry(hass, "area")
    socket = await pooled_socket(core_client)

    with (
        dropped_on(hass, "config/area_registry/list", socket),
        pytest.raises(ToolError) as exc,
    ):
        await create_helper(core_client, area_id=area_id)

    assert error_of(exc)["code"] == "CONNECTION_FAILED"
    assert hass.states.async_entity_ids("input_boolean") == []


async def test_a_dropped_registry_read_refuses_the_flow_helper_write(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    assert await async_setup_component(hass, "config", {})
    area_id = add_entry(hass, "area")
    socket = await pooled_socket(core_client)

    with (
        dropped_on(hass, "config/area_registry/list", socket),
        pytest.raises(ToolError) as exc,
    ):
        await HelperConfigTools(core_client).ha_config_set_helper(
            helper_type="min_max",
            name=fresh_name(),
            config={"entity_ids": ["sensor.a"], "type": "mean"},
            area_id=area_id,
            wait=False,
        )

    assert error_of(exc)["code"] == "CONNECTION_FAILED"
    assert hass.config_entries.async_entries("min_max") == []


# Revoking the token closes the server socket, leaving its heartbeat timer.
@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_a_refused_token_is_reported_as_auth_not_as_an_outage(
    hass: HomeAssistant, core_client: HomeAssistantClient, hass_access_token: str
) -> None:
    await set_up(hass, "input_boolean")
    item_id = await create_item(core_client, "input_boolean", {})
    revoke(hass, hass_access_token)

    with pytest.raises(ToolError) as exc:
        await HelperConfigTools(core_client).ha_config_set_helper(
            helper_type="input_boolean", helper_id=item_id, area_id="kitchen"
        )

    assert error_of(exc)["code"].startswith("AUTH")
    assert "Could not validate area_id" in error_of(exc)["message"]
