"""The component's visibility filter and the server's give the same answer.

Over one Core and one visibility config, the entities ``ha_search`` returns
through the component's ``search`` (which gets the config as its
``visibility`` param) must equal what the same search returns through Core's
own commands, where the server applies its resolver's hidden set. The two
filters were written to one design but never run against each other; this
file fails when they drift on any hide dimension or in the warnings they give.
What each filter hides is tested on its own layer:
``test_component_visibility_*_ws.py`` for the component's,
``visibility/test_resolver.py`` for the server's.

Both runs are ``ha_search(query, domain_filter="light", exact_match=True)``,
an entity-only substring search, so only the visibility filter can make them
differ. Every entity_id carries ``vismark``, which nothing else does.
Registries, states and Assist exposure are Core's, set up through its API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
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

from custom_components.ha_mcp_tools.websocket_api import (
    visibility as component_visibility,
)
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.component_api import invalidate_caps
from ha_mcp.visibility import resolver
from ha_mcp.visibility.model import VisibilityConfig, VisibilityWire
from ha_mcp.visibility.persistence import save_visibility_config

from ._client_spies import sent_messages
from ._search_tool import _build_ha_search
from ._visibility_config import visibility_config as _cfg

_QUERY = "vismark"


@dataclass
class _Ent:
    """One registered entity of a scenario."""

    entity_id: str
    category: str | None = None
    hidden_by: str | None = None
    area_id: str | None = None
    device_id: str | None = None
    labels: tuple[str, ...] = ()


@dataclass
class _Scenario:
    id: str
    ents: tuple[_Ent, ...]
    config: VisibilityConfig
    # device id -> (area_id, labels)
    devices: dict[str, tuple[str | None, tuple[str, ...]]] = field(default_factory=dict)
    states_only: tuple[str, ...] = ()
    include_hidden: bool = True
    # Entities exposed to Assist; every other entity is explicitly not exposed.
    exposed: frozenset[str] | None = None
    # False: Core has no Assist exposure (``homeassistant`` not set up), so the
    # Assist dimension is skipped and both filters warn.
    assist_available: bool = True


_SCENARIOS = [
    _Scenario(
        id="deny_incl_states_only",
        ents=(_Ent("light.vismark_keep"), _Ent("light.vismark_drop")),
        states_only=("light.vismark_ghost",),
        config=_cfg(deny_entity_ids=["light.vismark_drop", "light.vismark_ghost"]),
    ),
    _Scenario(
        id="exclude_category",
        ents=(
            _Ent("light.vismark_normal"),
            _Ent("light.vismark_diag", category="diagnostic"),
            _Ent("light.vismark_cfg", category="config"),
        ),
        config=VisibilityConfig(
            enabled=True, exclude_categories=["diagnostic", "config"]
        ),
    ),
    _Scenario(
        id="exclude_hidden",
        ents=(
            _Ent("light.vismark_normal"),
            _Ent("light.vismark_hid", hidden_by="user"),
        ),
        config=_cfg(exclude_hidden=True),
    ),
    _Scenario(
        id="exclude_area_direct_and_device_inherited",
        ents=(
            _Ent("light.vismark_direct", area_id="garage"),
            _Ent("light.vismark_viadev", device_id="d1"),
            _Ent("light.vismark_keep", area_id="office"),
        ),
        devices={"d1": ("garage", ())},
        config=_cfg(exclude_areas=["garage"]),
    ),
    _Scenario(
        id="exclude_label_direct_and_device_inherited",
        ents=(
            _Ent("light.vismark_direct", labels=("hide",)),
            _Ent("light.vismark_viadev", device_id="d1"),
            _Ent("light.vismark_keep", labels=("show",)),
        ),
        devices={"d1": (None, ("hide",))},
        config=_cfg(exclude_labels=["hide"]),
    ),
    _Scenario(
        id="allow_entity_ids_states_only_skips_unavailable_assist",
        ents=(),
        states_only=("light.vismark_keep", "light.vismark_drop"),
        config=_cfg(
            allow_entity_ids=["light.vismark_keep"], respect_assist_exposure=True
        ),
        assist_available=False,
    ),
    _Scenario(
        id="allow_entity_ids_restrict",
        ents=(_Ent("light.vismark_keep"), _Ent("light.vismark_drop")),
        states_only=("light.vismark_ghost",),
        config=_cfg(allow_entity_ids=["light.vismark_keep"]),
    ),
    _Scenario(
        id="allow_areas_restrict",
        ents=(
            _Ent("light.vismark_keep", area_id="office"),
            _Ent("light.vismark_drop", area_id="garage"),
        ),
        config=_cfg(allow_areas=["office"]),
    ),
    _Scenario(
        id="allow_label_direct_and_device_inherited",
        ents=(
            _Ent("light.vismark_direct", labels=("keep",)),
            _Ent("light.vismark_viadev", device_id="d1"),
            _Ent("light.vismark_drop", labels=("other",)),
        ),
        devices={"d1": (None, ("keep",))},
        config=_cfg(allow_labels=["keep"]),
    ),
    _Scenario(
        id="allow_area_device_inherited",
        ents=(
            _Ent("light.vismark_viadev", device_id="d1"),
            _Ent("light.vismark_drop", area_id="garage"),
        ),
        devices={"d1": ("office", ())},
        config=_cfg(allow_areas=["office"]),
    ),
    _Scenario(
        id="allow_match_overrides_category_and_assist",
        ents=(
            _Ent("light.vismark_allowed", category="diagnostic", hidden_by="user"),
            _Ent("light.vismark_drop"),
        ),
        exposed=frozenset(),
        config=VisibilityConfig(
            enabled=True,
            exclude_categories=["diagnostic"],
            exclude_hidden=True,
            allow_entity_ids=["light.vismark_allowed"],
            respect_assist_exposure=True,
        ),
    ),
    _Scenario(
        id="explicit_exclude_area_wins_over_allow",
        ents=(
            _Ent("light.vismark_conflict", area_id="office"),
            _Ent("light.vismark_allowed", area_id="kitchen"),
        ),
        config=_cfg(
            exclude_areas=["office"],
            allow_entity_ids=["light.vismark_conflict", "light.vismark_allowed"],
        ),
    ),
    _Scenario(
        id="assist_exposure",
        ents=(_Ent("light.vismark_a"), _Ent("light.vismark_b")),
        exposed=frozenset({"light.vismark_b"}),
        config=_cfg(respect_assist_exposure=True),
    ),
    _Scenario(
        id="include_hidden_true_keeps_hidden",
        ents=(
            _Ent("light.vismark_normal"),
            _Ent("light.vismark_hid", hidden_by="user"),
        ),
        # An active but inert dimension (deny of an absent id): include_hidden
        # is the only thing acting on the hidden entity.
        config=_cfg(deny_entity_ids=["light.vismark_absent"]),
        include_hidden=True,
    ),
    _Scenario(
        id="include_hidden_false_drops_hidden",
        ents=(
            _Ent("light.vismark_normal"),
            _Ent("light.vismark_hid", hidden_by="user"),
        ),
        config=_cfg(deny_entity_ids=["light.vismark_absent"]),
        include_hidden=False,
    ),
    # Each of these degrades one dimension, which both filters skip and warn about.
    _Scenario(
        id="unknown_category_warns",
        ents=(_Ent("light.vismark_a"), _Ent("light.vismark_b")),
        # exclude_hidden keeps the config active (a config with only an unknown
        # category reads as inactive, so the search would carry no visibility);
        # it hides nothing here. Every category Core has is known.
        config=VisibilityConfig(
            enabled=True,
            exclude_categories=[*(c.value for c in EntityCategory), "bogus"],
            exclude_hidden=True,
        ),
    ),
    _Scenario(
        id="empty_registry_allowlist_warns",
        ents=(),
        states_only=("light.vismark_g1", "light.vismark_g2"),
        exposed=frozenset({"light.vismark_g1"}),
        config=_cfg(allow_areas=["office"], respect_assist_exposure=True),
    ),
    _Scenario(
        id="assist_unavailable_warns",
        ents=(_Ent("light.vismark_a"), _Ent("light.vismark_b")),
        config=_cfg(respect_assist_exposure=True),
        assist_available=False,
    ),
    _Scenario(
        id="explicit_allow_survives_empty_registry_area_degradation",
        ents=(),
        states_only=("light.vismark_keep", "light.vismark_drop"),
        config=_cfg(
            allow_entity_ids=["light.vismark_keep"],
            allow_areas=["office"],
            respect_assist_exposure=True,
        ),
        assist_available=False,
    ),
]


async def _core(
    hass: HomeAssistant, client: HomeAssistantClient, scenario: _Scenario
) -> None:
    """Core's registries, states and Assist exposure for ``scenario``."""
    assert await async_setup_component(hass, "config", {})
    if scenario.assist_available:
        assert await async_setup_component(hass, "homeassistant", {})
    if scenario.exposed is not None:
        # Core lists only the entities exposed to Assist, so with new entities
        # exposed by default the server cannot see a states-only entity's
        # explicit un-expose (resolver.py says so); new entities stay unexposed
        # here, as the two filters are compared on what both can see.
        reply = await client.send_websocket_message(
            {
                "type": "homeassistant/expose_new_entities/set",
                "assistant": "conversation",
                "expose_new": False,
            }
        )
        assert reply["success"], reply
    names = {area for _, (area, _) in scenario.devices.items() if area}
    names |= {e.area_id for e in scenario.ents if e.area_id}
    for name in names:
        assert ar.async_get(hass).async_create(name.title()).id == name
    labels = {label for e in scenario.ents for label in e.labels}
    labels |= {label for _, (_, ls) in scenario.devices.items() for label in ls}
    for label in labels:
        assert lr.async_get(hass).async_create(label).label_id == label
    entry = MockConfigEntry(domain="test")
    entry.add_to_hass(hass)
    devices = dr.async_get(hass)
    device_ids: dict[str, str] = {}
    for did, (area, device_labels) in scenario.devices.items():
        device = devices.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={("test", did)}
        )
        devices.async_update_device(device.id, area_id=area, labels=set(device_labels))
        device_ids[did] = device.id
    entities = er.async_get(hass)
    for ent in scenario.ents:
        object_id = ent.entity_id.partition(".")[2]
        row = entities.async_get_or_create(
            "light",
            "test",
            object_id,
            suggested_object_id=object_id,
            config_entry=entry,
            device_id=device_ids.get(ent.device_id or ""),
            original_name=object_id,
            entity_category=EntityCategory(ent.category) if ent.category else None,
        )
        assert row.entity_id == ent.entity_id
        entities.async_update_entity(
            ent.entity_id,
            area_id=ent.area_id,
            labels=set(ent.labels),
            hidden_by=er.RegistryEntryHider(ent.hidden_by) if ent.hidden_by else None,
        )
    for entity_id in (*(e.entity_id for e in scenario.ents), *scenario.states_only):
        hass.states.async_set(entity_id, "on")
        if scenario.exposed is not None:
            async_expose_entity(
                hass, "conversation", entity_id, entity_id in scenario.exposed
            )


