"""The component's search hides what the server's visibility config hides.

The server sends its visibility config as the ``visibility`` param of
``ha_mcp_tools/search``; the component drops the hidden entities before it
counts and pages them. Each test sets up Core's registries, states and Assist
exposure through Core's API and sends the search over Core's WebSocket API.
Assist exposure is available only when the ``homeassistant`` integration is
set up.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.components.homeassistant.exposed_entities import (
    async_expose_entity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import label_registry as lr
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.typing import MockHAClientWebSocket

from ._component_core import ws_call, ws_result
from ._core_setup import add_test_entry

_SEARCH = "ha_mcp_tools/search"
_ASSIST_UNAVAILABLE = "Assist exposure data was unavailable"


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    """The config entry every registered device and entity belongs to."""
    return add_test_entry(hass)


def _area(hass: HomeAssistant, area_id: str) -> str:
    areas = ar.async_get(hass)
    area = areas.async_get_area(area_id) or areas.async_create(area_id.title())
    assert area.id == area_id
    return area.id


def _label(hass: HomeAssistant, label_id: str) -> str:
    labels = lr.async_get(hass)
    label = labels.async_get_label(label_id) or labels.async_create(label_id)
    assert label.label_id == label_id
    return label.label_id


def _device(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    key: str,
    *,
    area: str | None = None,
    labels: tuple[str, ...] = (),
) -> str:
    devices = dr.async_get(hass)
    device = devices.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("test", key)}
    )
    devices.async_update_device(
        device.id,
        area_id=_area(hass, area) if area else None,
        labels={_label(hass, label) for label in labels},
    )
    return device.id


def _register(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    entity_id: str,
    *,
    category: EntityCategory | None = None,
    hidden: bool = False,
    area: str | None = None,
    device_id: str | None = None,
    labels: tuple[str, ...] = (),
) -> None:
    """A registry entity with a state."""
    domain, object_id = entity_id.split(".", 1)
    entities = er.async_get(hass)
    row = entities.async_get_or_create(
        domain,
        "test",
        object_id,
        suggested_object_id=object_id,
        config_entry=entry,
        device_id=device_id,
        entity_category=category,
        hidden_by=er.RegistryEntryHider.USER if hidden else None,
    )
    assert row.entity_id == entity_id
    entities.async_update_entity(
        entity_id,
        area_id=_area(hass, area) if area else None,
        labels={_label(hass, label) for label in labels},
    )
    hass.states.async_set(entity_id, "on")


async def _set_up_assist(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "homeassistant", {})


async def _search(
    ws: MockHAClientWebSocket, visibility: dict[str, Any] | None = None
) -> dict[str, Any]:
    params: dict[str, Any] = {"search_types": ["entity"]}
    if visibility is not None:
        params["visibility"] = visibility
    result: dict[str, Any] = await ws_result(ws, _SEARCH, **params)
    return result


def _ids(result: dict[str, Any]) -> set[str]:
    return {e["entity_id"] for e in result["entities"]}


@pytest.mark.parametrize("visibility", [None, {}], ids=["absent", "empty"])
async def test_search_without_an_active_filter_hides_nothing(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    entry: MockConfigEntry,
    visibility: dict[str, Any] | None,
) -> None:
    _register(hass, entry, "light.a")
    hass.states.async_set("light.states_only", "on")

    result = await _search(component_ws, visibility)

    assert _ids(result) == {"light.a", "light.states_only"}
    assert result["entity_total_matches"] == 2
    assert "visibility_warnings" not in result


async def test_denied_entity_is_dropped_from_results_and_count(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """A deny needs no registry entry: a states-only entity is hidden too, and
    is not counted in ``entity_total_matches``."""
    hass.states.async_set("light.a", "on")
    hass.states.async_set("light.b", "on")

    result = await _search(component_ws, {"deny_entity_ids": ["light.a"]})

    assert _ids(result) == {"light.b"}
    assert result["entity_total_matches"] == 1


async def test_excluded_categories_hide_diagnostic_and_config_entities(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, entry: MockConfigEntry
) -> None:
    _register(hass, entry, "sensor.diag", category=EntityCategory.DIAGNOSTIC)
    _register(hass, entry, "sensor.cfg", category=EntityCategory.CONFIG)
    _register(hass, entry, "light.a")

    result = await _search(
        component_ws, {"exclude_categories": ["diagnostic", "config"]}
    )

    assert _ids(result) == {"light.a"}


async def test_unknown_excluded_category_hides_nothing(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, entry: MockConfigEntry
) -> None:
    _register(hass, entry, "sensor.diag", category=EntityCategory.DIAGNOSTIC)
    _register(hass, entry, "sensor.cfg", category=EntityCategory.CONFIG)
    _register(hass, entry, "light.a")

    result = await _search(component_ws, {"exclude_categories": ["bogus"]})

    assert _ids(result) == {"sensor.diag", "sensor.cfg", "light.a"}


async def test_exclude_hidden_drops_user_hidden_entities(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, entry: MockConfigEntry
) -> None:
    _register(hass, entry, "light.h", hidden=True)
    _register(hass, entry, "light.a")

    result = await _search(component_ws, {"exclude_hidden": True})

    assert _ids(result) == {"light.a"}


async def test_excluded_area_hides_direct_and_device_inherited_entities(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, entry: MockConfigEntry
) -> None:
    device_id = _device(hass, entry, "d1", area="garage")
    _register(hass, entry, "light.direct", area="garage")
    _register(hass, entry, "light.viadev", device_id=device_id)
    _register(hass, entry, "light.keep", area="office")

    result = await _search(component_ws, {"exclude_areas": ["garage"]})

    assert _ids(result) == {"light.keep"}


async def test_excluded_label_hides_direct_and_device_inherited_entities(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, entry: MockConfigEntry
) -> None:
    device_id = _device(hass, entry, "d1", labels=("hide",))
    _register(hass, entry, "light.direct", labels=("hide",))
    _register(hass, entry, "light.viadev", device_id=device_id)
    _register(hass, entry, "light.keep", labels=("show",))

    result = await _search(component_ws, {"exclude_labels": ["hide"]})

    assert _ids(result) == {"light.keep"}


async def test_allow_entity_ids_hides_every_other_entity_including_states_only(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, entry: MockConfigEntry
) -> None:
    _register(hass, entry, "light.keep")
    _register(hass, entry, "light.drop")
    hass.states.async_set("light.states_only", "on")

    result = await _search(component_ws, {"allow_entity_ids": ["light.keep"]})

    assert _ids(result) == {"light.keep"}


async def test_allow_areas_hides_entities_in_other_areas(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, entry: MockConfigEntry
) -> None:
    _register(hass, entry, "light.keep", area="office")
    _register(hass, entry, "light.drop", area="garage")

    result = await _search(component_ws, {"allow_areas": ["office"]})

    assert _ids(result) == {"light.keep"}


async def test_area_allowlist_over_an_empty_registry_does_not_blank_the_search(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """No registered entity can match an area, so the area allowlist is
    skipped rather than hiding every states-only entity."""
    hass.states.async_set("light.a", "on")
    hass.states.async_set("light.b", "on")

    result = await _search(component_ws, {"allow_areas": ["office"]})

    assert _ids(result) == {"light.a", "light.b"}


async def test_skipped_area_allowlist_does_not_bypass_assist_exposure(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """The wire opts into allowlist authorization, but the only allow
    dimension was skipped over an empty registry: no allowlist is left to
    authorize past Assist, so an unexposed entity stays hidden."""
    await _set_up_assist(hass)
    hass.states.async_set("sensor.exposed", "on")
    hass.states.async_set("sensor.unexposed", "on")
    async_expose_entity(hass, "conversation", "sensor.exposed", True)
    async_expose_entity(hass, "conversation", "sensor.unexposed", False)

    result = await _search(
        component_ws,
        {
            "allow_areas": ["kitchen"],
            "respect_assist_exposure": True,
            "allowlist_authorization": True,
        },
    )

    assert _ids(result) == {"sensor.exposed"}


async def test_allow_labels_keeps_direct_and_device_labelled_entities(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, entry: MockConfigEntry
) -> None:
    device_id = _device(hass, entry, "d1", labels=("important",))
    _register(hass, entry, "light.keep", labels=("important",))
    _register(hass, entry, "light.viadev", device_id=device_id)
    _register(hass, entry, "light.drop", labels=("other",))

    result = await _search(component_ws, {"allow_labels": ["important"]})

    assert _ids(result) == {"light.keep", "light.viadev"}


async def test_denied_entity_stays_hidden_when_its_area_is_allowed(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, entry: MockConfigEntry
) -> None:
    _register(hass, entry, "light.both", area="office")
    _register(hass, entry, "light.keep", area="office")

    result = await _search(
        component_ws,
        {"deny_entity_ids": ["light.both"], "allow_areas": ["office"]},
    )

    assert _ids(result) == {"light.keep"}


async def test_allowlisted_entity_passes_category_hidden_and_assist_filters(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, entry: MockConfigEntry
) -> None:
    """With ``allowlist_authorization`` an allow match authorizes the entity
    past the broad category, hidden and Assist filters."""
    await _set_up_assist(hass)
    _register(
        hass, entry, "sensor.allowed", category=EntityCategory.DIAGNOSTIC, hidden=True
    )
    _register(hass, entry, "sensor.drop")
    async_expose_entity(hass, "conversation", "sensor.allowed", False)

    result = await _search(
        component_ws,
        {
            "exclude_categories": ["diagnostic"],
            "exclude_hidden": True,
            "allow_entity_ids": ["sensor.allowed"],
            "respect_assist_exposure": True,
            "allowlist_authorization": True,
        },
    )

    assert _ids(result) == {"sensor.allowed"}


async def test_legacy_wire_allowlisted_entity_is_still_hidden_by_its_category(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, entry: MockConfigEntry
) -> None:
    """Without ``allowlist_authorization`` (an older server) an allow match
    still faces the category filter, as that server's own scan does."""
    _register(hass, entry, "sensor.allowed", category=EntityCategory.DIAGNOSTIC)
    _register(hass, entry, "sensor.drop")

    result = await _search(
        component_ws,
        {
            "exclude_categories": ["diagnostic"],
            "allow_entity_ids": ["sensor.allowed"],
        },
    )

    assert _ids(result) == set()


