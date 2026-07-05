import logging

from ha_mcp.visibility.model import VisibilityConfig
from ha_mcp.visibility.resolver import hidden_entity_ids


def _reg(*entries):
    return {"success": True, "result": list(entries)}


def _hidden(registry_result, config):
    """Just the hidden set (drops the warnings tuple element) for set-equality tests."""
    return hidden_entity_ids(registry_result, config)[0]


def test_enabled_bad_payload_warns_and_fails_open(caplog):
    with caplog.at_level(logging.WARNING):
        assert _hidden({"success": False}, VisibilityConfig(enabled=True)) == set()
    assert any("unusable" in r.message for r in caplog.records)


def test_enabled_exception_payload_fails_open(caplog):
    # gather(return_exceptions=True) can hand an Exception object through as the
    # registry payload; it is non-dict, so the filter degrades to empty (open).
    with caplog.at_level(logging.WARNING):
        assert _hidden(RuntimeError("boom"), VisibilityConfig(enabled=True)) == set()
    assert any("unusable" in r.message for r in caplog.records)


def test_enabled_non_list_result_warns_and_fails_open(caplog):
    with caplog.at_level(logging.WARNING):
        assert (
            _hidden({"success": True, "result": "nope"}, VisibilityConfig(enabled=True))
            == set()
        )
    assert any("not a list" in r.message for r in caplog.records)


def test_disabled_bad_payload_stays_silent(caplog):
    # Disabled is the default no-op; it must NOT warn on every call.
    with caplog.at_level(logging.WARNING):
        assert _hidden({"success": False}, VisibilityConfig(enabled=False)) == set()
    assert not caplog.records


def test_disabled_config_hides_nothing():
    reg = _reg({"entity_id": "sensor.a", "entity_category": "diagnostic"})
    assert _hidden(reg, VisibilityConfig(enabled=False)) == set()


def test_category_exclude():
    reg = _reg(
        {"entity_id": "sensor.batt", "entity_category": "diagnostic"},
        {"entity_id": "light.lr", "entity_category": None},
    )
    cfg = VisibilityConfig(enabled=True, exclude_categories=["diagnostic"])
    assert _hidden(reg, cfg) == {"sensor.batt"}


def test_denylist_and_area_and_label_union():
    reg = _reg(
        {"entity_id": "sensor.x", "area_id": "garage"},
        {"entity_id": "sensor.y", "labels": ["noise"]},
        {"entity_id": "sensor.z"},
    )
    cfg = VisibilityConfig(
        enabled=True,
        exclude_categories=[],
        deny_entity_ids=["sensor.z"],
        exclude_areas=["garage"],
        exclude_labels=["noise"],
    )
    assert _hidden(reg, cfg) == {"sensor.x", "sensor.y", "sensor.z"}


def test_deny_hides_entity_absent_from_registry():
    # A denied entity that has no entity-registry entry (legacy YAML / template
    # entity present only in states) must still be hidden — deny is a literal
    # entity_id match, not registry-gated. Only the registry-derived dimensions
    # (category/area/label/hidden_by) require a registry entry.
    reg = _reg({"entity_id": "sensor.registered"})
    cfg = VisibilityConfig(
        enabled=True, exclude_categories=[], deny_entity_ids=["sensor.ghost"]
    )
    assert _hidden(reg, cfg) == {"sensor.ghost"}


def test_deny_applies_over_empty_registry():
    # Empty registry list (healthy read, no entries) still honours the denylist.
    cfg = VisibilityConfig(
        enabled=True, exclude_categories=[], deny_entity_ids=["sensor.ghost"]
    )
    assert _hidden(_reg(), cfg) == {"sensor.ghost"}


def test_exclude_hidden_flag():
    reg = _reg({"entity_id": "sensor.h", "hidden_by": "user"})
    cfg = VisibilityConfig(enabled=True, exclude_categories=[], exclude_hidden=True)
    assert _hidden(reg, cfg) == {"sensor.h"}


