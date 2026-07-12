"""Contract test for the component-search seam (ha_mcp_tools/search).

The component's ``_do_search`` and the server's
``_shape_component_search_response`` were written against the same design
contract but never against each other's code. This suite pipes the REAL
component output (fake hass, real joins/scoring) through the REAL server
shaper and pins the result to the legacy ha_search record shapes — so a
vocabulary drift on either side (``alias`` vs ``friendly_name``, ``options``
vs ``config``, id-key renames) fails here instead of shipping a
component-served response whose records don't match the legacy path.

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
Scene records never carry a body either: a scene's config is always ``None``
from the component (its ``states`` are runtime objects), and it matches on the
entity-id KEYS of ``states`` (here ``light.contractmarker_lamp``).
"""

from __future__ import annotations

from ha_mcp.tools.tools_search import (
    _ResolvedSearch,
    _shape_component_search_response,
)

from .test_component_ws_search import (
    FakeConfigEntries,
    FakeHass,
    FakeState,
    wsapi,
)

AUTOMATION_KEYS = {
    "entity_id",
    "friendly_name",
    "score",
    "match_in_name",
    "match_in_config",
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
}
HELPER_FLOW_KEYS = {
    "entry_id",
    "helper_type",
    "name",
    "score",
    "match_in_name",
    "match_in_config",
}
ENTITY_KEYS = {
    "entity_id",
    "friendly_name",
    "domain",
    "state",
    "score",
    "match_type",
}


class _Entity:
    """Minimal automation/script entity double (raw_config accessor)."""

    def __init__(self, entity_id, name, raw_config, unique_id=None):
        self.entity_id = entity_id
        self.name = name
        self.raw_config = raw_config
        self.unique_id = unique_id


class _SceneEntity:
    def __init__(self, entity_id, name, scene_config, unique_id=None):
        self.entity_id = entity_id
        self.name = name
        self.scene_config = scene_config
        self.unique_id = unique_id


class _Component:
    def __init__(self, entities):
        self.entities = entities


class _Entry:
    def __init__(self, entry_id, domain, title, options, data=None):
        self.entry_id = entry_id
        self.domain = domain
        self.title = title
        self.options = options
        self.data = data or {}


def _search_marker_hass() -> FakeHass:
    """A hass where the query 'contractmarker' hits every surface once."""
    states = [
        FakeState(
            "light.contractmarker_lamp", "on", friendly_name="Contractmarker Lamp"
        ),
        FakeState(
            "input_boolean.contractmarker_flag",
            "off",
            friendly_name="Contractmarker Flag",
        ),
    ]
    data = {
        "automation": _Component(
            [
                _Entity(
                    "automation.storage_hit",
                    "Contractmarker Storage Automation",
                    {"id": "auto1", "alias": "Contractmarker Storage Automation"},
                    unique_id="auto1",
                ),
                _Entity(
                    "automation.yaml_hit",
                    "Yaml Automation",
                    {
                        "alias": "Yaml Automation",
                        "action": [{"service": "notify.contractmarker"}],
                        "secret_value": "hunter2-contractmarker-secret",
                    },
                    unique_id=None,
                ),
            ]
        ),
        "script": _Component(
            [
                _Entity(
                    "script.contractmarker_script",
                    "Contractmarker Script",
                    {"id": "scr1", "alias": "Contractmarker Script"},
                    unique_id="scr1",
                )
            ]
        ),
        "scene": _Component(
            [
                _SceneEntity(
                    "scene.contractmarker_scene",
                    "Contractmarker Scene",
                    # ``states`` keys are entity ids (the scene match corpus); the
                    # runtime State VALUES are never scored or emitted.
                    {
                        "id": "scn1",
                        "name": "Contractmarker Scene",
                        "states": {"light.contractmarker_lamp": {"state": "on"}},
                    },
                    unique_id="scn1",
                )
            ]
        ),
    }
    entries = [
        _Entry(
            "entry1",
            "template",
            "Contractmarker Template Helper",
            {"template": "{{ 1 }}"},
            data={"credential": "should-never-appear"},
        )
    ]
    hass = FakeHass(states=states, data=data)
    hass.config_entries = FakeConfigEntries(entries)
    return hass


def _resolved(include_config: bool) -> _ResolvedSearch:
    return _ResolvedSearch(
        query="contractmarker",
        query_text="contractmarker",
        domain_filter=None,
        area_filter=None,
        state_filter=None,
        parsed_search_types=None,
        parsed_fields=None,
        result_fields=None,
        limit=10,
        offset=0,
        exact_match=True,
        include_hidden=False,
        include_config=include_config,
        group_by_domain=False,
        per_domain_limit=None,
        config_time_budget=None,
        registry_eligible=True,
        body_eligible=True,
        body_skipped_by_intent_gate=False,
    )


