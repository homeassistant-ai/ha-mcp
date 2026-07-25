from ha_mcp.visibility.model import VisibilityConfig
from ha_mcp.visibility.resolver import config_has_active_hide_dimensions


def test_defaults_are_disabled_and_noop():
    cfg = VisibilityConfig()
    assert cfg.enabled is False
    assert cfg.version == 1
    assert cfg.exclude_categories == ["diagnostic", "config"]
    assert cfg.deny_entity_ids == []
    assert cfg.enforce is False


def test_roundtrips_through_json():
    cfg = VisibilityConfig(enabled=True, deny_entity_ids=["sensor.x"])
    dumped = cfg.model_dump(mode="json")
    assert VisibilityConfig.model_validate(dumped) == cfg


def test_enforce_roundtrips_through_json():
    cfg = VisibilityConfig(enabled=True, enforce=True, deny_entity_ids=["sensor.x"])
    dumped = cfg.model_dump(mode="json")
    assert dumped["enforce"] is True
    assert VisibilityConfig.model_validate(dumped) == cfg


def test_enforce_is_not_a_wire_dimension():
    """enforce changes how strongly hiding applies, not which entities hide, so it
    is deliberately absent from the component wire format."""
    assert "enforce" not in VisibilityConfig(enforce=True).to_wire()


def test_enforce_is_not_an_active_hide_dimension():
    """enforce alone (no hide dimension) hides nothing — it must not make an empty
    config count as active, or every search would drop off the component fast path."""
    cfg = VisibilityConfig(enabled=True, enforce=True, exclude_categories=[])
    assert config_has_active_hide_dimensions(cfg) is False
