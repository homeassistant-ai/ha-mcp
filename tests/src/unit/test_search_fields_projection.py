"""``fields=`` / ``result_fields=`` projection of ``ha_search`` on real Core (#1199).

``ha_search`` runs on the server's own client against a real ``hass`` without
the component, so every entity branch is the server's legacy search reading
Core's states and registries. ``project_fields`` is the shared response helper
behind the other tools' ``fields=``.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from homeassistant.components import websocket_api
from homeassistant.components.config import area_registry as config_areas
from homeassistant.components.config import device_registry as config_devices
from homeassistant.components.config import entity_registry as config_entities
from homeassistant.components.config import floor_registry as config_floors
from homeassistant.components.config import label_registry as config_labels
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.response_helpers import project_fields

from ._core_setup import add_test_entry
from ._search_tool import _build_ha_search

GROUP_MEMBERS = ["light.member_two", "light.member_one"]
KITCHEN_GROUP = {
    "entity_id": "light.kitchen",
    "is_group": True,
    "member_entity_ids": sorted(GROUP_MEMBERS),
}
MEMBERSHIP = ["entity_id", "is_group", "member_entity_ids"]


# ---------------------------------------------------------------------------
# project_fields: the shared top-level projection helper
# ---------------------------------------------------------------------------


def test_project_fields_without_fields_returns_the_response_untouched() -> None:
    data = {"success": True, "results": [1, 2], "count": 2}
    assert project_fields(data, None) is data


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param(["results", "count"], id="list"),
        pytest.param("results,count", id="csv-string"),
        pytest.param('["results", "count"]', id="json-array-string"),
    ],
)
def test_project_fields_keeps_requested_keys_and_success(fields: Any) -> None:
    """Every accepted form of ``fields`` narrows to the named keys, and
    ``success`` survives without being named."""
    data = {"success": True, "results": [1, 2], "count": 2, "query": "light"}
    result = project_fields(data, fields)
    assert result == {"success": True, "results": [1, 2], "count": 2}


def test_project_fields_empty_list_keeps_only_success() -> None:
    data = {"success": True, "results": [], "count": 0}
    assert project_fields(data, []) == {"success": True}


def test_project_fields_does_not_strip_the_callers_response() -> None:
    data = {"success": True, "results": [1], "count": 1}
    project_fields(data, ["results"])
    assert data == {"success": True, "results": [1], "count": 1}


def test_project_fields_never_hides_existing_warnings() -> None:
    """A narrowing caller still sees the diagnostics already in the response."""
    data = {"success": True, "results": [], "count": 0, "warnings": ["bad field"]}
    result = project_fields(data, ["results"])
    assert result["warnings"] == ["bad field"]


# ---------------------------------------------------------------------------
# The home the tool tests search
# ---------------------------------------------------------------------------


def _add(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    entity_id: str,
    state: str,
    name: str,
    *,
    area_id: str | None = None,
    device_id: str | None = None,
    members: list[str] | None = None,
    hidden: bool = False,
) -> None:
    """Register ``entity_id`` in Core and give it a state."""
    domain, object_id = entity_id.split(".")
    entity_registry.async_get_or_create(
        domain,
        "test",
        object_id,
        suggested_object_id=object_id,
        device_id=device_id,
        original_name=name,
    )
    updates: dict[str, Any] = {}
    if area_id is not None:
        updates["area_id"] = area_id
    if hidden:
        updates["hidden_by"] = er.RegistryEntryHider.USER
    if updates:
        entity_registry.async_update_entity(entity_id, **updates)
    attributes: dict[str, Any] = {"friendly_name": name}
    if members is not None:
        attributes["group_entities"] = members
    hass.states.async_set(entity_id, state, attributes)


@pytest.fixture
async def config(hass: HomeAssistant) -> None:
    """``config`` answers the registry commands the search reads."""
    assert await async_setup_component(hass, "config", {})


@pytest.fixture
def search(core_client: HomeAssistantClient) -> Any:
    """``ha_search`` on the server's client, without the component."""
    return _build_ha_search(core_client)