def _component_result(monkeypatch, include_config: bool):
    hass = _search_marker_hass()
    monkeypatch.setattr(
        wsapi, "_resolve_registries", lambda hass: wsapi._RegistryView()
    )
    return wsapi._do_search(
        hass,
        {
            "query": "contractmarker",
            "search_types": ["entity", "automation", "script", "scene", "helper"],
            "exact": True,
            "include_hidden": False,
            "include_config": include_config,
            "limit": 10,
            "offset": 0,
        },
    )


def _records(shaped, bucket):
    recs = shaped.get(bucket)
    assert isinstance(recs, list) and recs, f"expected records in {bucket}: {shaped}"
    return recs


def test_all_surfaces_match_and_shape_to_legacy_keys(monkeypatch) -> None:
    """Real component output → real server shaper → exact legacy key sets."""
    result = _component_result(monkeypatch, include_config=True)
    shaped = _shape_component_search_response(_resolved(True), result)

    assert shaped["success"] is True

    for rec in _records(shaped, "entities"):
        assert set(rec) == ENTITY_KEYS, f"entity record keys drifted: {rec}"

    autos = _records(shaped, "automations")
    assert {r["entity_id"] for r in autos} == {
        "automation.storage_hit",
        "automation.yaml_hit",
    }
    for rec in autos:
        assert set(rec) == AUTOMATION_KEYS | {"config"}, (
            f"automation record keys drifted: {rec}"
        )
        assert rec["friendly_name"], "friendly_name must be populated from alias"

    for rec in _records(shaped, "scripts"):
        assert set(rec) == SCRIPT_KEYS | {"config"}, (
            f"script record keys drifted: {rec}"
        )
        assert rec["script_id"] == "scr1"

    for rec in _records(shaped, "scenes"):
        assert set(rec) == SCENE_KEYS | {"config"}, f"scene record keys drifted: {rec}"
        assert rec["scene_id"] == "scn1"
        # Scenes never emit a component-served body: the config key follows the
        # include_config pop rule, but its value from the component is always None.
        assert rec["config"] is None

    helper_recs = _records(shaped, "helpers")
    by_kind = {("flow" if "entry_id" in r else "collection"): r for r in helper_recs}
    assert set(by_kind["collection"]) == HELPER_COLLECTION_KEYS | {"config"}, (
        f"collection helper keys drifted: {by_kind['collection']}"
    )
    assert set(by_kind["flow"]) == HELPER_FLOW_KEYS | {"config"}, (
        f"flow helper keys drifted: {by_kind['flow']}"
    )
    # Flow helper body rides `options` component-side, `config` in the envelope.
    assert by_kind["flow"]["config"] == {"template": "{{ 1 }}"}


def test_yaml_body_withheld_but_matched(monkeypatch) -> None:
    """A YAML automation matched on its body must carry no config body, and
    the resolved-secret string must be absent from the whole response."""
    result = _component_result(monkeypatch, include_config=True)
    shaped = _shape_component_search_response(_resolved(True), result)

    yaml_rec = next(
        r for r in shaped["automations"] if r["entity_id"] == "automation.yaml_hit"
    )
    assert yaml_rec["match_in_config"] is True
    assert yaml_rec["config"] is None
    assert "hunter2-contractmarker-secret" not in repr(shaped)
    # Storage sibling keeps its body under include_config.
    storage_rec = next(
        r for r in shaped["automations"] if r["entity_id"] == "automation.storage_hit"
    )
    assert storage_rec["config"] == {
        "id": "auto1",
        "alias": "Contractmarker Storage Automation",
    }


def test_include_config_false_strips_config_keys(monkeypatch) -> None:
    """Without include_config, no record in any bucket carries a config key —
    mirroring the legacy pipeline's include_config pop."""
    result = _component_result(monkeypatch, include_config=False)
    shaped = _shape_component_search_response(_resolved(False), result)

    for bucket in ("automations", "scripts", "scenes", "helpers"):
        for rec in _records(shaped, bucket):
            assert "config" not in rec, f"{bucket} record leaked config key: {rec}"

    # entry.data must never surface anywhere, with or without include_config.
    assert "should-never-appear" not in repr(shaped)


def test_envelope_matches_legacy_keys(monkeypatch) -> None:
    """Envelope keys parity: the component-served response exposes the same
    top-level keys the legacy path emits for a both-branches query."""
    result = _component_result(monkeypatch, include_config=False)
    shaped = _shape_component_search_response(_resolved(False), result)
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