async def _search(client: HomeAssistantClient, scenario: _Scenario) -> dict[str, Any]:
    resp: dict[str, Any] = await _build_ha_search(client)(
        query=_QUERY,
        domain_filter="light",
        exact_match=True,
        include_hidden=scenario.include_hidden,
        limit=50,
    )
    return resp


def _ids(resp: dict[str, Any]) -> set[str]:
    return {e["entity_id"] for e in resp["entities"]}


def _visibility_warnings(resp: dict[str, Any]) -> set[str]:
    return {w for w in resp.get("warnings", []) if w.startswith("Entity visibility")}


@pytest.mark.parametrize("scenario", _SCENARIOS, ids=[s.id for s in _SCENARIOS])
async def test_the_component_and_the_server_hide_the_same_entities(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    scenario: _Scenario,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    save_visibility_config(tmp_path, scenario.config)
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)
    client = component_core_client
    await _core(hass, client, scenario)

    with (
        sent_messages(client) as legacy_reads,
        patch.object(client, "get_states", wraps=client.get_states) as states,
    ):
        component = await _search(client, scenario)
    # Without the handshake the server searches over Core's own commands.
    monkeypatch.delitem(hass.data["websocket_api"], "ha_mcp_tools/info")
    invalidate_caps(client)
    server = await _search(client, scenario)

    # Every scenario keeps at least one entity: two empty answers prove nothing.
    assert _ids(component)
    assert _ids(component) == _ids(server)
    # Hidden entities are dropped before the count on both paths.
    assert (
        component["entity_total_matches"]
        == server["entity_total_matches"]
        == len(_ids(component))
    )
    # The component served the whole search: the server read nothing itself
    # and filtered nothing on top.
    legacy_reads.assert_not_awaited()
    states.assert_not_awaited()
    # The two filters give the same visibility warnings, word for word.
    assert _visibility_warnings(component) == _visibility_warnings(server)