@pytest.fixture
def home(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """``light.kitchen`` (a group, on) in the Kitchen; ``light.bedroom`` (off),
    ``switch.fan`` (on) and ``switch.pump`` (off) in no area."""
    kitchen = area_registry.async_create("Kitchen")
    _add(
        hass,
        entity_registry,
        "light.kitchen",
        "on",
        "Kitchen Light",
        area_id=kitchen.id,
        members=GROUP_MEMBERS,
    )
    _add(hass, entity_registry, "light.bedroom", "off", "Bedroom Light")
    _add(hass, entity_registry, "switch.fan", "on", "Fan Switch")
    _add(hass, entity_registry, "switch.pump", "off", "Pump Switch")


def _ids(records: list[dict[str, Any]]) -> set[str]:
    return {record["entity_id"] for record in records}


def _record(result: dict[str, Any], entity_id: str) -> dict[str, Any]:
    return next(r for r in result["entities"] if r.get("entity_id") == entity_id)


def _warned(result: dict[str, Any], text: str) -> bool:
    return any(text in warning for warning in result.get("warnings", []))


# ---------------------------------------------------------------------------
# fields=: top-level projection of the response
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("config", "home")
async def test_fields_drops_the_buckets_not_asked_for(search: Any) -> None:
    """``fields=["entities"]`` keeps the entity bucket and drops the config
    buckets an unprojected search returns."""
    full = await search(query="kitchen")
    projected = await search(query="kitchen", fields=["entities"])

    assert {"entities", "automations", "scripts", "scenes", "helpers"} <= set(full)
    assert _ids(projected["entities"]) == {"light.kitchen"}
    assert not {"automations", "scripts", "scenes", "helpers"} & set(projected)


@pytest.mark.usefixtures("config", "home")
async def test_fields_never_hides_success_and_pagination_keys(search: Any) -> None:
    """A narrowing caller still gets ``success`` and the pagination /
    completeness keys, so it cannot lose the signal that results are partial."""
    data = await search(query="kitchen", fields=["entities"])

    assert data["success"] is True
    assert "search_types" in data
    assert "count" in data


@pytest.mark.usefixtures("config", "home")
async def test_fields_typo_is_reported_not_an_empty_answer(search: Any) -> None:
    data = await search(query="kitchen", fields=["frobnicate"])
    assert _warned(data, "frobnicate")


@pytest.mark.usefixtures("config", "home")
@pytest.mark.parametrize(
    "fields", [pytest.param(123, id="not-a-list"), pytest.param('["', id="bad-json")]
)
async def test_unreadable_fields_is_rejected(search: Any, fields: Any) -> None:
    with pytest.raises(ToolError):
        await search(query="kitchen", fields=fields)


@pytest.mark.usefixtures("config", "home")
@pytest.mark.parametrize(
    ("kwargs", "search_type", "found"),
    [
        pytest.param(
            {"query": "kitchen", "area_filter": "kitchen"},
            "area_filtered_query",
            True,
            id="area-and-query",
        ),
        pytest.param({"area_filter": "kitchen"}, "area_only", True, id="area-only"),
        pytest.param(
            {"area_filter": "nonexistent"}, "area_only", False, id="unknown-area"
        ),
        pytest.param(
            {"domain_filter": "light"}, "domain_listing", True, id="domain-only"
        ),
    ],
)
async def test_fields_projection_reaches_every_entity_branch(
    search: Any, kwargs: dict[str, Any], search_type: str, found: bool
) -> None:
    """Each entity branch's response is projected: ``entities`` stays when
    asked for and goes when not."""
    full = await search(**kwargs)
    kept = await search(**kwargs, fields=["entities"])
    dropped = await search(**kwargs, fields=["success"])

    assert full["search_type"] == search_type
    assert bool(full["entities"]) is found
    assert "entities" in kept
    assert dropped["success"] is True
    assert "entities" not in dropped


@pytest.mark.usefixtures("config", "home")
async def test_area_listing_does_not_leak_internal_keys(search: Any) -> None:
    """Area records carry no ``_``-prefixed bookkeeping keys of the search."""
    data = await search(area_filter="kitchen")

    assert _ids(data["entities"]) == {"light.kitchen"}
    leaked = [key for record in data["entities"] for key in record if key[0] == "_"]
    assert not leaked


# ---------------------------------------------------------------------------
# Membership (is_group / member_entity_ids) across the legacy branches
# ---------------------------------------------------------------------------

_LEGACY_MODES = [
    pytest.param({"query": "kitchen", "area_filter": "kitchen"}, id="area-and-query"),
    pytest.param({"area_filter": "kitchen"}, id="area-only"),
    pytest.param({"domain_filter": "light"}, id="domain-listing"),
    pytest.param({"state_filter": "on"}, id="state-listing"),
]


@pytest.mark.usefixtures("config", "home")
@pytest.mark.parametrize("kwargs", _LEGACY_MODES)
async def test_membership_is_served_on_request_in_every_listing_mode(
    search: Any, kwargs: dict[str, Any]
) -> None:
    """A model told to check group membership before an "all except" command
    gets it whichever way it found the group, and only when it asks."""
    asked = await search(**kwargs, result_fields=MEMBERSHIP)
    default = await search(**kwargs)

    assert _record(asked, "light.kitchen") == KITCHEN_GROUP
    record = _record(default, "light.kitchen")
    assert "is_group" not in record
    assert "member_entity_ids" not in record


@pytest.mark.usefixtures("config", "home")
async def test_member_only_projection_keeps_the_group_flag(search: Any) -> None:
    """Asking only for member ids still says which record is a group, so an
    entity without members is not mistaken for one whose members were withheld."""
    result = await search(
        domain_filter="light",
        group_by_domain=True,
        result_fields=["member_entity_ids"],
        limit=20,
    )
    fan = await search(query="fan", result_fields=["member_entity_ids"])

    group = {"is_group": True, "member_entity_ids": sorted(GROUP_MEMBERS)}
    assert group in result["entities"]
    assert group in result["by_domain"]["light"]
    assert {"is_group": False} in result["by_domain"]["light"]
    assert fan["entities"] == [{"is_group": False}]
    assert not _warned(fan, "matched no record keys")


@pytest.fixture
def hidden_member_home(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """``light.group`` in the Living room groups a visible member and one the
    user hid."""
    living = area_registry.async_create("Living")
    _add(
        hass,
        entity_registry,
        "light.group",
        "on",
        "Group",
        area_id=living.id,
        members=["light.visible_member", "light.hidden_member"],
    )
    _add(
        hass,
        entity_registry,
        "light.visible_member",
        "on",
        "Visible Member",
        area_id=living.id,
    )
    _add(
        hass,
        entity_registry,
        "light.hidden_member",
        "on",
        "Hidden Member",
        area_id=living.id,
        hidden=True,
    )


_REDACTED_GROUP = {"entity_id": "light.group", "is_group": True}


@pytest.mark.usefixtures("config", "hidden_member_home")
async def test_exact_search_withholds_members_of_a_group_with_a_hidden_member(
    search: Any,
) -> None:
    """With hidden entities excluded, a group naming one keeps its group flag
    but not its member list; with them included the list is served."""
    redacted = await search(
        query="group", include_hidden=False, result_fields=MEMBERSHIP
    )
    served = await search(query="group", include_hidden=True, result_fields=MEMBERSHIP)

    assert redacted["entities"] == [_REDACTED_GROUP]
    assert _record(served, "light.group")["member_entity_ids"] == [
        "light.hidden_member",
        "light.visible_member",
    ]


@pytest.mark.usefixtures("config", "hidden_member_home")
async def test_fuzzy_search_withholds_members_of_a_group_with_a_hidden_member(
    search: Any,
) -> None:
    result = await search(
        query="group",
        exact_match=False,
        include_hidden=False,
        result_fields=MEMBERSHIP,
    )
    assert result["entities"] == [_REDACTED_GROUP]


@pytest.mark.usefixtures("config", "hidden_member_home")
async def test_area_listing_withholds_members_of_a_group_with_a_hidden_member(
    search: Any,
) -> None:
    result = await search(
        area_filter="Living", include_hidden=False, result_fields=MEMBERSHIP
    )
    assert _record(result, "light.group") == _REDACTED_GROUP
    assert "light.hidden_member" not in _ids(result["entities"])


# ---------------------------------------------------------------------------
# group_by_domain / per_domain_limit
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("config", "home")
@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"query": "light"}, id="exact-match"),
        pytest.param({"domain_filter": "light"}, id="domain-listing"),
    ],
)
async def test_per_domain_limit_caps_each_domain_bucket(
    search: Any, kwargs: dict[str, Any]
) -> None:
    uncapped = await search(**kwargs, group_by_domain=True, limit=20)
    capped = await search(**kwargs, group_by_domain=True, per_domain_limit=1, limit=20)

    assert len(uncapped["by_domain"]["light"]) == 2
    assert len(capped["by_domain"]["light"]) == 1