async def test_legacy_wire_allowlisted_entity_is_still_hidden_by_assist(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, entry: MockConfigEntry
) -> None:
    await _set_up_assist(hass)
    _register(hass, entry, "light.allowed")
    _register(hass, entry, "light.drop")
    async_expose_entity(hass, "conversation", "light.allowed", False)

    result = await _search(
        component_ws,
        {"allow_entity_ids": ["light.allowed"], "respect_assist_exposure": True},
    )

    assert _ids(result) == set()
    assert "visibility_warnings" not in result


async def test_excluded_area_wins_over_allow_entity_ids(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, entry: MockConfigEntry
) -> None:
    _register(hass, entry, "light.conflict", area="office")
    _register(hass, entry, "light.allowed", area="kitchen")

    result = await _search(
        component_ws,
        {
            "exclude_areas": ["office"],
            "allow_entity_ids": ["light.conflict", "light.allowed"],
        },
    )

    assert _ids(result) == {"light.allowed"}


async def test_allowlist_keeps_entities_through_their_device_area_and_label(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, entry: MockConfigEntry
) -> None:
    by_area = _device(hass, entry, "d1", area="office")
    by_label = _device(hass, entry, "d2", labels=("important",))
    _register(hass, entry, "light.viadev_area", device_id=by_area)
    _register(hass, entry, "light.viadev_label", device_id=by_label)
    _register(hass, entry, "light.drop")

    result = await _search(
        component_ws, {"allow_areas": ["office"], "allow_labels": ["important"]}
    )

    assert _ids(result) == {"light.viadev_area", "light.viadev_label"}


