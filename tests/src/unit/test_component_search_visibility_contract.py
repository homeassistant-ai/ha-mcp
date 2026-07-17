"""Cross-seam parity test for the ``search_visibility`` capability.

The load-bearing contract for the whole capability: over ONE shared fixture and
ONE visibility config, the surviving entity set the REAL component ``_do_search``
produces (driven through the REAL ``ha_search`` with the serialized ``visibility``
param) must EQUAL the surviving set the REAL legacy path produces (which applies
the server resolver's ``hidden_entity_ids``). The component's
``_visibility_hidden_set`` and the server's ``hidden_entity_ids`` were written to
the same design but never against each other; this file fails if their
composition drifts on ANY hide dimension.

Both paths run the same ``ha_search(query, domain_filter="light",
exact_match=True)`` — an entity-only substring search (``exact_match`` makes the
pre-visibility candidate set deterministic and, because both paths share
``_match_exact_search_entity``, identical). So the ONLY thing that can make the
two surviving sets differ is the visibility application under test. Every fixture
entity_id carries the shared ``vismark`` token (present nowhere else — not in an
area/label/alias) so both paths match all candidates identically before the
filter runs.

The Assist dimension is the one place the two sides compute differently: the
server reconstructs ``async_should_expose`` from the expose-list + expose_new,
the component delegates to an injectable ``should_expose_fn``. The fixture wires
the legacy side's inputs (an explicit expose-list, expose_new off) so its
reconstruction yields a known exposed set, and injects the component's fake to
that same set — the aligned-injection the component author documented.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.client.rest_client import HomeAssistantCommandError
from ha_mcp.tools import tools_search
from ha_mcp.visibility import resolver
from ha_mcp.visibility.model import VisibilityConfig
from ha_mcp.visibility.persistence import save_visibility_config

from ._component_routing_helpers import make_ws, patch_ws
from .test_component_ws_search import (
    FakeDevice,
    FakeHass,
    FakeRegEntry,
    FakeState,
    make_view,
    wsapi,
)
from .test_ha_search_component_routing import RoutingClient, _build_ha_search

# Shared substring token every fixture entity_id carries and nothing else does,
# so a substring ``exact_match`` search matches every candidate on entity_id
# alone — identically on both the component's ``_match_exact_search_entity`` and
# the server's.
_QUERY = "vismark"

_CAPS_SEARCH_VIS = {
    "schema_version": 1,
    "component_version": "1.2.0",
    "capabilities": ["search", "search_visibility"],
    "limits": {},
}


@dataclass
class _Ent:
    """One fixture entity, rendered into both a component registry entry and the
    server's ``config/entity_registry/list`` element."""

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
    expected: frozenset[str]
    devices: dict[str, tuple[str | None, tuple[str, ...]]] = field(default_factory=dict)
    states_only: tuple[str, ...] = ()
    include_hidden: bool = True
    exposed: frozenset[str] | None = None


def _cfg(**kw: Any) -> VisibilityConfig:
    """An enabled config with the default category exclude cleared, so each
    scenario activates exactly the one dimension it names."""
    kw.setdefault("exclude_categories", [])
    return VisibilityConfig(enabled=True, **kw)