@pytest.mark.usefixtures("config", "home")
async def test_per_domain_limit_without_grouping_changes_nothing(search: Any) -> None:
    result = await search(query="light", group_by_domain=False, per_domain_limit=1)

    assert _ids(result["entities"]) == {"light.kitchen", "light.bedroom"}
    assert "by_domain" not in result


@pytest.mark.usefixtures("config", "home")
async def test_state_only_listing_groups_matches_across_domains(search: Any) -> None:
    result = await search(state_filter="on", group_by_domain=True, limit=20)

    assert result["search_type"] == "state_listing"
    assert {
        domain: _ids(records) for domain, records in result["by_domain"].items()
    } == {"light": {"light.kitchen"}, "switch": {"switch.fan"}}


# ---------------------------------------------------------------------------
# state_filter
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("config", "home")
@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"query": "light"}, id="exact-match"),
        pytest.param({"domain_filter": "light"}, id="domain-listing"),
    ],
)
async def test_state_filter_keeps_only_entities_in_that_state(
    search: Any, kwargs: dict[str, Any]
) -> None:
    """The filter narrows the entities, and the caller's own input is not
    echoed back at the top level."""
    result = await search(**kwargs, state_filter="on")

    assert _ids(result["entities"]) == {"light.kitchen"}
    assert "state_filter" not in result


