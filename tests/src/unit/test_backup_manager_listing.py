"""Listing, reading, deleting and restoring snapshots, and where they live.

The snapshots are of a real label and a real area in Core's registries, taken
and restored through the backup manager's public calls.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import label_registry as lr

from ha_mcp import backup_manager as bm
from ha_mcp import config
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.utils.data_paths import get_data_dir

from ._backup_core import (
    add_label,
    captured_name,
    manage_backup,
    place_snapshot,
    snapshot_of,
)
from ._backup_core import backup_settings as backup_settings
from ._backup_core import snapshot_manager as snapshot_manager


async def test_invalid_listed_names_are_reported_without_aborting_a_bulk_delete(
    snapshot_manager: bm.BackupManager, label_registry: lr.LabelRegistry
) -> None:
    valid = await snapshot_of(snapshot_manager, add_label(label_registry, "Valid"))
    invalid = place_snapshot(
        snapshot_manager.backup_dir,
        "label.invalid..name.20260903_120000.yaml",
        "invalid..name",
        domain="label",
        text=False,
    )

    result = snapshot_manager.delete_bulk(domain="label")

    assert result == {"deleted": [valid.name], "failed": [invalid.name]}
    assert invalid.exists()


async def test_list_filters_by_domain(
    snapshot_manager: bm.BackupManager,
    label_registry: lr.LabelRegistry,
    area_registry: ar.AreaRegistry,
) -> None:
    await snapshot_of(snapshot_manager, add_label(label_registry, "Kitchen"))
    area = area_registry.async_create("Pantry")
    assert await snapshot_manager.maybe_snapshot("area_or_floor", f"area:{area.id}")

    assert len(snapshot_manager.list_snapshots()) == 2
    (only_labels,) = snapshot_manager.list_snapshots(domain="label")
    assert only_labels["domain"] == "label"


async def test_list_filters_by_entity_id(
    snapshot_manager: bm.BackupManager, label_registry: lr.LabelRegistry
) -> None:
    alpha = add_label(label_registry, "Alpha")
    await snapshot_of(snapshot_manager, alpha)
    await snapshot_of(snapshot_manager, add_label(label_registry, "Beta"))

    (only_alpha,) = snapshot_manager.list_snapshots(entity_id=alpha)

    assert only_alpha["entity_id"] == alpha


async def test_read_snapshot_returns_the_full_payload(
    snapshot_manager: bm.BackupManager, label_registry: lr.LabelRegistry
) -> None:
    kitchen = add_label(label_registry, "Kitchen")
    path = await snapshot_of(snapshot_manager, kitchen, tool_name="t")

    data = snapshot_manager.read_snapshot(path.name)

    assert data["domain"] == "label"
    assert data["entity_id"] == kitchen
    assert data["config"]["name"] == "Kitchen"


@pytest.mark.parametrize("name", ["../../etc/passwd", "subdir/file.yaml"])
async def test_read_snapshot_rejects_path_traversal(
    snapshot_manager: bm.BackupManager, name: str
) -> None:
    with pytest.raises(ValueError):
        snapshot_manager.read_snapshot(name)


async def test_read_snapshot_rejects_an_unknown_schema(
    snapshot_manager: bm.BackupManager,
) -> None:
    snapshot_manager.backup_dir.mkdir(parents=True)
    bad = snapshot_manager.backup_dir / "label.x.20260521_120000.yaml"
    bad.write_text(yaml.safe_dump({"schema_version": 999, "config": {}}))

    with pytest.raises(ValueError, match="schema_version"):
        snapshot_manager.read_snapshot(bad.name)


async def test_delete_snapshot_removes_the_file(
    snapshot_manager: bm.BackupManager, label_registry: lr.LabelRegistry
) -> None:
    path = await snapshot_of(snapshot_manager, add_label(label_registry, "Kitchen"))

    snapshot_manager.delete_snapshot(path.name)

    assert not path.exists()


async def test_bulk_delete_by_age_keeps_recent_snapshots(
    snapshot_manager: bm.BackupManager, label_registry: lr.LabelRegistry
) -> None:
    kitchen = add_label(label_registry, "Kitchen")
    old = await snapshot_of(snapshot_manager, kitchen)
    forty_days_ago = time.time() - 40 * 86400
    os.utime(old, (forty_days_ago, forty_days_ago))
    recent = await snapshot_of(snapshot_manager, kitchen)

    bulk = snapshot_manager.delete_bulk(older_than_days=30)

    assert bulk["deleted"] == [old.name]
    assert recent.exists()
    assert bulk["failed"] == []


async def test_a_bulk_delete_with_no_filter_is_refused_and_deletes_nothing(
    snapshot_manager: bm.BackupManager,
    label_registry: lr.LabelRegistry,
    core_client: HomeAssistantClient,
) -> None:
    """The manager deletes whatever its filters select, which is everything
    with none, so the tool refuses a call that names no snapshot and no
    filter."""
    path = await snapshot_of(snapshot_manager, add_label(label_registry, "Kitchen"))

    with pytest.raises(ToolError, match="at least one filter"):
        await manage_backup(core_client, scope="edits", action="delete")

    assert path.exists()
    deleted = await manage_backup(
        core_client, scope="edits", action="delete", domain="label"
    )
    assert deleted["data"]["deleted"] == [path.name]


async def test_restore_puts_the_label_back_and_reports_its_safety_backup(
    snapshot_manager: bm.BackupManager, label_registry: lr.LabelRegistry
) -> None:
    kitchen = add_label(label_registry, "Kitchen")
    path = await snapshot_of(snapshot_manager, kitchen)
    label_registry.async_update(kitchen, name="Renamed")

    result = await snapshot_manager.restore_snapshot(path.name)

    restored = label_registry.async_get_label(kitchen)
    assert restored is not None and restored.name == "Kitchen"
    assert result["domain"] == "label"
    assert result["entity_id"] == kitchen
    assert result["restored_from"] == path.name
    # The state it replaced was captured first, so the restore can be undone.
    assert (
        captured_name(
            snapshot_manager, snapshot_manager.backup_dir / result["safety_backup"]
        )
        == "Renamed"
    )


async def test_restore_of_an_unknown_domain_writes_nothing(
    snapshot_manager: bm.BackupManager,
) -> None:
    bogus = place_snapshot(
        snapshot_manager.backup_dir,
        "alien.x.20260521_120000.yaml",
        "x",
        domain="alien",
        held={"v": 1},
        text=False,
    )

    with pytest.raises(bm.BackupRestoreError) as caught:
        await snapshot_manager.restore_snapshot(bogus.name)

    assert caught.value.outcome["reason"] == "unsupported_domain"
    assert caught.value.outcome["apply_status"] == "not_applied"


async def test_restore_takes_no_safety_backup_while_auto_backup_is_off(
    snapshot_manager: bm.BackupManager,
    label_registry: lr.LabelRegistry,
    backup_settings: config.Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kitchen = add_label(label_registry, "Kitchen")
    path = await snapshot_of(snapshot_manager, kitchen)
    label_registry.async_update(kitchen, name="Renamed")
    monkeypatch.setattr(backup_settings, "enable_auto_backup", False)

    result = await snapshot_manager.restore_snapshot(path.name)

    assert result["safety_backup"] is None
    restored = label_registry.async_get_label(kitchen)
    assert restored is not None and restored.name == "Kitchen"


@pytest.fixture
def standalone_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A non-add-on process whose data dir is ``tmp_path / "data"``.

    ``get_data_dir`` is memoized, so the cache is cleared on both sides: before,
    so an earlier test's directory is not returned here, and after, so this
    test's tmpdir does not leak into the next one.
    """
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    # Path.home() ignores HOME on Windows; never inspect the operator's backups.
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(tmp_path / "data"))
    get_data_dir.cache_clear()
    yield tmp_path / "data"
    get_data_dir.cache_clear()


