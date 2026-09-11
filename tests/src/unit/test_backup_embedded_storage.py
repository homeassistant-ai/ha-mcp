"""Embedded backups respect HA's registered config owner without exporting trust."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from ha_mcp import backup_manager as bm
from ha_mcp import config

from .test_backup_manager import _mk_manager

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX ownership checks")


@pytest.fixture
def embedded_storage(tmp_path, monkeypatch):
    root = tmp_path / "config"
    root.mkdir(mode=0o755)
    data = root / ".ha_mcp"
    data.mkdir(mode=0o755)
    host_uid = os.geteuid() + 1000
    owners = {root: host_uid, data: host_uid}
    native_lstat = Path.lstat

    def lstat(path, *args, **kwargs):
        metadata = native_lstat(path, *args, **kwargs)
        if path in owners:
            fields = list(metadata)
            fields[4] = owners[path]
            return os.stat_result(fields)
        return metadata

    monkeypatch.setattr(Path, "lstat", lstat)
    monkeypatch.setattr(config, "get_embedded_config_dir", lambda: str(root))
    monkeypatch.setattr(bm, "get_data_dir", lambda: data)
    monkeypatch.setattr(bm, "_legacy_default_dir", lambda: tmp_path / "absent-legacy")
    return root, data, owners, host_uid


def test_embedded_default_creates_private_backup_under_host_owned_config(
    embedded_storage,
):
    root, data, _owners, _host_uid = embedded_storage
    manager = _mk_manager(data / "ignored")
    manager._settings.auto_backup_dir = ""

    snapshot = manager._write_snapshot("automation", "example", {"alias": "old"}, None)

    assert snapshot.parent == data / "backups"
    assert snapshot.stat().st_uid == os.geteuid()
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600
    assert stat.S_IMODE(snapshot.parent.stat().st_mode) == 0o700
    assert manager.read_snapshot(snapshot.name)["config"] == {"alias": "old"}
    assert stat.S_IMODE(root.stat().st_mode) == 0o755
    assert stat.S_IMODE(data.stat().st_mode) == 0o755


@pytest.mark.parametrize("location", ["external", "descendant", "leaf", "ancestor"])
def test_embedded_owner_allowance_does_not_trust_other_storage(
    embedded_storage, tmp_path, location
):
    root, data, owners, host_uid = embedded_storage
    directory = data / "backups"
    if location == "external":
        parent = tmp_path / "external"
        parent.mkdir()
        owners[parent] = host_uid
        directory = parent / "backups"
    elif location == "descendant":
        owners[data] = host_uid + 1
    elif location == "leaf":
        directory.mkdir()
        owners[directory] = host_uid
    else:
        owners[root.parent] = host_uid

    with pytest.raises(bm.UnsafeBackupStorageError, match="ownership"):
        _mk_manager(directory)._write_snapshot("automation", "example", {}, None)


@pytest.mark.parametrize("anchor", [None, "missing", "file", "loop"])
def test_environment_cannot_supply_missing_native_embedded_trust(
    embedded_storage, monkeypatch, anchor
):
    root, data, _owners, _host_uid = embedded_storage
    registered = root.parent / str(anchor) if anchor is not None else None
    if anchor == "file":
        registered.write_text("not a directory")
    elif anchor == "loop":
        registered.symlink_to(registered)
    monkeypatch.setattr(
        config,
        "get_embedded_config_dir",
        lambda: str(registered) if registered else None,
    )
    monkeypatch.setenv("HA_MCP_EMBEDDED", "1")
    monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(data))

    with pytest.raises(bm.UnsafeBackupStorageError, match="ownership"):
        _mk_manager(data / "backups")._write_snapshot("automation", "example", {}, None)
    # A missing native anchor must not break an unrelated, already trusted override.
    assert (
        _mk_manager(root.parent / "safe")
        ._write_snapshot("automation", "example", {}, None)
        .exists()
    )


@pytest.mark.parametrize("location", ["anchor", "data"])
@pytest.mark.parametrize("mode", [0o770, 0o777])
def test_embedded_config_owner_does_not_bypass_write_permissions(
    embedded_storage, location, mode
):
    root, data, _owners, _host_uid = embedded_storage
    directory = root if location == "anchor" else data
    directory.chmod(mode)

    with pytest.raises(bm.UnsafeBackupStorageError):
        _mk_manager(data / "backups")._write_snapshot("automation", "example", {}, None)

    assert stat.S_IMODE(directory.stat().st_mode) == mode
    assert not (data / "backups").exists()


@pytest.mark.parametrize("escape", ["symlink", "parent"])
def test_embedded_owner_cannot_escape_config_anchor(embedded_storage, tmp_path, escape):
    root, data, owners, host_uid = embedded_storage
    external = tmp_path / "external"
    external.mkdir()
    owners[external] = host_uid
    if escape == "symlink":
        alias = data / "alias"
        alias.symlink_to(external, target_is_directory=True)
        owners[alias] = host_uid
        directory = alias / "backups"
    else:
        directory = root / ".." / "external" / "backups"

    with pytest.raises(bm.UnsafeBackupStorageError, match="ownership"):
        _mk_manager(directory)._write_snapshot("automation", "example", {}, None)

    assert not (external / "backups").exists()


def test_embedded_alias_does_not_skip_unsafe_external_ancestry(
    embedded_storage, tmp_path
):
    root, _data, _owners, _host_uid = embedded_storage
    aliases = tmp_path / "aliases"
    aliases.mkdir()
    aliases.chmod(0o777)
    alias = aliases / "config"
    alias.symlink_to(root, target_is_directory=True)

    with pytest.raises(bm.UnsafeBackupStorageError, match="writes"):
        _mk_manager(alias / ".ha_mcp" / "backups")._write_snapshot(
            "automation", "example", {}, None
        )


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("change", ["mode", "owner"])
def test_embedded_migration_and_cached_alias_revalidate_canonical_storage(
    embedded_storage, tmp_path, enabled, change
):
    _root, data, owners, host_uid = embedded_storage
    directory = data / "backups"
    initial = _mk_manager(directory)
    snapshot = initial._write_snapshot("automation", "example", {"alias": "old"}, None)
    snapshot.chmod(0o644)
    alias = data / "alias"
    alias.symlink_to(directory, target_is_directory=True)
    owners[alias] = host_uid
    manager = _mk_manager(alias, enable_auto_backup=enabled)

    manager._prepare_directory()

    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600
    external = tmp_path / "external"
    external.mkdir()
    owners[external] = host_uid
    alias.unlink()
    alias.symlink_to(external, target_is_directory=True)
    assert manager.read_snapshot(snapshot.name)["config"] == {"alias": "old"}
    assert len(manager.list_snapshots()) == 1
    if change == "mode":
        data.chmod(0o777)
    else:
        owners[data] = host_uid + 1
    with pytest.raises(bm.UnsafeBackupStorageError):
        manager.read_snapshot(snapshot.name)