_SCENARIOS = [
    _Scenario(
        id="deny_incl_states_only",
        ents=(_Ent("light.vismark_keep"), _Ent("light.vismark_drop")),
        states_only=("light.vismark_ghost",),
        config=_cfg(deny_entity_ids=["light.vismark_drop", "light.vismark_ghost"]),
        expected=frozenset({"light.vismark_keep"}),
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
        expected=frozenset({"light.vismark_normal"}),
    ),
    _Scenario(
        id="exclude_hidden",
        ents=(
            _Ent("light.vismark_normal"),
            _Ent("light.vismark_hid", hidden_by="user"),
        ),
        config=_cfg(exclude_hidden=True),
        expected=frozenset({"light.vismark_normal"}),
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
        expected=frozenset({"light.vismark_keep"}),
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
        expected=frozenset({"light.vismark_keep"}),
    ),
    _Scenario(
        id="allow_entity_ids_restrict",
        ents=(_Ent("light.vismark_keep"), _Ent("light.vismark_drop")),
        states_only=("light.vismark_ghost",),
        config=_cfg(allow_entity_ids=["light.vismark_keep"]),
        expected=frozenset({"light.vismark_keep"}),
    ),
    _Scenario(
        id="allow_areas_restrict",
        ents=(
            _Ent("light.vismark_keep", area_id="office"),
            _Ent("light.vismark_drop", area_id="garage"),
        ),
        config=_cfg(allow_areas=["office"]),
        expected=frozenset({"light.vismark_keep"}),
    ),
    _Scenario(
        id="assist_via_injected_fake",
        ents=(_Ent("light.vismark_a"), _Ent("light.vismark_b")),
        exposed=frozenset({"light.vismark_b"}),
        config=_cfg(respect_assist_exposure=True),
        expected=frozenset({"light.vismark_b"}),
    ),
    _Scenario(
        id="include_hidden_true_keeps_hidden",
        ents=(
            _Ent("light.vismark_normal"),
            _Ent("light.vismark_hid", hidden_by="user"),
        ),
        # An active-but-inert dimension (deny of an absent id): the filter is
        # active without touching either entity, so include_hidden is the only
        # thing acting on the hidden_by entity.
        config=_cfg(deny_entity_ids=["light.vismark_absent"]),
        include_hidden=True,
        expected=frozenset({"light.vismark_normal", "light.vismark_hid"}),
    ),
    _Scenario(
        id="include_hidden_false_drops_hidden",
        ents=(
            _Ent("light.vismark_normal"),
            _Ent("light.vismark_hid", hidden_by="user"),
        ),
        config=_cfg(deny_entity_ids=["light.vismark_absent"]),
        include_hidden=False,
        expected=frozenset({"light.vismark_normal"}),
    ),
]


# --- component side (real _do_search behind the real ha_search) ---------------
def _real_search_ws(hass: FakeHass) -> AsyncMock:
    """A WS mock backed by the REAL component search: ``info`` is the real
    handshake (so the caps probe sees ``search_visibility``) and ``search`` runs
    the real ``_search_prep`` + ``_do_search`` against ``hass``."""
    ws = AsyncMock()

    async def _send(command_type: str, **kwargs: Any) -> dict[str, Any]:
        if command_type == "ha_mcp_tools/info":
            return {"success": True, "result": wsapi._do_info(hass)}
        assert command_type == "ha_mcp_tools/search", command_type
        params = dict(kwargs)
        extra = await wsapi._search_prep(hass, params)
        return {"success": True, "result": wsapi._do_search(hass, params, **extra)}

    ws.send_command = AsyncMock(side_effect=_send)
    return ws


def _component_view(scenario: _Scenario) -> Any:
    entity = {
        e.entity_id: FakeRegEntry(
            e.entity_id,
            entity_category=e.category,
            hidden_by=e.hidden_by,
            area_id=e.area_id,
            device_id=e.device_id,
            labels=e.labels,
        )
        for e in scenario.ents
    }
    devices = [
        FakeDevice(did, area_id=area, labels=set(labels))
        for did, (area, labels) in scenario.devices.items()
    ]
    return make_view(entity=entity, devices=devices)


async def _run_component(
    scenario: _Scenario, monkeypatch: Any
) -> tuple[dict[str, Any], RoutingClient]:
    monkeypatch.setattr(wsapi, "_resolve_registries", lambda hass: _component_view(scenario))
    if scenario.exposed is not None:
        exposed = set(scenario.exposed)
        monkeypatch.setattr(
            wsapi, "_assist_should_expose", lambda hass, eid: eid in exposed
        )
    states = [FakeState(e.entity_id) for e in scenario.ents]
    states += [FakeState(eid) for eid in scenario.states_only]
    hass = FakeHass(states=states)
    ws = _real_search_ws(hass)
    client = RoutingClient()
    ha_search = _build_ha_search(client)
    with patch_ws(ws, tools_search):
        resp = await ha_search(
            query=_QUERY,
            domain_filter="light",
            exact_match=True,
            include_hidden=scenario.include_hidden,
            limit=50,
        )
    return resp, client


