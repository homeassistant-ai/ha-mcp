from ha_mcp.visibility.model import VisibilityConfig


def test_defaults_are_disabled_and_noop():
    cfg = VisibilityConfig()
    assert cfg.enabled is False
    assert cfg.version == 1
    assert cfg.exclude_categories == ["diagnostic", "config"]
    assert cfg.deny_entity_ids == []


def test_roundtrips_through_json():
    cfg = VisibilityConfig(enabled=True, deny_entity_ids=["sensor.x"])
    dumped = cfg.model_dump(mode="json")
    assert VisibilityConfig.model_validate(dumped) == cfg
