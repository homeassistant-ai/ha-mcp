"""Backup storage is private before configuration bytes reach the filesystem."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ha_mcp import backup_manager as bm

from .test_backup_manager import _mk_manager


@pytest.mark.parametrize("operation", ["check", "capture"])
def test_new_backup_leaf_requests_private_directory_mode(
    tmp_path, monkeypatch, operation
):
    directory = tmp_path / "backups"
    manager = _mk_manager(directory)
    native_mkdir = Path.mkdir
    modes = []

    def mkdir(path, mode=0o777, *args, **kwargs):
        if path == directory:
            modes.append(mode)
        return native_mkdir(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    if operation == "check":
        manager._check_directory()
    else:
        manager._write_snapshot("automation", "example", {}, None)

    assert modes and set(modes) == {0o700}


def test_snapshot_temp_is_exclusively_created_private_before_writing(
    tmp_path, monkeypatch
):
    manager = _mk_manager(tmp_path)
    native_open = os.open
    creations = []

    def open_file(path, flags, mode=0o777, **kwargs):
        if flags & os.O_CREAT:
            creations.append((Path(path), flags, mode))
            assert mode == 0o600
            assert flags & os.O_EXCL
            assert not Path(path).exists()
        return native_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(os, "open", open_file)
    snapshot = manager._write_snapshot(
        "helper_template", "entry", {"options": {"state": "private value"}}, None
    )

    assert len(creations) == 1
    assert creations[0][0].parent == tmp_path
    assert creations[0][0] != snapshot.with_suffix(".yaml.tmp")
    assert (
        manager.read_snapshot(snapshot.name)["config"]["options"]["state"]
        == "private value"
    )


def test_snapshot_does_not_reuse_an_existing_predictable_temp(tmp_path, monkeypatch):
    monkeypatch.setattr(bm, "_now_ts", lambda: "20260910_000000")
    leftover = tmp_path / "automation.example.20260910_000000.yaml.tmp"
    leftover.write_text("unrelated unfinished write", encoding="utf-8")

    snapshot = _mk_manager(tmp_path)._write_snapshot("automation", "example", {}, None)

    assert snapshot.exists()
    assert leftover.read_text(encoding="utf-8") == "unrelated unfinished write"


@pytest.mark.parametrize("failure", ["fdopen", "write", "replace"])
def test_failed_snapshot_removes_only_its_temporary_file(
    tmp_path, monkeypatch, failure
):
    retained = tmp_path / "retained.yaml"
    retained.write_text("retained", encoding="utf-8")
    manager = _mk_manager(tmp_path)
    native_fdopen = os.fdopen

    class FailedWrite:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def write(self, content):
            self.stream.write(content[:20])
            raise OSError("disk full")

        def __exit__(self, *args):
            self.stream.close()

    if failure == "fdopen":

        def fail_fdopen(fd, *args):
            raise OSError("fdopen failed")

        monkeypatch.setattr(os, "fdopen", fail_fdopen)
    elif failure == "write":
        monkeypatch.setattr(
            os,
            "fdopen",
            lambda *args, **kwargs: FailedWrite(native_fdopen(*args, **kwargs)),
        )
    else:

        def fail_replace(*args):
            raise OSError("replace failed")

        monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(OSError, match=r"disk full|replace failed|fdopen failed"):
        manager._write_snapshot("automation", "example", {}, None)

    assert list(tmp_path.iterdir()) == [retained]
    assert retained.read_text(encoding="utf-8") == "retained"


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership and mode bits")
def test_posix_snapshot_and_new_directory_are_private_under_open_umask(tmp_path):
    directory = tmp_path / "backups"
    old_umask = os.umask(0)
    try:
        snapshot = _mk_manager(directory)._write_snapshot(
            "automation", "example", {}, None
        )
    finally:
        os.umask(old_umask)

    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership and mode bits")
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_posix_migration_restricts_only_owned_regular_snapshots(tmp_path, newline):
    manager = _mk_manager(tmp_path)
    snapshot = manager._write_snapshot("automation", "example", {}, None)
    snapshot.write_bytes(snapshot.read_text().replace("\n", newline).encode())
    snapshot.chmod(0o644)
    leftover = snapshot.with_suffix(".yaml.tmp")
    leftover.write_text(snapshot.read_text(), encoding="utf-8")
    leftover.chmod(0o644)
    read_only = tmp_path / "automation.readonly.20260910_000000.yaml"
    read_only.write_text(snapshot.read_text(), encoding="utf-8")
    read_only.chmod(0o444)
    tmp_path.chmod(0o755)
    unrelated = tmp_path / "notes.yaml"
    unrelated.write_text("notes", encoding="utf-8")
    impostor = tmp_path / "automation.other.20260910_000000.yaml"
    impostor.write_text("unrelated configuration", encoding="utf-8")
    outside = tmp_path.parent / f"{tmp_path.name}-outside.yaml"
    outside.write_text("# ha_mcp_backup\nsecret: outside", encoding="utf-8")
    link = tmp_path / "automation.link.20260910_000000.yaml"
    link.symlink_to(outside)
    hardlink = tmp_path / "automation.hardlink.20260910_000000.yaml"
    hardlink.hardlink_to(outside)
    nested = tmp_path / "nested"
    nested.mkdir()
    nested_snapshot = nested / snapshot.name
    nested_snapshot.write_text(snapshot.read_text(), encoding="utf-8")
    others = [unrelated, impostor, outside, nested_snapshot]
    for path in others:
        path.chmod(0o644)

    _mk_manager(tmp_path)._check_directory()

    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600
    assert stat.S_IMODE(leftover.stat().st_mode) == 0o600
    assert leftover.read_text() == snapshot.read_text()
    assert stat.S_IMODE(read_only.stat().st_mode) == 0o400
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o755
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o644 for path in others)
    assert link.is_symlink()


@pytest.mark.parametrize(
    ("mode", "owner", "links", "expected"),
    [
        (stat.S_IFREG | 0o644, 123, 1, True),
        (stat.S_IFREG | 0o644, 456, 1, False),
        (stat.S_IFREG | 0o644, 123, 2, False),
        (stat.S_IFLNK | 0o777, 123, 1, False),
        (stat.S_IFDIR | 0o755, 123, 1, False),
        (stat.S_IFREG | 0o000, 123, 1, False),
        (stat.S_IFREG | 0o400, 123, 1, False),
    ],
)
def test_migration_permission_filter(mode, owner, links, expected, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 123, raising=False)
    metadata = SimpleNamespace(st_mode=mode, st_uid=owner, st_nlink=links)
    assert bm._snapshot_needs_restriction(metadata) is expected


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory aliases and modes")
def test_posix_migration_supports_configured_directory_alias(tmp_path):
    directory = tmp_path / "backups"
    snapshot = _mk_manager(directory)._write_snapshot("automation", "example", {}, None)
    snapshot.chmod(0o644)
    alias = tmp_path / "alias"
    alias.symlink_to(directory, target_is_directory=True)

    manager = _mk_manager(alias)
    manager._check_directory()

    assert manager.init_dir_error is None
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600
    assert alias.is_symlink()


@pytest.mark.skipif(os.name != "posix", reason="POSIX nofollow descriptor semantics")
def test_posix_migration_never_follows_a_replacement_symlink(tmp_path, monkeypatch):
    snapshot = _mk_manager(tmp_path)._write_snapshot("automation", "example", {}, None)
    snapshot.chmod(0o644)
    outside = tmp_path.parent / f"{tmp_path.name}-outside.yaml"
    outside.write_text(snapshot.read_text(), encoding="utf-8")
    outside.chmod(0o644)
    native_open = os.open

    def open_file(path, flags, *args, **kwargs):
        if path == snapshot.name and kwargs.get("dir_fd") is not None:
            snapshot.unlink()
            snapshot.symlink_to(outside)
        return native_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", open_file)
    manager = _mk_manager(tmp_path)
    manager._check_directory()

    assert manager.init_dir_error is None
    assert stat.S_IMODE(outside.stat().st_mode) == 0o644
    assert snapshot.is_symlink()


def _unsafe_snapshot_store(tmp_path, placement, mode):
    """Seed trusted storage, then make one relevant directory replaceable."""
    namespace = tmp_path / "namespace"
    namespace.mkdir(mode=0o700)
    directory = namespace / "backups"
    manager = _mk_manager(directory)
    snapshot = manager._write_snapshot(
        "automation", "example", {"value": "original"}, None
    )
    configured = directory
    unsafe = directory if placement == "leaf" else namespace
    if placement.startswith("alias"):
        alias_parent = tmp_path / "aliases"
        alias_parent.mkdir(mode=0o700)
        alias = alias_parent / "backups"
        alias.symlink_to(directory, target_is_directory=True)
        configured = alias
        unsafe = alias_parent
        if placement == "alias_hop":
            configured = tmp_path / "outer-alias"
            configured.symlink_to(alias, target_is_directory=True)
    unsafe.chmod(mode)
    return configured, unsafe, snapshot


@pytest.mark.skipif(
    os.name != "posix", reason="POSIX directory replacement permissions"
)
@pytest.mark.parametrize("placement", ["leaf", "parent", "alias", "alias_hop"])
@pytest.mark.parametrize("mode", [0o770, 0o777])
def test_posix_storage_health_refuses_replaceable_directory(tmp_path, placement, mode):
    configured, unsafe, snapshot = _unsafe_snapshot_store(tmp_path, placement, mode)
    before = snapshot.read_bytes()
    manager = _mk_manager(configured)

    manager._check_directory()

    assert manager.init_dir_error is not None
    assert not manager.enabled
    assert stat.S_IMODE(unsafe.stat().st_mode) == mode
    assert snapshot.read_bytes() == before


@pytest.mark.skipif(
    os.name != "posix", reason="POSIX directory replacement permissions"
)
@pytest.mark.parametrize("operation", ["read", "list", "write", "delete", "rotate"])
@pytest.mark.parametrize("enabled", [False, True])
def test_posix_snapshot_access_rechecks_directory_trust(tmp_path, operation, enabled):
    directory = tmp_path / "backups"
    manager = _mk_manager(directory, enable_auto_backup=enabled)
    snapshot = manager._write_snapshot("automation", "example", {}, None)
    assert manager._directory_checked
    directory.chmod(0o777)
    before = snapshot.read_bytes()

    with pytest.raises(OSError):
        if operation == "read":
            manager.read_snapshot(snapshot.name)
        elif operation == "list":
            manager.list_snapshots()
        elif operation == "write":
            manager._write_snapshot("automation", "example", {}, None)
        elif operation == "delete":
            manager.delete_snapshot(snapshot.name)
        else:
            manager._rotate("automation", "example")

    assert stat.S_IMODE(directory.stat().st_mode) == 0o777
    assert snapshot.read_bytes() == before


@pytest.mark.skipif(
    os.name != "posix", reason="POSIX directory replacement permissions"
)
def test_posix_private_file_mode_does_not_make_replaced_snapshot_trustworthy(tmp_path):
    configured, unsafe, snapshot = _unsafe_snapshot_store(tmp_path, "leaf", 0o777)
    replacement = configured / "replacement"
    replacement.write_text(
        snapshot.read_text().replace("value: original", "value: replaced"),
        encoding="utf-8",
    )
    replacement.chmod(0o600)
    os.replace(replacement, snapshot)
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600
    assert "value: replaced" in snapshot.read_text()

    with pytest.raises(OSError):
        _mk_manager(configured, enable_auto_backup=False).read_snapshot(snapshot.name)

    assert stat.S_IMODE(unsafe.stat().st_mode) == 0o777


@pytest.mark.skipif(
    os.name != "posix", reason="POSIX directory replacement permissions"
)
@pytest.mark.parametrize("operation", ["diff", "restore"])
@pytest.mark.parametrize("enabled", [False, True])
async def test_posix_restore_and_diff_refuse_untrusted_storage_before_ha_access(
    tmp_path, operation, enabled
):
    configured, _, snapshot = _unsafe_snapshot_store(tmp_path, "leaf", 0o777)
    manager = _mk_manager(configured, enable_auto_backup=enabled)
    fetch = AsyncMock(return_value={"value": "live"})
    restore = AsyncMock(return_value={"value": "restored"})
    manager.register(
        bm.DomainHandler(domain="automation", fetch=fetch, restore=restore)
    )

    if operation == "diff":
        with pytest.raises(OSError):
            await manager.snapshot_comparison(snapshot.name)
    else:
        with pytest.raises(bm.BackupRestoreError) as caught:
            await manager.restore_snapshot(snapshot.name)
        assert caught.value.outcome["reason"] == "unsafe_backup_storage"
        assert caught.value.outcome["apply_status"] == "not_applied"

    fetch.assert_not_awaited()
    restore.assert_not_awaited()


@pytest.mark.skipif(
    os.name != "posix", reason="POSIX directory replacement permissions"
)
@pytest.mark.parametrize("mode", [0o700, 0o755])
@pytest.mark.parametrize("parent_mode", [0o700, 0o1777])
def test_posix_storage_accepts_nonwritable_leaf_and_protected_sticky_parent(
    tmp_path, mode, parent_mode
):
    parent = tmp_path / "namespace"
    parent.mkdir(mode=0o700)
    directory = parent / "backups"
    directory.mkdir(mode=mode)
    parent.chmod(parent_mode)
    manager = _mk_manager(directory)

    manager._check_directory()

    assert manager.init_dir_error is None
    assert manager.enabled
    assert stat.S_IMODE(directory.stat().st_mode) == mode
    assert stat.S_IMODE(parent.stat().st_mode) == parent_mode


@pytest.mark.skipif(
    os.name != "posix", reason="POSIX directory replacement permissions"
)
@pytest.mark.parametrize("operation", ["check", "capture"])
def test_posix_unsafe_parent_refused_before_creating_backup_directory(
    tmp_path, operation
):
    parent = tmp_path / "replaceable"
    parent.mkdir(mode=0o700)
    parent.chmod(0o777)
    directory = parent / "not-created"
    manager = _mk_manager(directory)

    if operation == "check":
        manager._check_directory()
        assert manager.init_dir_error is not None
        assert not manager.enabled
    else:
        with pytest.raises(OSError):
            manager._write_snapshot("automation", "example", {}, None)

    assert not directory.exists()
    assert stat.S_IMODE(parent.stat().st_mode) == 0o777


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory ownership")
@pytest.mark.parametrize("foreign", ["leaf", "sticky_child", "sticky_link"])
def test_posix_foreign_directory_or_sticky_child_is_not_trusted(
    tmp_path, monkeypatch, foreign
):
    parent = tmp_path / "namespace"
    parent.mkdir(mode=0o700)
    child = parent / "child"
    child.mkdir(mode=0o755)
    directory = child / "backups"
    snapshot = _mk_manager(directory)._write_snapshot("automation", "example", {}, None)
    configured = directory
    untrusted = directory
    if foreign == "sticky_child":
        parent.chmod(0o1777)
        untrusted = child
    elif foreign == "sticky_link":
        alias_parent = tmp_path / "aliases"
        alias_parent.mkdir(mode=0o700)
        alias_parent.chmod(0o1777)
        configured = alias_parent / "backups"
        configured.symlink_to(directory, target_is_directory=True)
        untrusted = configured
    native_lstat = Path.lstat

    def lstat(path, *args, **kwargs):
        metadata = native_lstat(path, *args, **kwargs)
        if path == untrusted:
            fields = list(metadata)
            fields[4] = os.geteuid() + 1
            return os.stat_result(fields)
        return metadata

    monkeypatch.setattr(Path, "lstat", lstat)
    manager = _mk_manager(configured)

    manager._check_directory()

    assert manager.init_dir_error is not None
    assert not manager.enabled
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert snapshot.exists()


@pytest.mark.skipif(
    os.name != "posix", reason="POSIX directory replacement permissions"
)
@pytest.mark.parametrize("mandatory", [False, True])
async def test_posix_forced_capture_refuses_unsafe_storage_when_disabled(
    tmp_path, mandatory
):
    configured, unsafe, snapshot = _unsafe_snapshot_store(tmp_path, "leaf", 0o777)
    manager = _mk_manager(configured, enable_auto_backup=False)
    manager.register(
        bm.DomainHandler(
            domain="automation",
            fetch=AsyncMock(return_value={"private-value": "must not be written"}),
            restore=AsyncMock(),
        )
    )
    error_type = bm.MandatoryBackupError if mandatory else bm.UnsafeBackupStorageError

    with pytest.raises(error_type) as caught:
        await manager.maybe_snapshot(
            "automation", "example", force=True, mandatory=mandatory
        )

    assert "private-value" not in str(caught.value)
    assert list(configured.iterdir()) == [snapshot]
    assert stat.S_IMODE(unsafe.stat().st_mode) == 0o777


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory alias ownership")
def test_posix_legacy_default_cannot_hide_unsafe_alias_ancestry(tmp_path, monkeypatch):
    configured, unsafe, snapshot = _unsafe_snapshot_store(tmp_path, "alias_hop", 0o777)
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.setattr(bm, "_legacy_default_dir", lambda: configured)
    monkeypatch.setattr(bm, "get_data_dir", lambda: tmp_path / "new-default")

    with pytest.raises(bm.UnsafeBackupStorageError):
        bm._resolve_default_dir()

    assert snapshot.exists()
    assert stat.S_IMODE(unsafe.stat().st_mode) == 0o777


@pytest.mark.skipif(
    os.name != "posix", reason="POSIX directory replacement permissions"
)
async def test_posix_mandatory_capture_revalidates_storage_before_throttle(tmp_path):
    directory = tmp_path / "backups"
    manager = _mk_manager(directory, auto_backup_throttle_minutes=10)
    fetch = AsyncMock(return_value={"value": "original"})
    manager.register(
        bm.DomainHandler(domain="automation", fetch=fetch, restore=AsyncMock())
    )
    snapshot = await manager.maybe_snapshot("automation", "example", mandatory=True)
    assert snapshot is not None
    directory.chmod(0o777)

    with pytest.raises(bm.MandatoryBackupError):
        await manager.maybe_snapshot("automation", "example", mandatory=True)

    fetch.assert_awaited_once()
    assert list(directory.iterdir()) == [snapshot]
    assert stat.S_IMODE(directory.stat().st_mode) == 0o777


@pytest.mark.skipif(os.name != "posix", reason="POSIX snapshot symlink semantics")
@pytest.mark.parametrize(
    "operation", ["read", "diff", "restore", "list", "rotate", "delete"]
)
async def test_posix_snapshot_symlink_cannot_bypass_directory_trust(
    tmp_path, monkeypatch, operation
):
    manager = _mk_manager(tmp_path, auto_backup_retain_per_entity=1)
    monkeypatch.setattr(bm, "_now_ts", lambda: "20200101_000000")
    snapshot = manager._write_snapshot(
        "automation", "example", {"value": "original"}, None
    )
    nested = tmp_path / "untrusted"
    nested.mkdir(mode=0o700)
    target = nested / "snapshot.yaml"
    original = snapshot.read_bytes()
    target.write_bytes(original)
    snapshot.unlink()
    snapshot.symlink_to(target)
    nested.chmod(0o777)
    monkeypatch.setattr(bm, "_now_ts", lambda: "20200101_000001")
    retained = manager._write_snapshot("automation", "example", {}, None)
    fetch = AsyncMock(return_value={"value": "live"})
    restore = AsyncMock(return_value={})
    manager.register(
        bm.DomainHandler(domain="automation", fetch=fetch, restore=restore)
    )
    opened = []
    native_open = Path.open

    def open_file(path, *args, **kwargs):
        if path in (snapshot, target):
            opened.append(path)
        return native_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_file)
    if operation == "restore":
        with pytest.raises(bm.BackupRestoreError) as caught:
            await manager.restore_snapshot(snapshot.name)
        assert caught.value.outcome["reason"] == "unsafe_backup_storage"
        assert caught.value.outcome["apply_status"] == "not_applied"
    elif operation in ("read", "diff", "delete"):
        with pytest.raises(OSError):
            if operation == "read":
                manager.read_snapshot(snapshot.name)
            elif operation == "delete":
                manager.delete_snapshot(snapshot.name)
            else:
                await manager.snapshot_comparison(snapshot.name)
    else:
        try:
            if operation == "list":
                assert snapshot.name not in {
                    row["name"] for row in manager.list_snapshots()
                }
            else:
                manager._rotate("automation", "example")
        except bm.UnsafeBackupStorageError:
            pass

    assert not opened
    fetch.assert_not_awaited()
    restore.assert_not_awaited()
    assert snapshot.is_symlink()
    assert retained.exists()
    assert target.read_bytes() == original


@pytest.mark.skipif(os.name != "posix", reason="POSIX legacy storage discovery")
def test_posix_absent_legacy_store_does_not_block_safe_default(tmp_path, monkeypatch):
    legacy_parent = tmp_path / "legacy"
    legacy_parent.mkdir(mode=0o700)
    legacy_parent.chmod(0o777)
    legacy = legacy_parent / "ha_mcp" / "backups"
    data_dir = tmp_path / "safe-data"
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.setattr(bm, "_legacy_default_dir", lambda: legacy)
    monkeypatch.setattr(bm, "get_data_dir", lambda: data_dir)

    assert bm._resolve_default_dir() == data_dir / "backups"
    assert not legacy.exists()
    assert stat.S_IMODE(legacy_parent.stat().st_mode) == 0o777