@pytest.mark.usefixtures("config", "home")
async def test_state_filter_ignores_surrounding_whitespace(search: Any) -> None:
    padded = await search(query="light", state_filter="  on  ")
    assert _ids(padded["entities"]) == {"light.kitchen"}


@pytest.mark.usefixtures("config", "home")
async def test_whitespace_only_state_filter_filters_nothing(search: Any) -> None:
    result = await search(query="light", state_filter="   ")
    assert _ids(result["entities"]) == {"light.kitchen", "light.bedroom"}


@pytest.mark.usefixtures("config", "home")
async def test_fuzzy_state_filter_reports_the_unfiltered_total_and_says_so(
    search: Any,
) -> None:
    """The fuzzy engine pages before the state filter runs, so
    ``entity_total_matches`` stays the unfiltered count while ``count`` is
    what survived the filter, and a note explains the difference."""
    unfiltered = await search(query="light", exact_match=False)
    filtered = await search(query="light", exact_match=False, state_filter="on")

    assert _ids(filtered["entities"]) == {"light.kitchen"}
    assert filtered["count"] == 1
    assert filtered["entity_total_matches"] == unfiltered["entity_total_matches"] > 1
    assert "has_more" in filtered["state_filter_note"]


@pytest.mark.usefixtures("config", "home")
async def test_state_filter_note_survives_fields_projection(search: Any) -> None:
    result = await search(
        query="light", exact_match=False, state_filter="on", fields=["entities"]
    )

    assert "entities" in result
    assert "state_filter_note" in result
    assert "entity_total_matches" in result


# ---------------------------------------------------------------------------
# result_fields: per-record projection and enrichment
# ---------------------------------------------------------------------------


def test_tool_description_tells_models_to_check_membership_before_exclusions(
    search: Any,
) -> None:
    description = search.__doc__ or ""

    assert 'control requests with exclusions such as "except"' in description
    assert "include `is_group` and `member_entity_ids`" in description
    assert "prefer leaf entities" in description


@pytest.mark.usefixtures("config", "home")
@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"query": "light"}, id="exact-match"),
        pytest.param({"domain_filter": "light"}, id="domain-listing"),
    ],
)
async def test_result_fields_narrows_each_record_only(
    search: Any, kwargs: dict[str, Any]
) -> None:
    """Each record keeps just the named keys; the envelope around them is
    untouched."""
    result = await search(**kwargs, result_fields=["entity_id", "state"])

    assert result["entities"]
    assert all(set(r) == {"entity_id", "state"} for r in result["entities"])
    assert {"success", "entity_total_matches", "count"} <= set(result)


@pytest.mark.usefixtures("config", "home")
async def test_unknown_result_fields_key_is_rejected(search: Any) -> None:
    """An unknown key is refused instead of projecting every record to ``{}``."""
    with pytest.raises(ToolError, match="Unknown result_fields"):
        await search(query="light", result_fields=["nonexistent_key"])


@pytest.mark.usefixtures("config", "home")
async def test_area_enrichment_names_each_entitys_area_without_warning(
    search: Any,
) -> None:
    data = await search(query="light", result_fields=["entity_id", "area"])

    assert _record(data, "light.kitchen")["area"] == "Kitchen"
    assert _record(data, "light.bedroom")["area"] is None
    assert not _warned(data, "enrichment incomplete")