def test_label_string_value_is_not_char_iterated():
    # A label served as a bare string (unexpected payload / mock) must count as
    # one whole label, not be char-iterated by set.intersection.
    reg = _reg({"entity_id": "sensor.s", "labels": "noise"})
    # "n" is a character of "noise" but not the whole label -> must NOT hide.
    char_cfg = VisibilityConfig(
        enabled=True, exclude_categories=[], exclude_labels=["n"]
    )
    assert _hidden(reg, char_cfg) == set()
    # The exact whole-string label DOES hide.
    exact_cfg = VisibilityConfig(
        enabled=True, exclude_categories=[], exclude_labels=["noise"]
    )
    assert _hidden(reg, exact_cfg) == {"sensor.s"}


def test_label_non_iterable_value_skips_entry_not_whole_filter():
    # A labels payload of an unexpected non-iterable type (int/dict) must not
    # raise (which would fail-open-disable the filter for every entity); the one
    # bad entry is skipped while a well-formed sibling still gets hidden.
    reg = _reg(
        {"entity_id": "sensor.bad", "labels": 5},
        {"entity_id": "sensor.good", "labels": ["noise"]},
    )
    cfg = VisibilityConfig(
        enabled=True, exclude_categories=[], exclude_labels=["noise"]
    )
    assert _hidden(reg, cfg) == {"sensor.good"}


def test_malformed_registry_returns_empty():
    cfg = VisibilityConfig(enabled=True)
    assert _hidden({"success": False}, cfg) == set()
    assert _hidden("nonsense", cfg) == set()


def test_deny_honored_on_unusable_registry():
    # deny_entity_ids is a literal entity_id match, registry-independent. A
    # transient unusable-registry read must NOT silently drop it (fail-fully-open
    # was not the goal): both degraded early returns still yield the deny seed.
    cfg = VisibilityConfig(
        enabled=True, exclude_categories=[], deny_entity_ids=["sensor.ghost"]
    )
    # non-dict / unsuccessful payload
    assert _hidden({"success": False}, cfg) == {"sensor.ghost"}
    assert _hidden("nonsense", cfg) == {"sensor.ghost"}
    # dict-success payload but result is not a list
    assert _hidden({"success": True, "result": "nope"}, cfg) == {"sensor.ghost"}


def test_degraded_registry_surfaces_warning():
    # An enabled-but-degraded read returns the deny seed AND a caller-facing
    # warning so the response can signal the degradation, not just log it.
    cfg = VisibilityConfig(
        enabled=True, exclude_categories=[], deny_entity_ids=["sensor.ghost"]
    )
    hidden, warnings = hidden_entity_ids({"success": False}, cfg)
    assert hidden == {"sensor.ghost"}
    assert any("unavailable" in w for w in warnings)


def test_unknown_category_dropped_with_warning():
    # A typo'd / unknown category is dropped (not hard-rejected) and surfaced as a
    # warning; the valid sibling category still hides.
    reg = _reg({"entity_id": "sensor.d", "entity_category": "diagnostic"})
    hidden, warnings = hidden_entity_ids(
        reg, VisibilityConfig(enabled=True, exclude_categories=["diagnostic", "typo"])
    )
    assert hidden == {"sensor.d"}
    assert any("typo" in w for w in warnings)


def test_disabled_returns_no_warnings():
    hidden, warnings = hidden_entity_ids(
        {"success": False}, VisibilityConfig(enabled=False)
    )
    assert hidden == set()
    assert warnings == []


def test_enabled_default_config_hides_both_diagnostic_and_config():
    # The shipped default exclude_categories is ["diagnostic", "config"]. Every
    # other test overrides that field, so this is the only one that fires the
    # actual default and proves BOTH default categories hide (config was never
    # exercised before).
    reg = _reg(
        {"entity_id": "sensor.diag", "entity_category": "diagnostic"},
        {"entity_id": "number.cfg", "entity_category": "config"},
        {"entity_id": "light.normal", "entity_category": None},
    )
    hidden, warnings = hidden_entity_ids(reg, VisibilityConfig(enabled=True))
    assert hidden == {"sensor.diag", "number.cfg"}
    assert warnings == []


# --- Allowlist dimension (opt-in restrict mode) ---


def _states(*entries):
    return list(entries)


def test_allowlist_area_hides_non_matching():
    reg = _reg(
        {"entity_id": "light.kitchen", "area_id": "kitchen"},
        {"entity_id": "light.bedroom", "area_id": "bedroom"},
    )
    cfg = VisibilityConfig(enabled=True, exclude_categories=[], allow_areas=["kitchen"])
    assert _hidden(reg, cfg) == {"light.bedroom"}


