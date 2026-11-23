"""Enforce mode keeps a file that defines a hidden item out of the file backups.

The hidden-definition check asks about the file as Home Assistant loads it now,
so a backup taken of such a file would hand its old content to anyone who later
reads the backup, once the file itself no longer defines the item. The file
tools therefore refuse before the pre-write snapshot is taken, and
``ha_manage_backup(scope="edits")`` refuses to read, compare, restore or take a
backup of a file while it defines a hidden item.

The files sit in Core's config directory and are read and written by the File &
YAML Tools entry's own services; snapshots are real files of the server's
backup manager.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp import backup_manager as bm
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.config import Settings
from ha_mcp.tools.tools_filesystem import FilesystemTools
from ha_mcp.visibility.model import VisibilityConfig

from .._backup_core import backup_settings as backup_settings
from .._backup_core import manage_backup
from .._core_setup import set_up_tools_entry
from .._visibility_core import enforce_hiding, error_code

HIDDEN = "script.secret_door"
DEFINING = "www/a.yaml"
SCRIPTS = "secret_door:\n  sequence: []\n# 30000\n"


async def _write(hass: HomeAssistant, path: str, text: str) -> None:
    target = Path(hass.config.path(path))

    def write() -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    await hass.async_add_executor_job(write)


async def _read(hass: HomeAssistant, path: str) -> str | None:
    target = Path(hass.config.path(path))
    exists = await hass.async_add_executor_job(target.exists)
    if not exists:
        return None
    return await hass.async_add_executor_job(target.read_text, "utf-8")


@pytest.fixture
async def manager(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    backup_settings: Settings,
) -> bm.BackupManager:
    """The backup manager on a ``hass`` whose configuration loads its scripts
    from ``www/a.yaml``, with the File & YAML Tools entry set up."""
    assert await async_setup_component(hass, "config", {})
    await set_up_tools_entry(hass)
    await _write(hass, "configuration.yaml", f"script: !include {DEFINING}\n")
    await _write(hass, DEFINING, SCRIPTS)
    return bm.get_backup_manager(component_core_client, backup_settings)


def _file_snapshots(manager: bm.BackupManager) -> list[dict[str, Any]]:
    return [s for s in manager.list_snapshots() if s["domain"] in ("file", "yaml")]


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param({"edits": [{"old": "30000", "new": "1"}]}, id="edits"),
        pytest.param({"content": "x: 1\n", "overwrite": True}, id="overwrite"),
        pytest.param(None, id="delete"),
    ],
)
async def test_a_refused_change_leaves_no_backup_of_the_hidden_definition(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    manager: bm.BackupManager,
    set_config: Callable[..., VisibilityConfig],
    arguments: dict[str, Any] | None,
) -> None:
    """A snapshot taken before the refusal would show the hidden definition
    through ha_manage_backup once the file no longer holds it."""
    enforce_hiding(set_config, HIDDEN)
    tools = FilesystemTools(component_core_client)

    with pytest.raises(ToolError) as excinfo:
        if arguments is None:
            await tools.ha_delete_file(path=DEFINING, confirm=True)
        else:
            await tools.ha_write_file(path=DEFINING, **arguments)

    assert error_code(excinfo) == "ENTITY_VISIBILITY_ENFORCED"
    assert await _read(hass, DEFINING) == SCRIPTS
    assert _file_snapshots(manager) == []


@pytest.mark.parametrize("action", ["view", "diff", "restore"])
@pytest.mark.parametrize(
    ("domain", "target"),
    [("file", DEFINING), ("yaml", f"{DEFINING}::secret_door")],
)
async def test_a_backup_of_a_file_defining_a_hidden_item_is_not_read_out(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    manager: bm.BackupManager,
    set_config: Callable[..., VisibilityConfig],
    action: str,
    domain: str,
    target: str,
) -> None:
    """A backup taken before the item was hidden holds the same definition the
    file does, and a restore would write over it."""
    snapshot = await manager.maybe_snapshot(domain, target, force=True)
    assert snapshot is not None
    enforce_hiding(set_config, HIDDEN)

    with pytest.raises(ToolError) as excinfo:
        await manage_backup(
            component_core_client,
            scope="edits",
            action=action,
            backup_name=snapshot.name,
        )

    assert error_code(excinfo) == "ENTITY_VISIBILITY_ENFORCED"
    assert await _read(hass, DEFINING) == SCRIPTS
    assert [s["name"] for s in _file_snapshots(manager)] == [snapshot.name]


async def test_no_backup_is_taken_of_a_file_defining_a_hidden_item(
    component_core_client: HomeAssistantClient,
    manager: bm.BackupManager,
    set_config: Callable[..., VisibilityConfig],
) -> None:
    """An on-demand backup would hold the hidden definition for a later read."""
    enforce_hiding(set_config, HIDDEN)

    with pytest.raises(ToolError) as excinfo:
        await manage_backup(
            component_core_client,
            scope="edits",
            action="create",
            domain="file",
            entity_id=DEFINING,
        )

    assert error_code(excinfo) == "ENTITY_VISIBILITY_ENFORCED"
    assert _file_snapshots(manager) == []


@pytest.mark.parametrize(
    "bak",
    [
        pytest.param("configuration.yaml.20260101_120000.bak", id="defining-file"),
        pytest.param("packages_my_pkg.yaml.20260101_120000.bak", id="ambiguous-path"),
    ],
)
async def test_a_legacy_backup_is_not_read_out_unless_its_file_is_known_clear(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    manager: bm.BackupManager,
    set_config: Callable[..., VisibilityConfig],
    bak: str,
) -> None:
    """A legacy backup names its file only through its own name; one whose name
    fits more than one file cannot be checked and is refused."""
    await _write(
        hass, "configuration.yaml", "script:\n  secret_door:\n    sequence: []\n"
    )
    await _write(hass, f".ha_mcp_tools_backups/{bak}", "a: 1\n")
    enforce_hiding(set_config, HIDDEN)

    with pytest.raises(ToolError) as excinfo:
        await manage_backup(
            component_core_client,
            scope="edits",
            action="view",
            backup_name=f"legacy:{bak}",
        )

    assert error_code(excinfo) == "ENTITY_VISIBILITY_ENFORCED"


@pytest.mark.parametrize("delete", [False, True], ids=["write", "delete"])
async def test_without_the_tools_entry_a_change_reports_the_missing_entry(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    backup_settings: Settings,
    set_config: Callable[..., VisibilityConfig],
    delete: bool,
) -> None:
    """The entry is what the user can fix, so it is named before the check of
    a file the tools cannot touch anyway."""
    assert await async_setup_component(hass, "config", {})
    await _write(hass, "configuration.yaml", f"script: !include {DEFINING}\n")
    await _write(hass, DEFINING, SCRIPTS)
    enforce_hiding(set_config, HIDDEN)
    tools = FilesystemTools(component_core_client)

    with pytest.raises(ToolError) as excinfo:
        if delete:
            await tools.ha_delete_file(path=DEFINING, confirm=True)
        else:
            await tools.ha_write_file(path=DEFINING, content="x: 1\n", overwrite=True)

    assert error_code(excinfo) == "COMPONENT_NOT_INSTALLED"
