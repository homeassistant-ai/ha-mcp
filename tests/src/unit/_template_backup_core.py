"""A real Template helper and the backup manager that captures it, for the
Template backup tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from ha_mcp import backup_manager as bm
from ha_mcp import config
from ha_mcp.client.rest_client import HomeAssistantClient

from ._template_helper_core import create_template_sensor


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> config.Settings:
    """The server's real settings, with auto-backup on and its own directory."""
    configured = config.get_global_settings()
    for name, value in {
        "enable_auto_backup": True,
        "enable_snapshot_actions": True,
        "backup_read_only": False,
        "read_only_mode": False,
        "enable_snapshot_delete": False,
        "auto_backup_throttle_minutes": 30,
        "auto_backup_retain_per_entity": 5,
        "auto_backup_dir": str(tmp_path / "backups"),
    }.items():
        monkeypatch.setattr(configured, name, value)
    return configured


@pytest.fixture
async def template_entry(hass: HomeAssistant) -> ConfigEntry:
    return await create_template_sensor(hass, "saved")


@pytest.fixture
def manager(
    component_core_client: HomeAssistantClient, settings: config.Settings
) -> bm.BackupManager:
    return bm.get_backup_manager(component_core_client, settings)


def degrade_secret_scrub(hass: HomeAssistant) -> None:
    """A secrets.yaml that is present but cannot be parsed."""
    Path(hass.config.path("secrets.yaml")).write_text("key: [unclosed\n")


async def real_snapshot(manager: bm.BackupManager, entry: ConfigEntry) -> Path:
    path = await manager.maybe_snapshot("helper_template", entry.entry_id, force=True)
    assert path is not None
    return path


def legacy_snapshot(
    manager: bm.BackupManager,
    path: Path,
    entity_id: str | None,
    *,
    entry_id: str | None = None,
    emptied: bool = False,
) -> Path:
    """Rewrite a real snapshot's identity the way an older release stored it."""
    data = manager.read_snapshot(path.name)
    data["entity_id"] = entity_id
    if entry_id is not None:
        data["config"]["entry_id"] = entry_id
    if emptied:
        data["config"] = {}
    path.write_text(yaml.safe_dump(data))
    return path
