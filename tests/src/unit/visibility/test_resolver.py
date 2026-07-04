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
        assert (
            _hidden({"success": False}, VisibilityConfig(enabled=True))
            == set()
        )
    assert any("unusable" in r.message for r in caplog.records)


def test_enabled_exception_payload_fails_open(caplog):
    # gather(return_exceptions=True) can hand an Exception object through as the
    # registry payload; it is non-dict, so the filter degrades to empty (open).
    with caplog.at_level(logging.WARNING):
        assert (
            _hidden(RuntimeError("boom"), VisibilityConfig(enabled=True))
            == set()
        )
    assert any("unusable" in r.message for r in caplog.records)


def test_enabled_non_list_result_warns_and_fails_open(caplog):
    with caplog.at_level(logging.WARNING):
        assert (
            _hidden(
                {"success": True, "result": "nope"}, VisibilityConfig(enabled=True)
            )
            == set()
        )
    assert any("not a list" in r.message for r in caplog.records)


def test_disabled_bad_payload_stays_silent(caplog):
    # Disabled is the default no-op; it must NOT warn on every call.
    with caplog.at_level(logging.WARNING):
        assert (
            _hidden({"success": False}, VisibilityConfig(enabled=False))
            == set()
        )
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
    assert (
        _hidden({"success": True, "result": "nope"}, cfg)
        == {"sensor.ghost"}
    )


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