def test_allowlist_entity_id_literal():
    reg = _reg({"entity_id": "light.a"}, {"entity_id": "light.b"})
    cfg = VisibilityConfig(
        enabled=True, exclude_categories=[], allow_entity_ids=["light.a"]
    )
    assert _hidden(reg, cfg) == {"light.b"}


def test_allowlist_labels():
    reg = _reg(
        {"entity_id": "light.a", "labels": ["voice"]},
        {"entity_id": "light.b", "labels": ["other"]},
    )
    cfg = VisibilityConfig(enabled=True, exclude_categories=[], allow_labels=["voice"])
    assert _hidden(reg, cfg) == {"light.b"}


def test_allowlist_inactive_hides_nothing():
    # Empty allow_* => allowlist inactive => nothing hidden by it.
    reg = _reg({"entity_id": "light.a", "area_id": "x"})
    cfg = VisibilityConfig(enabled=True, exclude_categories=[])
    assert _hidden(reg, cfg) == set()


def test_allowlist_hides_states_only_entity():
    # M1 fix: a states-only entity (no registry entry) under an active allowlist
    # must be hidden unless literally allowed - the resolver reaches it via the
    # states list, not just the registry.
    reg = _reg({"entity_id": "light.kitchen", "area_id": "kitchen"})
    states = _states(
        {"entity_id": "light.kitchen"},
        {"entity_id": "sensor.yaml_only"},  # not in registry
    )
    cfg = VisibilityConfig(enabled=True, exclude_categories=[], allow_areas=["kitchen"])
    hidden, _ = hidden_entity_ids(reg, cfg, states)
    assert "sensor.yaml_only" in hidden  # states-only, not allowed => hidden
    assert "light.kitchen" not in hidden  # allowed via area


def test_allowlist_states_only_entity_allowed_by_id():
    reg = _reg()
    states = _states({"entity_id": "sensor.yaml_only"})
    cfg = VisibilityConfig(
        enabled=True, exclude_categories=[], allow_entity_ids=["sensor.yaml_only"]
    )
    hidden, _ = hidden_entity_ids(reg, cfg, states)
    assert hidden == set()


def test_allowlist_deny_still_wins():
    reg = _reg({"entity_id": "light.kitchen", "area_id": "kitchen"})
    cfg = VisibilityConfig(
        enabled=True,
        exclude_categories=[],
        allow_areas=["kitchen"],
        deny_entity_ids=["light.kitchen"],
    )
    assert _hidden(reg, cfg) == {"light.kitchen"}


def test_allow_and_exclude_compose_exclude_wins_over_allow():
    # Central invariant: hide dimensions compose. An allow dimension and an
    # exclude dimension are active simultaneously, so an entity that an allowlist
    # matches but an exclude also matches must stay hidden (exclude wins) - a
    # refactor that let restrict mode skip the exclude loop would leak it.
    reg = _reg(
        # diagnostic AND in the allowed area -> exclude must still hide it.
        {
            "entity_id": "sensor.diag_allowed",
            "entity_category": "diagnostic",
            "area_id": "kitchen",
        },
        # allowed area, not excluded -> the one entity that stays visible.
        {"entity_id": "light.kitchen", "area_id": "kitchen"},
        # not allowed, not excluded -> hidden by the allowlist restriction.
        {"entity_id": "light.bedroom", "area_id": "bedroom"},
    )
    cfg = VisibilityConfig(
        enabled=True,
        exclude_categories=["diagnostic"],
        allow_areas=["kitchen"],
    )
    assert _hidden(reg, cfg) == {"sensor.diag_allowed", "light.bedroom"}


def test_empty_registry_with_area_allowlist_degrades_open_not_blank():
    # Fail-open guard: registry success but empty + an area/label allowlist would
    # otherwise hide every states-only candidate (the fail-closed blank the design
    # forbids). The registry-derived allow dimensions degrade to open and warn.
    reg = {"success": True, "result": []}
    states = [
        {"entity_id": "light.a", "attributes": {}},
        {"entity_id": "sensor.b", "attributes": {}},
    ]
    cfg = VisibilityConfig(enabled=True, exclude_categories=[], allow_areas=["kitchen"])
    hidden, warnings = hidden_entity_ids(reg, cfg, states)
    assert hidden == set()  # not blanked
    assert any("registry returned no entries" in w for w in warnings)