async def test_the_component_accepts_exactly_the_visibility_keys_the_server_sends(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """The component refuses an unknown ``visibility`` key, so its schema must
    hold every key the server sends and no key the server cannot send:
    ``to_wire`` emits the hide dimensions, and the server adds the optional
    precedence flag per the component's capability."""
    _, schema = hass.data["websocket_api"]["ha_mcp_tools/search"]
    visibility = next(v for k, v in schema.schema.items() if str(k) == "visibility")
    accepted = {str(key) for key in visibility.schema}

    assert set(VisibilityConfig().to_wire()) == set(VisibilityWire.__required_keys__)
    assert accepted == set(VisibilityWire.__required_keys__) | set(
        VisibilityWire.__optional_keys__
    )


def test_the_device_registry_warnings_core_cannot_produce_are_the_servers() -> None:
    """Core never holds the conflicting or invalid device rows these two warn
    about, so no search can compare them. The component cannot import the
    server and keeps its own copy of each."""
    assert (
        component_visibility.DEVICE_REGISTRY_CONFLICT_WARNING
        == resolver.DEVICE_REGISTRY_CONFLICT_WARNING
    )
    assert (
        component_visibility.DEVICE_REGISTRY_INVALID_AREA_WARNING
        == resolver.DEVICE_REGISTRY_INVALID_AREA_WARNING
    )