async def test_assist_exposure_hides_unexposed_entities(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, entry: MockConfigEntry
) -> None:
    await _set_up_assist(hass)
    _register(hass, entry, "light.a")
    _register(hass, entry, "light.b")
    async_expose_entity(hass, "conversation", "light.a", False)
    async_expose_entity(hass, "conversation", "light.b", True)

    result = await _search(component_ws, {"respect_assist_exposure": True})

    assert _ids(result) == {"light.b"}


async def test_assist_exposure_is_ignored_when_not_requested(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, entry: MockConfigEntry
) -> None:
    await _set_up_assist(hass)
    _register(hass, entry, "light.a")
    async_expose_entity(hass, "conversation", "light.a", False)

    result = await _search(component_ws, {"deny_entity_ids": ["light.absent"]})

    assert _ids(result) == {"light.a"}


async def test_unavailable_assist_exposure_hides_nothing_and_warns(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, entry: MockConfigEntry
) -> None:
    """Core has no Assist exposure (``homeassistant`` is not set up): the
    dimension is skipped and the reply says so."""
    _register(hass, entry, "light.a")
    _register(hass, entry, "sensor.b")

    result = await _search(component_ws, {"respect_assist_exposure": True})

    assert _ids(result) == {"light.a", "sensor.b"}
    assert len(result["visibility_warnings"]) == 1
    assert _ASSIST_UNAVAILABLE in result["visibility_warnings"][0]


async def test_search_enumerates_the_entity_registry_once(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    entity_registry: er.EntityRegistry,
    entry: MockConfigEntry,
) -> None:
    """The hidden set and the warnings share one registry inventory, so a
    large registry is walked once per search, not once per consumer."""
    _register(hass, entry, "light.a")
    _register(hass, entry, "light.b")
    entities = entity_registry.entities

    with patch.object(entities, "values", wraps=entities.values) as values:
        result = await _search(
            component_ws,
            {
                "allow_entity_ids": ["light.a"],
                "respect_assist_exposure": True,
                "allowlist_authorization": True,
            },
        )

    assert _ids(result) == {"light.a"}
    assert values.call_count == 1


async def test_search_accepts_every_visibility_key_the_server_sends(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    hass.states.async_set("light.a", "on")
    visibility = {
        "exclude_categories": ["config"],
        "deny_entity_ids": ["light.denied"],
        "exclude_areas": ["office"],
        "exclude_labels": ["lb"],
        "allow_entity_ids": ["light.a"],
        "allow_areas": ["kitchen"],
        "allow_labels": ["lb2"],
        "exclude_hidden": True,
        "respect_assist_exposure": True,
        "allowlist_authorization": True,
    }

    result = await _search(component_ws, visibility)

    assert _ids(result) == {"light.a"}


@pytest.mark.parametrize(
    "visibility",
    [{"exclude_area": ["x"]}, "nope"],
    ids=["unknown_key", "not_a_dict"],
)
async def test_malformed_visibility_is_refused_not_ignored(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, visibility: Any
) -> None:
    """A typo'd or newer-server dimension fails loudly (the server then falls
    back to its own filtered path) instead of being dropped silently."""
    hass.states.async_set("light.a", "on")

    reply = await ws_call(component_ws, _SEARCH, visibility=visibility)

    assert reply["success"] is False
    assert reply["error"]["code"] == "invalid_format"