def test_empty_registry_allowlist_still_honors_allow_entity_ids():
    # allow_entity_ids is registry-independent, so it keeps restricting even when
    # the area dimension degraded: only the explicitly allowed id survives.
    reg = {"success": True, "result": []}
    states = [
        {"entity_id": "light.a", "attributes": {}},
        {"entity_id": "sensor.b", "attributes": {}},
    ]
    cfg = VisibilityConfig(
        enabled=True,
        exclude_categories=[],
        allow_areas=["kitchen"],
        allow_entity_ids=["light.a"],
    )
    hidden, _ = hidden_entity_ids(reg, cfg, states)
    assert hidden == {"sensor.b"}


# --- Assist-exposure dimension (respect_assist_exposure) ---


def _assist_cfg(**kw):
    return VisibilityConfig(
        enabled=True, exclude_categories=[], respect_assist_exposure=True, **kw
    )


def test_assist_explicit_override_false_hides():
    reg = _reg({"entity_id": "light.a"})
    hidden, _ = hidden_entity_ids(reg, _assist_cfg(), None, {"light.a": False}, True)
    assert hidden == {"light.a"}


def test_assist_explicit_override_true_shows_even_diagnostic():
    # Override wins outright, even over entity_category (matches async_should_expose).
    reg = _reg({"entity_id": "sensor.x", "entity_category": "diagnostic"})
    hidden, _ = hidden_entity_ids(reg, _assist_cfg(), None, {"sensor.x": True}, True)
    assert hidden == set()


def test_assist_default_exposed_domain_shown():
    reg = _reg({"entity_id": "light.a"})
    hidden, _ = hidden_entity_ids(reg, _assist_cfg(), None, {}, True)
    assert hidden == set()  # light is a default-exposed domain


def test_assist_non_default_domain_hidden():
    reg = _reg({"entity_id": "sensor.random"})  # sensor, no device_class
    hidden, _ = hidden_entity_ids(reg, _assist_cfg(), None, {}, True)
    assert hidden == {"sensor.random"}


def test_assist_binary_sensor_device_class_shown():
    # B1 regression: binary_sensor is NOT a default domain, but device_class door
    # IS a default-exposed device class - without the device-class set this would
    # be wrongly hidden.
    reg = _reg({"entity_id": "binary_sensor.front", "device_class": "door"})
    hidden, _ = hidden_entity_ids(reg, _assist_cfg(), None, {}, True)
    assert hidden == set()


def test_assist_sensor_device_class_from_state_attr():
    # device_class absent from registry but present in state attributes (HA reads
    # it from the live entity) still counts.
    reg = _reg({"entity_id": "sensor.temp"})
    states = _states(
        {"entity_id": "sensor.temp", "attributes": {"device_class": "temperature"}}
    )
    hidden, _ = hidden_entity_ids(reg, _assist_cfg(), states, {}, True)
    assert hidden == set()


def test_assist_entity_category_blocks_default():
    # B2 regression: a default-domain entity with entity_category is not exposed.
    reg = _reg({"entity_id": "light.cfg", "entity_category": "config"})
    hidden, _ = hidden_entity_ids(reg, _assist_cfg(), None, {}, True)
    assert hidden == {"light.cfg"}


def test_assist_expose_new_off_hides_unconfigured():
    reg = _reg({"entity_id": "light.a"})
    hidden, _ = hidden_entity_ids(reg, _assist_cfg(), None, {}, False)
    assert hidden == {"light.a"}


def test_assist_missing_data_warns_and_skips():
    # respect_assist_exposure on but the seam could not supply data (None):
    # dimension is skipped (not fail-closed) and a warning surfaces.
    reg = _reg({"entity_id": "light.a"})
    hidden, warnings = hidden_entity_ids(reg, _assist_cfg(), None, None, False)
    assert hidden == set()
    assert any("Assist exposure data was unavailable" in w for w in warnings)
