import pytest

from ha_mcp.visibility.model import VisibilityConfig
from ha_mcp.visibility.persistence import (
    load_visibility_config,
    save_visibility_config,
)


def test_absent_file_returns_default(tmp_path):
    assert load_visibility_config(tmp_path) == VisibilityConfig()


def test_save_then_load_roundtrip_bumps_version(tmp_path):
    save_visibility_config(tmp_path, VisibilityConfig(version=1, enabled=True))
    loaded = load_visibility_config(tmp_path)
    assert loaded.enabled is True
    assert loaded.version == 2  # save bumps


def test_corrupt_json_raises_valueerror(tmp_path):
    (tmp_path / "entity_visibility.json").write_text("{ not json")
    with pytest.raises(ValueError):
        load_visibility_config(tmp_path)
