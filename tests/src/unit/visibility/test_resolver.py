import logging

from ha_mcp.visibility.model import VisibilityConfig
from ha_mcp.visibility.resolver import hidden_entity_ids


def _reg(*entries):
    return {"success": True, "result": list(entries)}


def test_enabled_bad_payload_warns_and_fails_open(caplog):
    with caplog.at_level(logging.WARNING):
        assert (
            hidden_entity_ids({"success": False}, VisibilityConfig(enabled=True))
            == set()
        )
    assert any("unusable" in r.message for r in caplog.records)


def test_enabled_exception_payload_fails_open(caplog):
    # gather(return_exceptions=True) can hand an Exception object through as the
    # registry payload; it is non-dict, so the filter degrades to empty (open).
    with caplog.at_level(logging.WARNING):
        assert (
            hidden_entity_ids(RuntimeError("boom"), VisibilityConfig(enabled=True))
            == set()
        )
    assert any("unusable" in r.message for r in caplog.records)


def test_enabled_non_list_result_warns_and_fails_open(caplog):
    with caplog.at_level(logging.WARNING):
        assert (
            hidden_entity_ids(
                {"success": True, "result": "nope"}, VisibilityConfig(enabled=True)
            )
            == set()
        )
    assert any("not a list" in r.message for r in caplog.records)


def test_disabled_bad_payload_stays_silent(caplog):
    # Disabled is the default no-op; it must NOT warn on every call.
    with caplog.at_level(logging.WARNING):
        assert (
            hidden_entity_ids({"success": False}, VisibilityConfig(enabled=False))
            == set()
        )
    assert not caplog.records


def test_disabled_config_hides_nothing():
    reg = _reg({"entity_id": "sensor.a", "entity_category": "diagnostic"})
    assert hidden_entity_ids(reg, VisibilityConfig(enabled=False)) == set()


def test_category_exclude():
    reg = _reg(
        {"entity_id": "sensor.batt", "entity_category": "diagnostic"},
        {"entity_id": "light.lr", "entity_category": None},
    )
    cfg = VisibilityConfig(enabled=True, exclude_categories=["diagnostic"])
    assert hidden_entity_ids(reg, cfg) == {"sensor.batt"}


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
    assert hidden_entity_ids(reg, cfg) == {"sensor.x", "sensor.y", "sensor.z"}


def test_exclude_hidden_flag():
    reg = _reg({"entity_id": "sensor.h", "hidden_by": "user"})
    cfg = VisibilityConfig(enabled=True, exclude_categories=[], exclude_hidden=True)
    assert hidden_entity_ids(reg, cfg) == {"sensor.h"}


def test_label_string_value_is_not_char_iterated():
    # A label served as a bare string (unexpected payload / mock) must count as
    # one whole label, not be char-iterated by set.intersection.
    reg = _reg({"entity_id": "sensor.s", "labels": "noise"})
    # "n" is a character of "noise" but not the whole label -> must NOT hide.
    char_cfg = VisibilityConfig(
        enabled=True, exclude_categories=[], exclude_labels=["n"]
    )
    assert hidden_entity_ids(reg, char_cfg) == set()
    # The exact whole-string label DOES hide.
    exact_cfg = VisibilityConfig(
        enabled=True, exclude_categories=[], exclude_labels=["noise"]
    )
    assert hidden_entity_ids(reg, exact_cfg) == {"sensor.s"}


def test_malformed_registry_returns_empty():
    cfg = VisibilityConfig(enabled=True)
    assert hidden_entity_ids({"success": False}, cfg) == set()
    assert hidden_entity_ids("nonsense", cfg) == set()
