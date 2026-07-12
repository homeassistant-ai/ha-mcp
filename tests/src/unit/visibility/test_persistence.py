import os

import pytest

from ha_mcp.visibility import persistence
from ha_mcp.visibility.model import VisibilityConfig
from ha_mcp.visibility.persistence import (
    VISIBILITY_FILENAME,
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


def test_same_signature_reuses_parsed_config(tmp_path, monkeypatch):
    """Two loads of an unchanged file parse once and hand back the same object."""
    save_visibility_config(tmp_path, VisibilityConfig(enabled=True))

    parse_calls = 0
    real_loads = persistence.json.loads

    def counting_loads(*args, **kwargs):
        nonlocal parse_calls
        parse_calls += 1
        return real_loads(*args, **kwargs)

    monkeypatch.setattr(persistence.json, "loads", counting_loads)
    first = load_visibility_config(tmp_path)
    second = load_visibility_config(tmp_path)

    assert first.enabled is True
    assert second is first  # cached parse reused, not re-validated
    assert parse_calls == 1  # parsed exactly once across the two loads


def test_changed_mtime_reloads(tmp_path):
    """A same-size rewrite with a bumped mtime invalidates the memo (reloads)."""
    path = tmp_path / VISIBILITY_FILENAME
    # Two 17-byte payloads so only the mtime axis of the signature moves.
    path.write_text('{"enabled": true}', encoding="utf-8")
    first = load_visibility_config(tmp_path)
    assert first.enabled is True

    path.write_text('{"enabled":false}', encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    second = load_visibility_config(tmp_path)

    assert second.enabled is False  # stale parse not served
    assert second is not first
