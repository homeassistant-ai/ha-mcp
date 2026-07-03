from ha_mcp.visibility.model import VisibilityConfig
from ha_mcp.visibility.resolver import hidden_entity_ids


def _reg(*entries):
    return {"success": True, "result": list(entries)}


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


def test_malformed_registry_returns_empty():
    cfg = VisibilityConfig(enabled=True)
    assert hidden_entity_ids({"success": False}, cfg) == set()
    assert hidden_entity_ids("nonsense", cfg) == set()
