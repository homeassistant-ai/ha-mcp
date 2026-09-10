"""Backup storage is private before configuration bytes reach the filesystem."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace

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
