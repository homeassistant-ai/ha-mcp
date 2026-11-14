"""The component's search warns about every visibility dimension it skips, and
reads Assist exposure the way Core resolves it without writing it back.

Each test sets up Core's registries, states and Assist exposure through Core's
API and sends ``ha_mcp_tools/search`` with its ``visibility`` param over Core's
WebSocket API. Assist exposure is available only when the ``homeassistant``
integration is set up.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.components.homeassistant import exposed_entities
from homeassistant.components.homeassistant.exposed_entities import (
    async_expose_entity,
    async_get_entity_settings,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.typing import MockHAClientWebSocket

from ._component_core import ws_result
from ._core_setup import add_test_entry

_SEARCH = "ha_mcp_tools/search"
_ASSIST_UNAVAILABLE = "Assist exposure data was unavailable"
_REGISTRY_EMPTY = "entity registry returned no entries"


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    """The config entry every registered entity belongs to."""
    return add_test_entry(hass)


def _register(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    entity_id: str,
    *,
    category: EntityCategory | None = None,
    area: str | None = None,
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
        entity_category=category,
    )
    assert row.entity_id == entity_id
    if area:
        area_id = ar.async_get(hass).async_create(area.title()).id
        entities.async_update_entity(entity_id, area_id=area_id)
    hass.states.async_set(entity_id, "on")


async def _set_up_assist(
    hass: HomeAssistant, ws: MockHAClientWebSocket, *, expose_new: bool = True
) -> None:
    """Core's Assist exposure, with new entities exposed or not."""
    assert await async_setup_component(hass, "homeassistant", {})
    await ws_result(
        ws,
        "homeassistant/expose_new_entities/set",
        assistant="conversation",
        expose_new=expose_new,
    )


async def _search(
    ws: MockHAClientWebSocket, visibility: dict[str, Any]
) -> dict[str, Any]:
    result: dict[str, Any] = await ws_result(
        ws, _SEARCH, search_types=["entity"], visibility=visibility
    )
    return result


def _ids(result: dict[str, Any]) -> set[str]:
    return {e["entity_id"] for e in result["entities"]}


# -----------------------------------------------------------------------------
# Warnings for a skipped dimension
# -----------------------------------------------------------------------------
async def test_unknown_excluded_category_is_named_in_a_warning(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, entry: MockConfigEntry
) -> None:
    _register(hass, entry, "sensor.x")

    result = await _search(
        component_ws, {"exclude_categories": ["bogus", "diagnostic"]}
    )

    assert len(result["visibility_warnings"]) == 1
    assert "exclude_categories ['bogus']" in result["visibility_warnings"][0]


async def test_known_excluded_categories_give_no_warning(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, entry: MockConfigEntry
) -> None:
    _register(hass, entry, "sensor.x")

    result = await _search(component_ws, {"exclude_categories": ["diagnostic"]})

    assert "visibility_warnings" not in result


