"""Long snapshot names retain their original Core config-entry identities."""

import asyncio
import errno
import hashlib
import os
import tempfile
from contextlib import nullcontext
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp.backup_manager import BackupManager, get_backup_manager
from ha_mcp.client.rest_client import HomeAssistantClient


@pytest.fixture
async def manager(
    hass: HomeAssistant, core_client: HomeAssistantClient, tmp_path: Path
) -> BackupManager:
    assert await async_setup_component(hass, "config", {})
    return get_backup_manager(
        core_client,
        SimpleNamespace(
            enable_auto_backup=True,
            auto_backup_throttle_minutes=0,
            auto_backup_retain_per_entity=1,
            auto_backup_dir=str(tmp_path),
        ),
    )


async def capture(hass: HomeAssistant, manager: BackupManager, identity: str) -> Path:
    MockConfigEntry(domain="test", entry_id=identity).add_to_hass(hass)
    path = await manager.maybe_snapshot("integration", identity, force=True)
    assert path is not None
    return path


async def test_bulk_delete_requires_the_original_identity(
    hass: HomeAssistant, manager: BackupManager
) -> None:
    identity = "a" * 113
    snapshot = await capture(hass, manager, identity)
    stem = snapshot.name.split(".")[1]

    assert manager.delete_bulk(entity_id=stem)["deleted"] == []
    assert snapshot.is_file()
    assert manager.delete_bulk(entity_id=identity)["deleted"] == [snapshot.name]


async def test_rotation_does_not_prune_another_identity_with_the_same_stem(
    hass: HomeAssistant, manager: BackupManager
) -> None:
    identity = "a" * 113
    original = await capture(hass, manager, identity)
    stem = original.name.split(".")[1]

    other = await capture(hass, manager, stem)

    assert original.is_file()
    assert other.is_file()
    assert original.name != other.name
    assert manager.read_snapshot(original.name)["entity_id"] == identity
    assert manager.read_snapshot(other.name)["entity_id"] == stem


async def test_legacy_digest_lists_both_filename_generations(
    hass: HomeAssistant, manager: BackupManager
) -> None:
    identity = f"config:{'a' * 120}"
    current = await capture(hass, manager, identity)
    digest = hashlib.sha256(identity.encode()).hexdigest()[:8]
    legacy_stem = f"{identity.replace(':', '_')}-{digest}"
    legacy = current.with_name(f"integration.{legacy_stem}.20260101_000000.yaml")
    legacy.write_bytes(current.read_bytes())

    for selector in (identity, legacy_stem):
        rows = manager.list_snapshots(domain="integration", entity_id=selector)
        assert [row["name"] for row in rows] == [current.name, legacy.name]
        assert {row["entity_id"] for row in rows} == {identity}


async def test_long_identities_get_separate_files_with_complete_headers(
    hass: HomeAssistant, manager: BackupManager
) -> None:
    common = "a" * 300
    first = await capture(hass, manager, f"{common}1")
    second = await capture(hass, manager, f"{common}2")

    assert first.name != second.name
    assert manager.read_snapshot(first.name)["entity_id"] == f"{common}1"
    assert manager.read_snapshot(second.name)["entity_id"] == f"{common}2"
    assert not list(first.parent.glob("*.tmp"))


@pytest.mark.parametrize("link_supported", [True, False])
async def test_parallel_captures_with_the_same_stem_do_not_overwrite(
    hass: HomeAssistant, manager: BackupManager, link_supported: bool
) -> None:
    longer = "a" * 113
    digest = hashlib.sha256(longer.encode()).hexdigest()[:8]
    shorter = f"{longer[:103]}-{digest}"
    both_writers = Barrier(2, timeout=15)
    mkstemp = tempfile.mkstemp

    def overlap(**kwargs: Any) -> tuple[int, str]:
        both_writers.wait()
        return mkstemp(**kwargs)

    link = (
        nullcontext()
        if link_supported
        else patch(
            "ha_mcp.backup_manager.os.link",
            side_effect=OSError(errno.EOPNOTSUPP, "hard links unavailable"),
        )
    )
    with (
        link,
        patch("ha_mcp.backup_manager._now_ts", return_value="20260101_000000"),
        patch("ha_mcp.backup_manager.tempfile", SimpleNamespace(mkstemp=overlap)),
    ):
        first, second = await asyncio.gather(
            capture(hass, manager, longer),
            capture(hass, manager, shorter),
        )

    assert first.name != second.name
    assert manager.read_snapshot(first.name)["entity_id"] == longer
    assert manager.read_snapshot(second.name)["entity_id"] == shorter


async def test_publication_preserves_a_snapshot_created_at_the_selected_name(
    hass: HomeAssistant, manager: BackupManager
) -> None:
    longer = "a" * 113
    digest = hashlib.sha256(longer.encode()).hexdigest()[:8]
    shorter = f"{longer[:103]}-{digest}"
    stamp = "20260101_000000"
    existing = await capture(hass, manager, shorter)
    payload = existing.read_bytes()
    occupied = existing.with_name(f"integration.{shorter}.{stamp}.yaml")
    existing.unlink()

    def occupy_before_publish(publish):
        def competing_write(source, target):
            if Path(target) == occupied:
                with occupied.open("xb") as stream:
                    stream.write(payload)
            return publish(source, target)

        return competing_write

    with (
        patch("ha_mcp.backup_manager._now_ts", return_value=stamp),
        patch("ha_mcp.backup_manager.os", wraps=os) as backup_os,
    ):
        backup_os.name = os.name
        backup_os.rename.side_effect = occupy_before_publish(os.rename)
        backup_os.link.side_effect = occupy_before_publish(os.link)
        written = await capture(hass, manager, longer)

    assert written.name == f"integration.{shorter}.{stamp}_01.yaml"
    assert occupied.read_bytes() == payload
    assert manager.read_snapshot(written.name)["entity_id"] == longer
    assert not list(written.parent.glob("*.tmp"))