@pytest.fixture
def default_dir_manager(
    standalone_env: Path,
    core_client: HomeAssistantClient,
    backup_settings: config.Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> bm.BackupManager:
    """A manager whose settings name no backup directory, so it picks a default."""
    monkeypatch.setattr(backup_settings, "auto_backup_dir", "")
    return bm.BackupManager(backup_settings, core_client)


def _install_with_data_dir(monkeypatch: pytest.MonkeyPatch, present: bool) -> None:
    """Whether ``/data`` exists, as it does only in the add-on's container."""
    native_is_dir = Path.is_dir

    def is_dir(path: Path, **kwargs: Any) -> bool:
        return present if str(path) == "/data" else native_is_dir(path, **kwargs)

    monkeypatch.setattr(Path, "is_dir", is_dir)


class TestDefaultDir:
    """#2372: the default must live in the directory the data volume persists."""

    async def test_a_new_install_defaults_under_the_data_dir(
        self, default_dir_manager: bm.BackupManager, standalone_env: Path
    ) -> None:
        assert default_dir_manager.backup_dir == standalone_env / "backups"

    async def test_resolving_the_default_creates_no_storage(
        self, default_dir_manager: bm.BackupManager, standalone_env: Path
    ) -> None:
        assert not default_dir_manager.backup_dir.exists()
        assert default_dir_manager.init_dir_error is None
        assert default_dir_manager.enabled

    async def test_existing_home_snapshots_keep_their_dir(
        self,
        standalone_env: Path,
        tmp_path: Path,
        default_dir_manager: bm.BackupManager,
    ) -> None:
        legacy = tmp_path / "home" / ".local" / "share" / "ha_mcp" / "backups"
        legacy.mkdir(parents=True)
        (legacy / "automation.kitchen.20260101_000000.yaml").write_text("x")

        assert default_dir_manager.backup_dir == legacy.resolve()

    async def test_existing_xdg_snapshots_keep_their_dir(
        self,
        standalone_env: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        core_client: HomeAssistantClient,
        backup_settings: config.Settings,
    ) -> None:
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
        legacy = tmp_path / "xdg" / "ha_mcp" / "backups"
        legacy.mkdir(parents=True)
        (legacy / "script.morning.20260101_000000.yaml").write_text("x")
        monkeypatch.setattr(backup_settings, "auto_backup_dir", "")

        assert bm.BackupManager(backup_settings, core_client).backup_dir == legacy

    async def test_an_empty_legacy_dir_moves_to_the_data_dir(
        self,
        standalone_env: Path,
        tmp_path: Path,
        default_dir_manager: bm.BackupManager,
    ) -> None:
        """An empty directory has nothing to list or restore from, so keeping it
        would only preserve the ephemeral location the issue is about."""
        (tmp_path / "home" / ".local" / "share" / "ha_mcp" / "backups").mkdir(
            parents=True
        )

        assert default_dir_manager.backup_dir == standalone_env / "backups"

    async def test_an_unreadable_legacy_dir_counts_as_empty(
        self,
        standalone_env: Path,
        monkeypatch: pytest.MonkeyPatch,
        core_client: HomeAssistantClient,
        backup_settings: config.Settings,
    ) -> None:
        def unreadable(path: Path) -> Any:
            raise PermissionError("denied")

        monkeypatch.setattr(Path, "iterdir", unreadable)
        monkeypatch.setattr(backup_settings, "auto_backup_dir", "")

        manager = bm.BackupManager(backup_settings, core_client)

        assert manager.backup_dir == standalone_env / "backups"

    async def test_the_addon_directory_is_unchanged_by_the_data_dir_default(
        self,
        standalone_env: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        core_client: HomeAssistantClient,
        backup_settings: config.Settings,
    ) -> None:
        """The add-on path is named in the add-on option descriptions; the
        data-dir default must not displace it even when snapshots exist under
        the legacy path."""
        legacy = tmp_path / "home" / ".local" / "share" / "ha_mcp" / "backups"
        legacy.mkdir(parents=True)
        (legacy / "script.morning.20260101_000000.yaml").write_text("x")
        _install_with_data_dir(monkeypatch, present=True)
        monkeypatch.setenv("SUPERVISOR_TOKEN", "token")
        monkeypatch.setattr(backup_settings, "auto_backup_dir", "")

        manager = bm.BackupManager(backup_settings, core_client)

        assert manager.backup_dir == Path("/data/ha_mcp_backups")

    async def test_the_supervisor_token_alone_does_not_make_it_the_addon(
        self,
        standalone_env: Path,
        monkeypatch: pytest.MonkeyPatch,
        core_client: HomeAssistantClient,
        backup_settings: config.Settings,
    ) -> None:
        """The HA core container carries the token too. Without /data the
        data-dir default applies."""
        _install_with_data_dir(monkeypatch, present=False)
        monkeypatch.setenv("SUPERVISOR_TOKEN", "token")
        monkeypatch.setattr(backup_settings, "auto_backup_dir", "")

        manager = bm.BackupManager(backup_settings, core_client)

        assert manager.backup_dir == standalone_env / "backups"