async def test_area_allowlist_over_an_empty_registry_warns(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    hass.states.async_set("light.a", "on")

    result = await _search(component_ws, {"allow_areas": ["office"]})

    assert len(result["visibility_warnings"]) == 1
    assert _REGISTRY_EMPTY in result["visibility_warnings"][0]


async def test_area_allowlist_over_registered_entities_gives_no_warning(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, entry: MockConfigEntry
) -> None:
    _register(hass, entry, "light.a", area="office")

    result = await _search(component_ws, {"allow_areas": ["office"]})

    assert _ids(result) == {"light.a"}
    assert "visibility_warnings" not in result


async def test_allow_entity_ids_over_an_empty_registry_gives_no_warning(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """An entity-id allowlist needs no registry data, so nothing is skipped."""
    hass.states.async_set("light.a", "on")

    result = await _search(component_ws, {"allow_entity_ids": ["light.a"]})

    assert "visibility_warnings" not in result


async def test_available_assist_exposure_gives_no_warning(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, entry: MockConfigEntry
) -> None:
    await _set_up_assist(hass, component_ws)
    _register(hass, entry, "light.a")

    result = await _search(component_ws, {"respect_assist_exposure": True})

    assert "visibility_warnings" not in result


async def test_authorizing_allowlist_makes_unavailable_assist_no_degradation(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, entry: MockConfigEntry
) -> None:
    """An authorizing allowlist decides alone, so missing Assist exposure
    (``homeassistant`` not set up) costs nothing and is not reported."""
    _register(hass, entry, "light.a")
    _register(hass, entry, "light.b")

    result = await _search(
        component_ws,
        {
            "allow_entity_ids": ["light.a"],
            "respect_assist_exposure": True,
            "allowlist_authorization": True,
        },
    )

    assert _ids(result) == {"light.a"}
    assert "visibility_warnings" not in result


async def test_legacy_wire_allowlist_still_warns_on_unavailable_assist(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, entry: MockConfigEntry
) -> None:
    """Without ``allowlist_authorization`` Assist still applies beside the
    allowlist, so its absence is a degradation the operator must see."""
    _register(hass, entry, "light.a")

    result = await _search(
        component_ws,
        {"allow_entity_ids": ["light.a"], "respect_assist_exposure": True},
    )

    assert len(result["visibility_warnings"]) == 1
    assert _ASSIST_UNAVAILABLE in result["visibility_warnings"][0]


async def test_every_skipped_dimension_is_warned_about(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    hass.states.async_set("light.a", "on")

    result = await _search(
        component_ws,
        {
            "exclude_categories": ["bogus"],
            "allow_areas": ["office"],
            "respect_assist_exposure": True,
        },
    )

    warnings = result["visibility_warnings"]
    assert len(warnings) == 3
    for phrase in ("['bogus']", _REGISTRY_EMPTY, _ASSIST_UNAVAILABLE):
        assert any(phrase in w for w in warnings), phrase


# -----------------------------------------------------------------------------
# Assist exposure as Core resolves it
# -----------------------------------------------------------------------------
async def test_explicitly_exposed_entity_shows_when_new_entities_are_not_exposed(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, entry: MockConfigEntry
) -> None:
    await _set_up_assist(hass, component_ws, expose_new=False)
    _register(hass, entry, "light.a")
    async_expose_entity(hass, "conversation", "light.a", True)

    result = await _search(component_ws, {"respect_assist_exposure": True})

    assert _ids(result) == {"light.a"}


async def test_explicitly_unexposed_entity_hides_though_its_domain_is_exposed(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, entry: MockConfigEntry
) -> None:
    await _set_up_assist(hass, component_ws)
    _register(hass, entry, "light.a")
    _register(hass, entry, "light.b")
    async_expose_entity(hass, "conversation", "light.a", False)

    result = await _search(component_ws, {"respect_assist_exposure": True})

    assert _ids(result) == {"light.b"}


async def test_entity_without_a_setting_hides_when_new_entities_are_not_exposed(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, entry: MockConfigEntry
) -> None:
    await _set_up_assist(hass, component_ws, expose_new=False)
    _register(hass, entry, "light.a")

    result = await _search(component_ws, {"respect_assist_exposure": True})

    assert _ids(result) == set()


@pytest.mark.parametrize(
    ("entity_id", "category", "exposed"),
    [
        ("light.a", None, True),
        ("sensor.a", None, False),
        ("light.diag", EntityCategory.DIAGNOSTIC, False),
    ],
    ids=["default_domain", "no_device_class", "entity_category"],
)
async def test_entity_without_a_setting_follows_cores_default_exposure(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    entry: MockConfigEntry,
    entity_id: str,
    category: EntityCategory | None,
    exposed: bool,
) -> None:
    await _set_up_assist(hass, component_ws)
    _register(hass, entry, entity_id, category=category)

    result = await _search(component_ws, {"respect_assist_exposure": True})

    assert _ids(result) == ({entity_id} if exposed else set())


async def test_states_only_entity_without_a_setting_follows_default_exposure(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """Core knows no setting for an id outside the registry and the exposure
    store ("Unknown entity"); that is no override, not a failure, so the
    default decides."""
    await _set_up_assist(hass, component_ws)
    hass.states.async_set("light.states_only", "on")
    hass.states.async_set("sensor.states_only", "on")

    result = await _search(component_ws, {"respect_assist_exposure": True})

    assert _ids(result) == {"light.states_only"}


async def test_failing_exposure_read_hides_nothing(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _set_up_assist(hass, component_ws)
    _register(hass, entry, "light.a")
    async_expose_entity(hass, "conversation", "light.a", False)

    def _fail(*_args: Any) -> Any:
        raise RuntimeError("exposure store unreadable")

    monkeypatch.setattr(exposed_entities, "async_get_entity_settings", _fail)

    result = await _search(component_ws, {"respect_assist_exposure": True})

    assert _ids(result) == {"light.a"}


async def test_search_leaves_assist_exposure_settings_unchanged(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, entry: MockConfigEntry
) -> None:
    """Core's own should-expose check stores the default it computes; the
    search reads exposure only and stores nothing."""
    await _set_up_assist(hass, component_ws)
    _register(hass, entry, "light.a")
    _register(hass, entry, "sensor.a")
    hass.states.async_set("sensor.states_only", "on")

    result = await _search(component_ws, {"respect_assist_exposure": True})

    assert _ids(result) == {"light.a"}
    assert async_get_entity_settings(hass, "light.a") == {}
    assert async_get_entity_settings(hass, "sensor.a") == {}
    with pytest.raises(HomeAssistantError, match="Unknown entity"):
        async_get_entity_settings(hass, "sensor.states_only")
