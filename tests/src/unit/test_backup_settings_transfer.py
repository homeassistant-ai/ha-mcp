"""``ha_manage_backup(scope="settings")``: off until a person allows it, then the
settings page's export and import, held to the same limits as the dev tools.
The server is real and talks to a real ``hass``; its settings files are in
``tmp_path``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ha_mcp._vendor.fastmcp import FastMCP
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.config import reset_global_settings
from ha_mcp.read_only import read_only_request
from ha_mcp.tools import backup
from ha_mcp.tools.tools_dev import DevTools

from ._real_server import build_server
from ._settings_ui_app import settings_world as settings_world


def _write(directory: Path, name: str, document: dict[str, Any]) -> None:
    (directory / name).write_text(json.dumps(document), encoding="utf-8")


def _read(directory: Path, name: str) -> Any:
    return json.loads((directory / name).read_text(encoding="utf-8"))


def _allow(directory: Path) -> None:
    # What the Backups tab saves when a person turns the toggle on.
    _write(
        directory, "backup_settings.json", {"enable_settings_transfer_actions": True}
    )
    reset_global_settings()


async def _manage_backup(
    monkeypatch: pytest.MonkeyPatch, client: HomeAssistantClient, **arguments: Any
) -> Any:
    mcp = FastMCP("backup-settings-tests")
    backup.register_backup_tools(mcp, client, server=build_server(monkeypatch, client))
    tool = await mcp.get_tool("ha_manage_backup")
    return await tool.fn(**arguments)


@pytest.mark.parametrize("action", ["export", "import"])
async def test_settings_stay_closed_until_a_person_allows_them(
    settings_world: Path,
    monkeypatch: pytest.MonkeyPatch,
    core_client: HomeAssistantClient,
    action: str,
) -> None:
    _write(settings_world, "feature_flags.json", {"redact_secrets": True})
    reset_global_settings()

    with pytest.raises(ToolError, match="enable_settings_transfer_actions") as err:
        await _manage_backup(
            monkeypatch,
            core_client,
            scope="settings",
            action=action,
            settings_file={"format": "ha-mcp-settings"},
        )

    assert "Backups tab" in str(err.value)
    assert _read(settings_world, "feature_flags.json") == {"redact_secrets": True}


async def test_an_allowed_export_imports_back_as_the_settings_page_does(
    settings_world: Path,
    monkeypatch: pytest.MonkeyPatch,
    core_client: HomeAssistantClient,
) -> None:
    _allow(settings_world)
    _write(settings_world, "feature_flags.json", {"redact_secrets": True})
    exported = await _manage_backup(
        monkeypatch, core_client, scope="settings", action="export"
    )
    settings_file = exported["settings_file"]
    assert settings_file["contains_credentials"] is False
    _write(settings_world, "feature_flags.json", {"redact_secrets": False})

    result = await _manage_backup(
        monkeypatch,
        core_client,
        scope="settings",
        action="import",
        settings_file=settings_file,
    )

    assert result["success"] is True
    assert _read(settings_world, "feature_flags.json") == {"redact_secrets": True}


async def test_an_import_keeps_the_human_backup_controls_this_server_has(
    settings_world: Path,
    monkeypatch: pytest.MonkeyPatch,
    core_client: HomeAssistantClient,
) -> None:
    _allow(settings_world)
    exported = await _manage_backup(
        monkeypatch, core_client, scope="settings", action="export"
    )
    settings_file = exported["settings_file"]
    settings_file["server_settings"]["backup"] = {
        "enable_settings_transfer_actions": False,
        "enable_snapshot_delete": True,
        "snapshot_delete_min_age_days": 0,
        "auto_backup_retain_per_entity": 12,
    }

    await _manage_backup(
        monkeypatch,
        core_client,
        scope="settings",
        action="import",
        settings_file=settings_file,
    )

    assert _read(settings_world, "backup_settings.json") == {
        "enable_settings_transfer_actions": True,
        "auto_backup_retain_per_entity": 12,
    }


async def test_read_only_mode_allows_the_export_only(
    settings_world: Path,
    monkeypatch: pytest.MonkeyPatch,
    core_client: HomeAssistantClient,
) -> None:
    _allow(settings_world)
    with read_only_request():
        exported = await _manage_backup(
            monkeypatch, core_client, scope="settings", action="export"
        )
        with pytest.raises(ToolError, match=r"[Rr]ead.only"):
            await _manage_backup(
                monkeypatch,
                core_client,
                scope="settings",
                action="import",
                settings_file=exported["settings_file"],
            )


@pytest.mark.parametrize(
    "change",
    [
        {"enable_settings_transfer_actions": True},
        {"enable_snapshot_delete": True},
        {"snapshot_delete_min_age_days": 0},
    ],
)
async def test_developer_tools_cannot_change_the_human_backup_controls(
    settings_world: Path,
    monkeypatch: pytest.MonkeyPatch,
    core_client: HomeAssistantClient,
    change: dict[str, Any],
) -> None:
    with pytest.raises(ToolError, match="human-only"):
        await DevTools(core_client).ha_dev_manage_settings(
            action="set_backup_config", backup=change
        )
