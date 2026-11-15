"""The backup manager on a real ``hass``, for the backup manager tests.

``backup_settings`` is the server's own settings with auto-backup on and its
directory under ``tmp_path``. ``snapshot_manager`` is the manager of
``core_client`` on a ``hass`` with Core's ``config`` commands, every default
handler registered; ``file_snapshot_manager`` is the same on a ``hass`` that
also has the File & YAML Tools entry set up, with Core's config directory a
temporary one of the test's own. A test changes what a snapshot holds through
Core (a registry fixture, a config entry, a file in Core's config directory)
and the manager reads it back over the client.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, Self

import pytest
import yaml
from homeassistant.core import HomeAssistant
from homeassistant.helpers import label_registry as lr
from homeassistant.setup import async_setup_component

from ha_mcp import backup_manager as bm
from ha_mcp import config
from ha_mcp._vendor.fastmcp import FastMCP
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools import backup

from ._core_setup import set_up_tools_entry

HEADER = "# ha_mcp_backup\n"
_STEM = re.compile(r"^[a-z_]+\.(?P<stem>.+)\.\d{8}_\d{6}(?:_\d{2})?\.yaml$")


@pytest.fixture
def backup_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> config.Settings:
    """The server's real settings: auto-backup on, no throttle, five kept per
    entity, snapshots under ``tmp_path / "backups"``."""
    configured = config.get_global_settings()
    for name, value in {
        "enable_snapshot_actions": True,
        "backup_read_only": False,
        "read_only_mode": False,
        "enable_auto_backup": True,
        "auto_backup_throttle_minutes": 0,
        "auto_backup_retain_per_entity": 5,
        "auto_backup_dir": str(tmp_path / "backups"),
    }.items():
        monkeypatch.setattr(configured, name, value)
    return configured


@pytest.fixture
async def snapshot_manager(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    backup_settings: config.Settings,
) -> bm.BackupManager:
    """The backup manager of ``core_client``: Core answers its registry reads."""
    assert await async_setup_component(hass, "config", {})
    return bm.get_backup_manager(core_client, backup_settings)


@pytest.fixture
async def file_snapshot_manager(
    hass: HomeAssistant,
    hass_config_dir: str,
    snapshot_manager: bm.BackupManager,
) -> bm.BackupManager:
    """``snapshot_manager`` on a ``hass`` with the File & YAML Tools entry set up
    (``hass_config_dir`` is its config directory, a temporary one)."""
    await set_up_tools_entry(hass)
    return snapshot_manager


class SnapshotClock:
    """The time the manager stamps on snapshots: file names and ``captured``.

    Only the manager's own reading of the date is set; the event loop's clock
    keeps running, so Core's WebSocket and its timers are unaffected.
    """

    def __init__(self) -> None:
        self.moment = datetime(2026, 9, 3, 12, tzinfo=UTC)

    def set(self, moment: str) -> None:
        """Jump to ``moment`` (an ISO date and time, UTC)."""
        self.moment = datetime.fromisoformat(moment).replace(tzinfo=UTC)

    def tick(self, seconds: int = 1) -> None:
        self.moment += timedelta(seconds=seconds)


@pytest.fixture
def snapshot_clock(monkeypatch: pytest.MonkeyPatch) -> SnapshotClock:
    """A clock the test sets for the manager's snapshot stamps."""
    clock = SnapshotClock()

    class _Moment(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromisoformat(clock.moment.isoformat())

    monkeypatch.setattr(bm, "datetime", _Moment)
    return clock


async def manage_backup(client: HomeAssistantClient, **arguments: Any) -> Any:
    """Call ``ha_manage_backup`` registered on a server for ``client``."""
    mcp = FastMCP("backup-manager-tests")
    backup.register_backup_tools(mcp, client)
    tool = await mcp.get_tool("ha_manage_backup")
    return await tool.fn(**arguments)


def add_label(registry: lr.LabelRegistry, name: str) -> str:
    """A label in Core's registry; its id, which is also its snapshot target."""
    return registry.async_create(name).label_id


async def snapshot_of(manager: bm.BackupManager, target: str, **options: Any) -> Path:
    """A snapshot of the label ``target``, which has to exist for one to be written."""
    path = await manager.maybe_snapshot("label", target, **options)
    assert path is not None, f"nothing was captured for {target}"
    return path


def captured_name(manager: bm.BackupManager, path: Path) -> str:
    """The label name a snapshot holds."""
    return str(manager.read_snapshot(path.name)["config"]["name"])


def stem_of(path: Path) -> str:
    """The id part of a snapshot's file name (``<domain>.<stem>.<timestamp>.yaml``)."""
    found = _STEM.match(path.name)
    assert found is not None, path.name
    return found.group("stem")


def place_snapshot(
    directory: Path,
    name: str,
    entity_id: str,
    *,
    domain: str = "file",
    held: Any = "body\n",
    text: bool = True,
) -> Path:
    """Write a snapshot file under an explicit name, as an older release (or
    another process) left it, and return it. ``text`` marks file content, as
    the file domains' snapshots are."""
    payload = {
        "schema_version": bm.SCHEMA_VERSION,
        "domain": domain,
        "entity_id": entity_id,
        "captured": "2026-09-03T00:00:00+00:00",
        "tool": "t",
        "config": held,
        **({"kind": "text"} if text else {}),
    }
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    target.write_text(HEADER + yaml.safe_dump(payload, sort_keys=False))
    return target


def break_backup_dir(
    settings: config.Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Path:
    """Point the backup directory under a regular file, so creating it fails the
    way a read-only or full disk does; unlinking the returned file repairs it."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setattr(settings, "auto_backup_dir", str(blocker / "backups"))
    return blocker