@pytest.mark.usefixtures("home")
async def test_area_registry_refused_during_enrichment_is_reported(
    hass: HomeAssistant, search: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Core without its area registry commands refuses that read: the records
    still come back, and the empty area is flagged instead of reading as
    "in no area"."""
    for section in (config_devices, config_entities, config_floors, config_labels):
        assert section.async_setup(hass)

    with caplog.at_level(logging.WARNING):
        data = await search(query="light", result_fields=["entity_id", "area"])

    assert _ids(data["entities"]) == {"light.kitchen", "light.bedroom"}
    assert _record(data, "light.kitchen")["area"] is None
    assert _warned(data, "enrichment incomplete")
    assert "result_fields_enrichment_failed" in caplog.text


@pytest.mark.usefixtures("home")
async def test_refused_registry_entries_read_on_area_query_is_reported(
    hass: HomeAssistant, search: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """The area+query branch's entry read is refused by Core: the enrichment
    reports it rather than taking the failed read for "no entries"."""
    for section in (config_areas, config_devices, config_floors, config_labels):
        assert section.async_setup(hass)
    websocket_api.async_register_command(hass, config_entities.websocket_list_entities)

    with caplog.at_level(logging.WARNING):
        data = await search(
            query="kitchen", area_filter="kitchen", result_fields=["entity_id", "area"]
        )

    assert data["search_type"] == "area_filtered_query"
    assert _ids(data["entities"]) == {"light.kitchen"}
    assert _warned(data, "enrichment incomplete")
    assert "result_fields_enrichment_failed" in caplog.text


@pytest.mark.usefixtures("config")
async def test_computed_name_alias_is_not_published_as_none(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, search: Any
) -> None:
    """Core sends its "computed name" alias as ``null``; the aliases served
    are the real ones, sorted, never the string ``"None"``."""
    _add(hass, entity_registry, "light.lamp", "on", "Lamp")
    entity_registry.async_update_entity(
        "light.lamp", aliases=[er.COMPUTED_NAME, "Kitchen", "Cocina"]
    )

    data = await search(query="lamp", result_fields=["entity_id", "aliases"])

    assert data["entities"] == [
        {"entity_id": "light.lamp", "aliases": ["Cocina", "Kitchen"]}
    ]


@pytest.mark.usefixtures("config")
async def test_entity_without_registry_entry_has_no_aliases(
    hass: HomeAssistant, search: Any
) -> None:
    hass.states.async_set("light.loose", "on", {"friendly_name": "Loose"})

    data = await search(query="loose", result_fields=["entity_id", "aliases"])

    assert data["entities"] == [{"entity_id": "light.loose", "aliases": []}]


@pytest.fixture
def office(area_registry: ar.AreaRegistry, floor_registry: fr.FloorRegistry) -> str:
    """The Office area on the Upstairs floor; returns its id."""
    upstairs = floor_registry.async_create("Upstairs")
    return area_registry.async_create("Office", floor_id=upstairs.floor_id).id


@pytest.mark.usefixtures("config")
async def test_entity_on_child_device_is_placed_by_parent_devices_area(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    office: str,
    search: Any,
) -> None:
    entry = add_test_entry(hass)
    parent = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("test", "parent")}
    )
    device_registry.async_update_device(parent.id, area_id=office)
    child = device_registry.async_get_or_create_child(
        config_entry_id=entry.entry_id,
        identifiers={("test", "child")},
        parent_device_id=parent.id,
    )
    _add(hass, entity_registry, "sensor.child", "1", "Child", device_id=child.id)

    data = await search(query="child", result_fields=["area", "floor"])

    assert data["entities"] == [{"area": "Office", "floor": "Upstairs"}]


@pytest.mark.usefixtures("config")
async def test_empty_entity_area_does_not_inherit_its_devices_area(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    office: str,
    search: Any,
) -> None:
    """Core stores an entity's area as ``""`` when it is cleared to empty; that
    entity is not reported in its device's area."""
    entry = add_test_entry(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("test", "device")}
    )
    device_registry.async_update_device(device.id, area_id=office)
    _add(hass, entity_registry, "sensor.probe", "1", "Probe", device_id=device.id)
    entity_registry.async_update_entity("sensor.probe", area_id="")

    data = await search(query="probe", result_fields=["area", "floor"])

    assert data["entities"] == [{"area": None, "floor": None}]
