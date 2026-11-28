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

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp import backup_manager as bm
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.config import Settings
from ha_mcp.tools import auto_backup
from ha_mcp.tools.tools_filesystem import FilesystemTools
from ha_mcp.visibility.model import VisibilityConfig

from .._backup_core import backup_settings as backup_settings
from .._backup_core import manage_backup
from .._config_dir_yaml import write_config_files
from .._core_setup import set_up_tools_entry
from .._tool_errors import error_body
from .._visibility_core import enforce_hiding, error_code

HIDDEN = "script.secret_door"
DEFINING = "www/a.yaml"
CLEAR = "www/b.yaml"
SCRIPTS = "secret_door:\n  sequence: []\n# 30000\n"
DEFINING_CONFIG = {
    "configuration.yaml": f"script: !include {DEFINING}\n",
    DEFINING: SCRIPTS,
}


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
    await write_config_files(hass, DEFINING_CONFIG)
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


async def test_a_write_waiting_for_another_one_checks_the_file_it_left(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    manager: bm.BackupManager,
    set_config: Callable[..., VisibilityConfig],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check runs under the file lock: checked before the wait, the write
    would snapshot and replace a hidden definition the other write added."""
    await write_config_files(hass, {DEFINING: "# 30000\n"})
    enforce_hiding(set_config, HIDDEN)
    real_lock = auto_backup.registry_update_lock

    @asynccontextmanager
    async def another_write_first(
        registry: str, resource_id: str
    ) -> AsyncIterator[None]:
        await write_config_files(hass, {DEFINING: SCRIPTS})
        async with real_lock(registry, resource_id):
            yield

    monkeypatch.setattr(auto_backup, "registry_update_lock", another_write_first)

    with pytest.raises(ToolError) as excinfo:
        await FilesystemTools(component_core_client).ha_write_file(
            path=DEFINING, content="x: 1\n", overwrite=True
        )
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
    later = SCRIPTS + "# later\n"
    await write_config_files(hass, {DEFINING: later})
    enforce_hiding(set_config, HIDDEN)

    with pytest.raises(ToolError) as excinfo:
        await manage_backup(
            component_core_client,
            scope="edits",
            action=action,
            backup_name=snapshot.name,
        )

    assert error_code(excinfo) == "ENTITY_VISIBILITY_ENFORCED"
    assert await _read(hass, DEFINING) == later
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
        pytest.param("packages_.20260101_120000.bak", id="folder-path"),
    ],
)
async def test_a_legacy_backup_of_a_hidden_or_unknown_file_is_not_read_out(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    manager: bm.BackupManager,
    set_config: Callable[..., VisibilityConfig],
    bak: str,
) -> None:
    """A legacy backup names its file only through its own name; one whose name
    fits more than one file cannot be checked and is refused."""
    await write_config_files(
        hass,
        {
            "configuration.yaml": "script:\n  secret_door:\n    sequence: []\n",
            f".ha_mcp_tools_backups/{bak}": "a: 1\n",
        },
    )
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
    await write_config_files(hass, DEFINING_CONFIG)
    enforce_hiding(set_config, HIDDEN)
    tools = FilesystemTools(component_core_client)

    with pytest.raises(ToolError) as excinfo:
        if delete:
            await tools.ha_delete_file(path=DEFINING, confirm=True)
        else:
            await tools.ha_write_file(path=DEFINING, content="x: 1\n", overwrite=True)

    assert error_code(excinfo) == "COMPONENT_NOT_INSTALLED"


async def test_a_file_defining_nothing_hidden_is_still_written_with_a_backup(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    manager: bm.BackupManager,
    set_config: Callable[..., VisibilityConfig],
) -> None:
    """The refusals above are about the file, not about enforce mode."""
    await write_config_files(hass, {CLEAR: "x: 1\n"})
    enforce_hiding(set_config, HIDDEN)

    await FilesystemTools(component_core_client).ha_write_file(
        path=CLEAR, content="x: 2\n", overwrite=True
    )

    assert await _read(hass, CLEAR) == "x: 2\n"
    assert [s["entity_id"] for s in _file_snapshots(manager)] == [CLEAR]


@pytest.mark.parametrize(
    ("domain", "target"),
    [("file", CLEAR), ("yaml", "configuration.yaml::script"), ("legacy", "")],
)
async def test_a_backup_of_a_file_defining_nothing_hidden_is_shown(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    manager: bm.BackupManager,
    set_config: Callable[..., VisibilityConfig],
    domain: str,
    target: str,
) -> None:
    """A backup whose file is known and defines nothing hidden stays readable."""
    bak = "packages_clear.yaml.20260101_120000.bak"
    await write_config_files(
        hass, {CLEAR: "x: 1\n", f".ha_mcp_tools_backups/{bak}": "a: 1\n"}
    )
    if domain == "legacy":
        name = f"legacy:{bak}"
    else:
        snapshot = await manager.maybe_snapshot(domain, target, force=True)
        assert snapshot is not None
        name = snapshot.name
    enforce_hiding(set_config, HIDDEN)

    result = await manage_backup(
        component_core_client, scope="edits", action="view", backup_name=name
    )

    assert result["success"] is True


async def test_a_backup_is_taken_of_a_file_defining_nothing_hidden(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    manager: bm.BackupManager,
    set_config: Callable[..., VisibilityConfig],
) -> None:
    await write_config_files(hass, {CLEAR: "x: 1\n"})
    enforce_hiding(set_config, HIDDEN)

    await manage_backup(
        component_core_client,
        scope="edits",
        action="create",
        domain="file",
        entity_id=CLEAR,
    )

    assert [s["entity_id"] for s in _file_snapshots(manager)] == [CLEAR]


async def test_a_legacy_backup_whose_read_failed_is_not_shown_on_the_retry(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    manager: bm.BackupManager,
    set_config: Callable[..., VisibilityConfig],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check's read of the backup gets no answer. Left to the action like a
    backup that does not exist, the backup would be read again there and, once
    the component answers, its hidden definition handed out unchecked."""
    bak = "configuration.yaml.20260101_120000.bak"
    await write_config_files(
        hass,
        {
            "configuration.yaml": "script:\n  secret_door:\n    sequence: []\n",
            f".ha_mcp_tools_backups/{bak}": "script:\n  secret_door: {}\n",
        },
    )
    enforce_hiding(set_config, HIDDEN)
    # Listing fetches the caller token, so the dropped call below is the read.
    await manage_backup(component_core_client, scope="edits", action="list")
    monkeypatch.setattr(
        component_core_client,
        "call_service",
        AsyncMock(side_effect=HomeAssistantConnectionError("dropped")),
    )

    with pytest.raises(ToolError) as excinfo:
        await manage_backup(
            component_core_client,
            scope="edits",
            action="view",
            backup_name=f"legacy:{bak}",
        )

    dropped = component_core_client.call_service
    assert isinstance(dropped, AsyncMock)
    assert dropped.await_count == 1
    assert error_code(excinfo) == "ENTITY_VISIBILITY_ENFORCED"


async def test_an_invalid_snapshot_is_reported_as_invalid_while_enforcing(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    manager: bm.BackupManager,
    set_config: Callable[..., VisibilityConfig],
) -> None:
    """The action reads a snapshot the same way, so its own error says what is
    wrong with it."""
    await write_config_files(hass, {CLEAR: "x: 1\n"})
    snapshot = await manager.maybe_snapshot("file", CLEAR, force=True)
    assert snapshot is not None
    await hass.async_add_executor_job(snapshot.write_text, "- not a snapshot\n")
    enforce_hiding(set_config, HIDDEN)

    with pytest.raises(ToolError) as excinfo:
        await manage_backup(
            component_core_client,
            scope="edits",
            action="view",
            backup_name=snapshot.name,
        )

    assert error_code(excinfo) == "VALIDATION_INVALID_PARAMETER"


async def test_a_backup_target_naming_no_file_is_refused_while_enforcing(
    component_core_client: HomeAssistantClient,
    manager: bm.BackupManager,
    set_config: Callable[..., VisibilityConfig],
) -> None:
    """A ``yaml`` target without ``::`` names no file the check could judge."""
    enforce_hiding(set_config, HIDDEN)

    with pytest.raises(ToolError) as excinfo:
        await manage_backup(
            component_core_client,
            scope="edits",
            action="create",
            domain="yaml",
            entity_id=DEFINING,
        )

    assert error_code(excinfo) == "ENTITY_VISIBILITY_ENFORCED"
    assert _file_snapshots(manager) == []


BROKEN = "www/broken.yaml"
UNCLOSED = "a: [unclosed\n"


async def _break_the_configuration(hass: HomeAssistant, tools: FilesystemTools) -> None:
    """Write a YAML error into a file the configuration includes, as an agent
    would: the write is checked while the configuration still loads."""
    await write_config_files(
        hass,
        {
            "configuration.yaml": (
                f"script: !include {DEFINING}\nsensor: !include {BROKEN}\n"
            ),
            BROKEN: "[]\n",
        },
    )
    await tools.ha_write_file(path=BROKEN, content=UNCLOSED, overwrite=True)


def _names_the_way_out(exc: pytest.ExceptionInfo[ToolError]) -> bool:
    suggestions = " ".join(error_body(exc)["error"]["suggestions"])
    return "ha_write_file" in suggestions and "Enforce mode off" in suggestions


async def test_a_configuration_that_does_not_load_can_be_repaired(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    manager: bm.BackupManager,
    set_config: Callable[..., VisibilityConfig],
) -> None:
    """A YAML error anywhere leaves every file unchecked; the whole-file write
    that fixes it must still run, or nothing an agent can call gets out."""
    enforce_hiding(set_config, HIDDEN)
    tools = FilesystemTools(component_core_client)
    await _break_the_configuration(hass, tools)

    with pytest.raises(ToolError) as excinfo:
        await tools.ha_read_file(path=BROKEN)
    assert error_code(excinfo) == "CONFIG_INVALID"
    assert _names_the_way_out(excinfo)

    await tools.ha_write_file(path=BROKEN, content="[]\n", overwrite=True)

    assert await _read(hass, BROKEN) == "[]\n"
    assert UNCLOSED in [
        manager.read_snapshot(s["name"])["config"]
        for s in _file_snapshots(manager)
        if s["entity_id"] == BROKEN
    ]
    with pytest.raises(ToolError) as excinfo:
        await tools.ha_read_file(path=DEFINING)
    assert error_code(excinfo) == "ENTITY_VISIBILITY_ENFORCED"


@pytest.mark.parametrize("call", ["delete", "restore"])
async def test_a_delete_or_restore_runs_while_the_configuration_does_not_load(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    manager: bm.BackupManager,
    set_config: Callable[..., VisibilityConfig],
    call: str,
) -> None:
    tools = FilesystemTools(component_core_client)
    await write_config_files(hass, {BROKEN: "[]\n"})
    good = await manager.maybe_snapshot("file", BROKEN, force=True)
    assert good is not None
    enforce_hiding(set_config, HIDDEN)
    await _break_the_configuration(hass, tools)

    if call == "delete":
        await tools.ha_delete_file(path=BROKEN, confirm=True)
        assert await _read(hass, BROKEN) is None
    else:
        result = await manage_backup(
            component_core_client,
            scope="edits",
            action="restore",
            backup_name=good.name,
        )
        assert result["success"] is True
        assert await _read(hass, BROKEN) == "[]\n"


@pytest.mark.parametrize("call", ["edits", "create", "view", "diff"])
async def test_what_the_check_must_judge_is_refused_with_the_way_out(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    manager: bm.BackupManager,
    set_config: Callable[..., VisibilityConfig],
    call: str,
) -> None:
    """These show or keep a file's content, which a failed load cannot clear."""
    snapshot = await manager.maybe_snapshot("file", DEFINING, force=True)
    assert snapshot is not None
    enforce_hiding(set_config, HIDDEN)
    tools = FilesystemTools(component_core_client)
    await _break_the_configuration(hass, tools)

    with pytest.raises(ToolError) as excinfo:
        if call == "edits":
            await tools.ha_write_file(
                path=DEFINING, edits=[{"old": "30000", "new": "1"}]
            )
        elif call == "create":
            await manage_backup(
                component_core_client,
                scope="edits",
                action="create",
                domain="file",
                entity_id=DEFINING,
            )
        else:
            await manage_backup(
                component_core_client,
                scope="edits",
                action=call,
                backup_name=snapshot.name,
            )

    assert error_code(excinfo) == "CONFIG_INVALID"
    assert _names_the_way_out(excinfo)
    assert await _read(hass, DEFINING) == SCRIPTS
