"""Template identity validation scales with changed files, including legacy aliases."""

from unittest.mock import Mock

import pytest
import yaml

from .test_template_backup_review import _config
from .test_template_backup_review import manager as manager


@pytest.mark.parametrize("unrelated_count", [10, 100])
def test_rotation_does_not_reparse_unchanged_histories(
    manager, monkeypatch, unrelated_count
):
    manager._settings.auto_backup_retain_per_entity = 2
    for number in range(unrelated_count):
        entry_id = f"other-{number}"
        manager._write_snapshot(
            "helper_template", entry_id, _config(entry_id=entry_id), "test"
        )
    legacy = manager._write_snapshot(
        "helper_template", "sensor.old_name", _config(), "test"
    )
    current = manager._write_snapshot(
        "helper_template", "template-entry", _config(), "test"
    )
    read = Mock(wraps=manager.read_snapshot)
    monkeypatch.setattr(manager, "read_snapshot", read)
    manager._rotate("helper_template", "template-entry")
    assert (
        read.call_count == unrelated_count + 2
    )  # Discover and validate all history once.
    read.reset_mock()
    manager._rotate("helper_template", "template-entry")
    assert read.call_count == 0, (
        "Unchanged histories should not require full YAML parsing"
    )
    added = manager._write_snapshot(
        "helper_template", "template-entry", _config(), "test"
    )
    manager._rotate("helper_template", "template-entry")
    assert read.call_count == 1, (
        "Only the newly captured file needs identity validation"
    )
    assert len(list(manager.backup_dir.glob("*.yaml"))) == unrelated_count + 2
    assert added.exists()
    assert len([path for path in (legacy, current) if path.exists()]) == 1


def test_changed_legacy_identity_is_revalidated_before_filter_or_delete(manager):
    path = manager._write_snapshot(
        "helper_template", "sensor.reused", _config(), "test"
    )
    assert manager.list_snapshots(entity_id="template-entry")
    data = manager.read_snapshot(path.name)
    data["config"]["entry_id"] = "a-different-entry"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert manager.list_snapshots(entity_id="template-entry") == []
    assert manager.delete_bulk(entity_id="template-entry")["deleted"] == []
    assert manager.list_snapshots(entity_id="a-different-entry")[0]["name"] == path.name
    assert manager.delete_bulk(entity_id="a-different-entry")["deleted"] == [path.name]


def test_invalid_changed_identity_is_not_rotated_or_deleted(manager):
    path = manager._write_snapshot(
        "helper_template", "template-entry", _config(), "test"
    )
    assert manager.list_snapshots(entity_id="template-entry")
    data = manager.read_snapshot(path.name)
    data["config"]["entry_id"] = "conflicting-entry"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    manager._settings.auto_backup_retain_per_entity = 1
    manager._write_snapshot("helper_template", "template-entry", _config(), "test")
    manager._rotate("helper_template", "template-entry")
    manager.delete_bulk(entity_id="template-entry")
    assert path.exists()


def test_imported_legacy_history_is_discovered_after_rotation(manager):
    manager._settings.auto_backup_retain_per_entity = 1
    current = manager._write_snapshot(
        "helper_template", "template-entry", _config(), "test"
    )
    manager._rotate("helper_template", "template-entry")
    legacy = manager.backup_dir / "helper_template.sensor.old_name.20200101_000000.yaml"
    data = manager.read_snapshot(current.name)
    data["entity_id"] = "sensor.old_name"
    legacy.write_text(yaml.safe_dump(data), encoding="utf-8")
    manager._rotate("helper_template", "template-entry")
    assert current.exists()
    assert not legacy.exists()


@pytest.mark.parametrize("delete_method", ["single", "bulk", "external"])
def test_reused_filename_does_not_keep_deleted_identity(manager, delete_method):
    path = manager._write_snapshot(
        "helper_template", "sensor.reused", _config(), "test"
    )
    assert manager.list_snapshots(entity_id="template-entry")
    data = manager.read_snapshot(path.name)
    if delete_method == "single":
        manager.delete_snapshot(path.name)
    elif delete_method == "bulk":
        manager.delete_bulk(entity_id="template-entry")
    else:
        path.unlink()
    data["config"]["entry_id"] = "a-different-entry"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert manager.list_snapshots(entity_id="template-entry") == []
    assert manager.list_snapshots(entity_id="a-different-entry")[0]["name"] == path.name


def test_identity_changed_during_read_is_not_used_for_deletion(manager, monkeypatch):
    path = manager._write_snapshot(
        "helper_template", "sensor.reused", _config(), "test"
    )
    original_read = manager.read_snapshot

    def read_then_replace(name):
        data = original_read(name)
        changed = {**data, "config": _config(entry_id="different-entry")}
        path.write_text(yaml.safe_dump(changed), encoding="utf-8")
        return data

    monkeypatch.setattr(manager, "read_snapshot", read_then_replace)
    assert manager.delete_bulk(entity_id="template-entry")["deleted"] == []
    assert path.exists()
    monkeypatch.setattr(manager, "read_snapshot", original_read)
    assert manager.list_snapshots(entity_id="different-entry")[0]["name"] == path.name
