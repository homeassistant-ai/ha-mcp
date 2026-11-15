"""Contract test for the component-search seam (ha_mcp_tools/search).

The component's search and the server's ``ha_search`` shaping were written
against the same design contract but never against each other's code. Here
``ha_search`` runs on the server's own client against a real ``hass`` that
answers the component's commands (``component_core_client``), and its records
are pinned to the legacy ``ha_search`` record shapes, so a vocabulary drift on
either side (``alias`` vs ``friendly_name``, ``options`` vs ``config``, id-key
renames) fails here instead of shipping.

Legacy per-bucket record keys (the contract being pinned; see
``smart_search/_deep.py`` / ``_scenes.py`` record builders):

- automations: entity_id, friendly_name, score, match_in_name,
  match_in_config [, config]
- scripts: + script_id
- scenes: + scene_id
- helpers (collection): entity_id, helper_type, name, score, match_in_name,
  match_in_config [, config]
- helpers (flow): entry_id instead of entity_id

``config`` is present only under ``include_config=True`` (mirroring the
legacy pipeline's pop), and YAML-backed records must never carry a body.
Scene records never carry a body either.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers import label_registry as lr
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.typing import MockHAClientWebSocket

from custom_components.ha_mcp_tools.websocket_api.constants import (
    ALL_SEARCH_TYPES,
    WS_SEARCH,
)
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import get_websocket_client
from ha_mcp.utils.entity_membership import normalize_member_entity_ids

from ._component_core import ws_result
from ._core_setup import add_test_entry, set_up_domain
from ._search_tool import _build_ha_search, _setup_visibility_disabled

AUTOMATION_KEYS = {
    "entity_id",
    "friendly_name",
    "score",
    "match_in_name",
    "match_in_config",
    # Discussion #2258: set when HA's own reference graph names this config as
    # referencing the queried entity. Emitted on EVERY config record, not only
    # graph hits, so a caller can read it without probing for the key -- the
    # component surface must carry it too or the two routes drift.
    "match_in_references",
}
SCRIPT_KEYS = AUTOMATION_KEYS | {"script_id"}
SCENE_KEYS = AUTOMATION_KEYS | {"scene_id"}
HELPER_COLLECTION_KEYS = {
    "entity_id",
    "helper_type",
    "name",
    "score",
    "match_in_name",
    "match_in_config",
    "match_in_references",
}
HELPER_FLOW_KEYS = HELPER_COLLECTION_KEYS - {"entity_id"} | {"entry_id"}
ENTITY_KEYS = {"entity_id", "friendly_name", "domain", "state", "score", "match_type"}
# The component path adds the name Core composes, which only it can ask for.
COMPONENT_ENTITY_KEYS = ENTITY_KEYS | {"name"}
SECRET = "hunter2-contractmarker-secret"


@pytest.fixture
def search(
    component_core_client: HomeAssistantClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., Any]:
    """``ha_search`` served by the component on ``hass``."""
    _setup_visibility_disabled(tmp_path, monkeypatch)
    return _build_ha_search(component_core_client)


@pytest.fixture
async def marker_home(hass: HomeAssistant) -> None:
    """A home where the query 'contractmarker' hits every surface once."""
    hass.states.async_set(
        "light.contractmarker_lamp", "on", {"friendly_name": "Contractmarker Lamp"}
    )
    await set_up_domain(
        hass, "input_boolean", {"contractmarker_flag": {"name": "Contractmarker Flag"}}
    )
    trigger = [{"trigger": "event", "event_type": "probe"}]
    await set_up_domain(
        hass,
        "automation",
        [
            {
                "id": "auto1",
                "alias": "Contractmarker Storage Automation",
                "triggers": trigger,
                "actions": [],
            },
            {
                "alias": "Yaml Automation",
                "triggers": trigger,
                "actions": [
                    {"action": "notify.contractmarker", "data": {"message": SECRET}}
                ],
            },
        ],
    )
    await set_up_domain(
        hass,
        "script",
        {"contractmarker_script": {"alias": "Contractmarker Script", "sequence": []}},
    )
    await set_up_domain(
        hass,
        "scene",
        [
            {
                "id": "scn1",
                "name": "Contractmarker Scene",
                "entities": {"light.contractmarker_lamp": "on"},
            }
        ],
    )
    MockConfigEntry(
        domain="template",
        entry_id="entry1",
        title="Contractmarker Template Helper",
        options={"template": "{{ 1 }}"},
        data={"credential": "should-never-appear"},
    ).add_to_hass(hass)


async def _marker_search(search: Callable[..., Any], include_config: bool) -> Any:
    return await search(
        query="contractmarker",
        exact_match=True,
        include_hidden=False,
        include_config=include_config,
        limit=10,
    )


def _records(shaped: dict[str, Any], bucket: str) -> list[dict[str, Any]]:
    recs = shaped.get(bucket)
    assert isinstance(recs, list) and recs, f"expected records in {bucket}: {shaped}"
    return recs


@pytest.mark.usefixtures("marker_home")
async def test_all_surfaces_match_and_shape_to_legacy_keys(
    search: Callable[..., Any],
) -> None:
    shaped = await _marker_search(search, include_config=True)

    assert shaped["success"] is True
    for rec in _records(shaped, "entities"):
        assert set(rec) == COMPONENT_ENTITY_KEYS, f"entity record keys drifted: {rec}"
    autos = _records(shaped, "automations")
    assert {r["entity_id"] for r in autos} == {
        "automation.contractmarker_storage_automation",
        "automation.yaml_automation",
    }
    for rec in autos:
        assert set(rec) == AUTOMATION_KEYS | {"config"}, rec
        assert rec["friendly_name"], "friendly_name must be populated from alias"
    for rec in _records(shaped, "scripts"):
        assert set(rec) == SCRIPT_KEYS | {"config"}, rec
        assert rec["script_id"] == "contractmarker_script"
    for rec in _records(shaped, "scenes"):
        assert set(rec) == SCENE_KEYS | {"config"}, rec
        assert rec["scene_id"] == "scn1"
        assert rec["config"] is None
    by_kind = {
        ("flow" if "entry_id" in r else "collection"): r
        for r in _records(shaped, "helpers")
    }
    assert set(by_kind["collection"]) == HELPER_COLLECTION_KEYS | {"config"}
    assert set(by_kind["flow"]) == HELPER_FLOW_KEYS | {"config"}
    # The flow helper's body rides `options` component-side, `config` here.
    assert by_kind["flow"]["config"] == {"template": "{{ 1 }}"}


@pytest.mark.usefixtures("marker_home")
async def test_a_yaml_body_is_matched_but_withheld(
    search: Callable[..., Any],
) -> None:
    shaped = await _marker_search(search, include_config=True)

    by_id = {r["entity_id"]: r for r in shaped["automations"]}
    yaml_rec = by_id["automation.yaml_automation"]
    assert (yaml_rec["match_in_config"], yaml_rec["config"]) == (True, None)
    assert SECRET not in repr(shaped)
    storage = by_id["automation.contractmarker_storage_automation"]["config"]
    assert (storage["id"], storage["alias"]) == (
        "auto1",
        "Contractmarker Storage Automation",
    )


@pytest.mark.usefixtures("marker_home")
async def test_without_include_config_no_record_carries_a_config_key(
    search: Callable[..., Any],
) -> None:
    """Mirrors the legacy pipeline's include_config pop; entry.data never
    surfaces, with or without include_config."""
    shaped = await _marker_search(search, include_config=False)

    for bucket in ("automations", "scripts", "scenes", "helpers"):
        for rec in _records(shaped, bucket):
            assert "config" not in rec, f"{bucket} record leaked config key: {rec}"
    assert "should-never-appear" not in repr(shaped)


@pytest.mark.usefixtures("marker_home")
async def test_the_envelope_has_the_legacy_keys(search: Callable[..., Any]) -> None:
    shaped = await _marker_search(search, include_config=False)

    for key in (
        "success",
        "query",
        "entities",
        "automations",
        "scripts",
        "scenes",
        "helpers",
        "entity_total_matches",
        "config_total_matches",
        "count",
        "offset",
        "limit",
        "has_more",
        "next_offset",
    ):
        assert key in shaped, f"envelope key missing: {key}"


async def test_every_public_search_type_but_dashboards_is_served_by_the_component(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    search: Callable[..., Any],
) -> None:
    """#2008: a type the server accepts but the component does not serve would
    route its whole call to legacy in silence. ``dashboard`` is served by the
    dashboards leg alongside the command (#2289)."""
    with pytest.raises(ToolError, match="Valid types") as refused:
        await search(query="x", search_types=["frobnicate"])
    message = str(refused.value)
    listed = message[message.index("Valid types: ") + len("Valid types: ") :]
    valid = set(ast.literal_eval(listed[: listed.index("]") + 1]))
    ws = await get_websocket_client(
        url=component_core_client.base_url,
        token=component_core_client.token,
        verify_ssl=False,
    )

    assert valid - {"dashboard"} == set(ALL_SEARCH_TYPES) - {"entity"}
    for search_type in sorted(valid - {"dashboard"}):
        with patch.object(ws, "send_command", wraps=ws.send_command) as send:
            await search(query="x", search_types=[search_type])
        assert WS_SEARCH in [c.args[0] for c in send.call_args_list], search_type


# --- entity records ----------------------------------------------------------------
@pytest.fixture
def enrich_marker(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    area_registry: ar.AreaRegistry,
    floor_registry: fr.FloorRegistry,
    label_registry: lr.LabelRegistry,
) -> Callable[[Any], None]:
    """``light.enrichmarker`` in the Office upstairs, labeled, with an alias;
    the returned setter gives its state the ``group_entities`` it is passed
    (``None``: none)."""
    upstairs = floor_registry.async_create("Upstairs")
    office = area_registry.async_create("Office", floor_id=upstairs.floor_id)
    label = label_registry.async_create("Favorites")
    entry = entity_registry.async_get_or_create(
        "light", "test", "enrichmarker", suggested_object_id="enrichmarker"
    )
    entity_registry.async_update_entity(
        entry.entity_id, aliases=["desk"], area_id=office.id, labels={label.label_id}
    )

    def set_members(members: Any) -> None:
        attributes: dict[str, Any] = {"friendly_name": "Enrichmarker"}
        if members is not None:
            attributes["group_entities"] = members
        hass.states.async_set("light.enrichmarker", "on", attributes)

    set_members(["light.two", "light.one"])
    return set_members


async def _entity(search: Callable[..., Any], **kwargs: Any) -> Any:
    shaped = await search(query="enrichmarker", exact_match=True, **kwargs)
    return shaped["entities"][0]


@pytest.mark.usefixtures("enrich_marker")
async def test_the_default_entity_record_is_the_base_keys_plus_the_name(
    search: Callable[..., Any],
) -> None:
    """Even though the component record already carries the enrichment join."""
    assert set(await _entity(search)) == COMPONENT_ENTITY_KEYS


@pytest.fixture
def sensor_on_a_device(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
    area_registry: ar.AreaRegistry,
    floor_registry: fr.FloorRegistry,
) -> None:
    """``sensor.enrichmarker`` on a child device in the Garage downstairs whose
    parent is in the Office upstairs."""
    upstairs = floor_registry.async_create("Upstairs")
    ground = floor_registry.async_create("Ground")
    office = area_registry.async_create("Office", floor_id=upstairs.floor_id)
    garage = area_registry.async_create("Garage", floor_id=ground.floor_id)
    config_entry = add_test_entry(hass)
    parent = device_registry.async_get_or_create(
        config_entry_id=config_entry.entry_id, identifiers={("test", "parent")}
    )
    device_registry.async_update_device(parent.id, area_id=office.id)
    child = device_registry.async_get_or_create_child(
        config_entry_id=config_entry.entry_id,
        identifiers={("test", "child")},
        parent_device_id=parent.id,
    )
    device_registry.async_update_child_device(child.id, area_id=garage.id)
    entity_registry.async_get_or_create(
        "sensor",
        "test",
        "enrichmarker",
        suggested_object_id="enrichmarker",
        device_id=child.id,
    )
    hass.states.async_set(
        "sensor.enrichmarker", "on", {"friendly_name": "Enrichmarker"}
    )


@pytest.mark.usefixtures("sensor_on_a_device")
async def test_a_child_devices_own_area_and_floor_reach_the_public_search(
    search: Callable[..., Any],
) -> None:
    """The child's direct area wins over its parent's, exactly as Core does."""
    record = await _entity(search, result_fields=["entity_id", "area", "floor"])

    assert record == {
        "entity_id": "sensor.enrichmarker",
        "area": "Garage",
        "floor": "Ground",
    }


async def test_a_device_held_twice_gives_no_area_to_the_public_search(
    hass: HomeAssistant,
    search: Callable[..., Any],
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
    area_registry: ar.AreaRegistry,
    floor_registry: fr.FloorRegistry,
) -> None:
    """Ambiguous device rows cannot assign an arbitrary area or floor."""
    upstairs = floor_registry.async_create("Upstairs")
    office = area_registry.async_create("Office", floor_id=upstairs.floor_id)
    garage = area_registry.async_create("Garage")
    device = device_registry.async_get_or_create(
        config_entry_id=add_test_entry(hass).entry_id,
        identifiers={("test", "dup")},
    )
    before = device_registry.async_update_device(device.id, area_id=office.id)
    after = device_registry.async_update_device(device.id, area_id=garage.id)
    entity_registry.async_get_or_create(
        "sensor",
        "test",
        "enrichmarker",
        suggested_object_id="enrichmarker",
        device_id=device.id,
    )
    hass.states.async_set(
        "sensor.enrichmarker", "on", {"friendly_name": "Enrichmarker"}
    )

    with patch.object(
        dr.ActiveDeviceRegistryItems, "values", return_value=[before, after]
    ):
        record = await _entity(search, result_fields=["entity_id", "area", "floor"])

    assert record == {"entity_id": "sensor.enrichmarker", "area": None, "floor": None}


@pytest.mark.parametrize(
    ("members", "expected"),
    [
        (
            ["light.two", "light.one"],
            {"is_group": True, "member_entity_ids": ["light.one", "light.two"]},
        ),
        # A leaf says is_group=False; absence is not rewritten to null.
        (None, {"is_group": False}),
        # An explicitly empty collection remains distinguishable from a leaf.
        ([], {"is_group": True, "member_entity_ids": []}),
    ],
    ids=["group", "leaf", "empty-group"],
)
async def test_membership_fields_are_opt_in_and_explicit(
    search: Callable[..., Any],
    enrich_marker: Callable[[Any], None],
    members: Any,
    expected: dict[str, Any],
) -> None:
    enrich_marker(members)

    default = await _entity(search)
    requested = await _entity(
        search, result_fields=["entity_id", "is_group", "member_entity_ids"]
    )

    assert "is_group" not in default
    assert requested == {"entity_id": "light.enrichmarker", **expected}


@pytest.mark.usefixtures("enrich_marker")
async def test_member_ids_never_lose_the_group_discriminator(
    search: Callable[..., Any],
) -> None:
    is_group = await _entity(search, result_fields=["entity_id", "is_group"])
    members = await _entity(search, result_fields=["entity_id", "member_entity_ids"])

    assert is_group == {"entity_id": "light.enrichmarker", "is_group": True}
    assert members == {
        "entity_id": "light.enrichmarker",
        "is_group": True,
        "member_entity_ids": ["light.one", "light.two"],
    }


_MEMBERSHIP_CASES = [
    (
        {
            "group_entities": ["light.two", "light.one", "light.two"],
            "entity_id": ["light.legacy"],
        },
        ["light.one", "light.two"],
    ),
    ({"entity_id": ("light.two", "light.one")}, ["light.one", "light.two"]),
    ({"group_entities": [], "entity_id": ["light.legacy"]}, []),
    ({"entity_id": "light.reference"}, None),
    ({"entity_id": b"light.reference"}, None),
    ({"entity_id": bytearray(b"light.reference")}, None),
    ({"group_entities": {"light.member": True}}, None),
    (
        {"group_entities": frozenset({"light.two", "light.one"})},
        ["light.one", "light.two"],
    ),
    ({"entity_id": ["light.Not_Valid"]}, None),
    ({"entity_id": ["light.not-valid"]}, None),
    ({"entity_id": ["light.lämp"]}, None),
    ({"group_entities": ["light.valid", "not-an-entity"]}, None),
    # Malformed modern data is rejected before the legacy fallback.
    (
        {
            "group_entities": ["light.valid", "not-an-entity"],
            "entity_id": ["light.legacy"],
        },
        ["light.legacy"],
    ),
    (
        {"group_entities": ["light.modern"], "entity_id": ["light.legacy"]},
        ["light.modern"],
    ),
    (
        {"group_entities": ["light.group", "light.member"]},
        ["light.group", "light.member"],
    ),
    ({"group_members": ["light.not_supported"]}, None),
]


@pytest.mark.parametrize(("attributes", "expected"), _MEMBERSHIP_CASES)
def test_the_servers_membership_normalizer(
    hass: HomeAssistant, attributes: dict[str, Any], expected: list[str] | None
) -> None:
    hass.states.async_set("light.marker", "on", attributes)
    state = hass.states.get("light.marker")
    assert state is not None

    assert normalize_member_entity_ids(state.attributes) == expected


@pytest.mark.parametrize(("attributes", "expected"), _MEMBERSHIP_CASES)
async def test_the_component_normalizes_membership_like_the_server(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    attributes: dict[str, Any],
    expected: list[str] | None,
) -> None:
    """Server and component normalization stay aligned across all edge cases."""
    hass.states.async_set(
        "light.marker", "on", {"friendly_name": "Membership marker", **attributes}
    )

    result = await ws_result(
        component_ws,
        WS_SEARCH,
        query="membership marker",
        search_types=["entity"],
        result_fields=["is_group", "member_entity_ids"],
    )

    (record,) = result["entities"]
    assert record.get("member_entity_ids") == expected
    assert record["is_group"] is (expected is not None)


@pytest.mark.parametrize(
    ("visibility", "include_hidden"),
    [({"deny_entity_ids": ["light.denied"]}, True), (None, False)],
    ids=["visibility-denied", "registry-hidden"],
)
async def test_a_group_withholds_members_it_may_not_show(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    entity_registry: er.EntityRegistry,
    visibility: dict[str, Any] | None,
    include_hidden: bool,
) -> None:
    """Member ids honor both hard visibility and include_hidden=False."""
    for object_id in ("membership_group", "visible", "denied"):
        entity_registry.async_get_or_create(
            "light", "test", object_id, suggested_object_id=object_id
        )
    entity_registry.async_update_entity(
        "light.denied", hidden_by=er.RegistryEntryHider.USER
    )
    hass.states.async_set(
        "light.membership_group",
        "on",
        {
            "friendly_name": "Membership marker",
            "group_entities": ["light.visible", "light.denied"],
        },
    )
    extra = {"visibility": visibility} if visibility else {}

    result = await ws_result(
        component_ws,
        WS_SEARCH,
        query="membership marker",
        search_types=["entity"],
        include_hidden=include_hidden,
        result_fields=["is_group", "member_entity_ids"],
        **extra,
    )

    (record,) = result["entities"]
    assert record["is_group"] is True
    assert "member_entity_ids" not in record