# --- legacy side (server resolver over WS payloads) ---------------------------
class _LegacyClient:
    """HA client whose WS/REST reads render ``scenario`` into the shapes the
    legacy ``_exact_match_search`` + ``load_hidden_set`` pipeline consumes."""

    def __init__(self, scenario: _Scenario) -> None:
        self.base_url = "http://ha.local:8123"
        self.token = "tok"
        self.get_states_calls = 0
        self.ws_types: Counter[str] = Counter()
        self._states = [
            {"entity_id": e.entity_id, "state": "on", "attributes": {}}
            for e in scenario.ents
        ] + [
            {"entity_id": eid, "state": "on", "attributes": {}}
            for eid in scenario.states_only
        ]
        self._registry = {
            "success": True,
            "result": [
                {
                    "entity_id": e.entity_id,
                    "entity_category": e.category,
                    "hidden_by": e.hidden_by,
                    "area_id": e.area_id,
                    "device_id": e.device_id,
                    "labels": list(e.labels),
                }
                for e in scenario.ents
            ],
        }
        self._device = {
            "success": True,
            "result": [
                {"id": did, "area_id": area, "labels": list(labels)}
                for did, (area, labels) in scenario.devices.items()
            ],
        }
        self._exposed = set(scenario.exposed or ())

    async def get_states(self) -> list[dict[str, Any]]:
        self.get_states_calls += 1
        return [dict(s) for s in self._states]

    async def get_config(self) -> dict[str, Any]:
        return {"time_zone": "UTC"}

    async def send_websocket_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        msg_type = msg.get("type", "")
        self.ws_types[msg_type] += 1
        if msg_type == "config/entity_registry/list":
            return self._registry
        if msg_type == "config/device_registry/list":
            return self._device
        if msg_type == "config/entity_registry/get_entries":
            return {"success": True, "result": {}}
        if msg_type == "homeassistant/expose_entity/list":
            return {
                "success": True,
                "result": {
                    "exposed_entities": {
                        eid: {"conversation": True} for eid in self._exposed
                    }
                },
            }
        if msg_type == "homeassistant/expose_new_entities/get":
            # expose_new OFF: only the explicit expose-list is exposed, so the
            # server's reconstruction reduces to "eid in exposed" — the set the
            # component's injected fake is aligned to.
            return {"success": True, "result": {"expose_new": False}}
        return {"success": False}


async def _run_legacy(scenario: _Scenario) -> dict[str, Any]:
    client = _LegacyClient(scenario)
    # info → unknown_command ⇒ no caps ⇒ the legacy pipeline serves the search.
    ws = make_ws(
        "ha_mcp_tools/search",
        info_exc=HomeAssistantCommandError("no info", "unknown_command"),
    )
    ha_search = _build_ha_search(client)
    with patch_ws(ws, tools_search):
        resp = await ha_search(
            query=_QUERY,
            domain_filter="light",
            exact_match=True,
            include_hidden=scenario.include_hidden,
            limit=50,
        )
    return resp


@pytest.mark.parametrize("scenario", _SCENARIOS, ids=[s.id for s in _SCENARIOS])
@pytest.mark.asyncio
async def test_component_visibility_matches_legacy(
    scenario: _Scenario, tmp_path: Any, monkeypatch: Any
) -> None:
    """The component-filtered surviving set EQUALS the legacy path's, per dimension."""
    save_visibility_config(tmp_path, scenario.config)
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)

    comp_resp, comp_client = await _run_component(scenario, monkeypatch)
    legacy_resp = await _run_legacy(scenario)

    comp_ids = {e["entity_id"] for e in comp_resp["entities"]}
    legacy_ids = {e["entity_id"] for e in legacy_resp["entities"]}

    # Component == expected == legacy: the composition matches on this dimension.
    assert comp_ids == set(scenario.expected), (
        f"component surviving set drifted for {scenario.id}: {comp_ids}"
    )
    assert comp_ids == legacy_ids, (
        f"component vs legacy surviving set drifted for {scenario.id}: "
        f"{comp_ids} != {legacy_ids}"
    )
    # Hidden exclusion lands BEFORE the count on both paths: the totals match the
    # post-exclusion survivor count, not the raw candidate count.
    assert (
        comp_resp["entity_total_matches"]
        == legacy_resp["entity_total_matches"]
        == len(scenario.expected)
    )
    # No double-filtering: the component served the whole request in-process; the
    # server never re-fetched the state machine or re-applied the filter on top.
    assert comp_client.get_states_calls == 0
    assert comp_client.ws_types == Counter()
