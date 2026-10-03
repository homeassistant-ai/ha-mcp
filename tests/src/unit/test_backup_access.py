"""Backup controls reject explicit operations before touching Home Assistant."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp._vendor.fastmcp import Client, FastMCP
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.backup_manager import BackupManager, DomainHandler
from ha_mcp.config import (
    BACKUP_OVERRIDE_FIELDS,
    Settings,
    get_backup_setting_origin,
    get_global_settings,
    reset_global_settings,
)
from ha_mcp.read_only import read_only_request
from ha_mcp.tools import backup
from ha_mcp.tools.auto_backup import with_auto_backup
from ha_mcp.tools.tools_dev import DevTools
from ha_mcp.transforms.categorized_search import CategorizedSearchTransform

pytestmark = pytest.mark.asyncio


@pytest.fixture
def backup_world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    values = Settings().model_dump()
    values.update(
        enable_snapshot_actions=True,
        backup_read_only=False,
        read_only_mode=False,
        enable_snapshot_delete=False,
        enable_auto_backup=True,
        auto_backup_dir=str(tmp_path / "backups"),
    )
    settings = SimpleNamespace(**values)
    for module in (backup, "ha_mcp.read_only", "ha_mcp.tools.auto_backup"):
        if isinstance(module, str):
            monkeypatch.setattr(f"{module}.get_global_settings", lambda: settings)
        else:
            monkeypatch.setattr(module, "get_global_settings", lambda: settings)
    ha = MagicMock()
    ha.base_url = "http://backup-test.invalid"
    ha.token = "test-token"
    ha.verify_ssl = True
    ha.send_websocket_message = AsyncMock(
        return_value={"success": True, "result": {"backups": []}}
    )
    ws = MagicMock()
    ws.send_command = AsyncMock(
        return_value={"success": True, "result": {"backups": []}}
    )
    ws.disconnect = AsyncMock()
    connect = AsyncMock(return_value=(ws, None))
    monkeypatch.setattr(backup, "get_connected_ws_client", connect)
    manager = BackupManager(settings, ha)

    async def fetch(_client: Any, _entity_id: str) -> dict[str, Any]:
        return {"alias": "Before edit", "actions": []}

    async def restore(_client: Any, _entity_id: str, config: Any) -> dict[str, Any]:
        return {"config": config}

    manager.register(DomainHandler(domain="automation", fetch=fetch, restore=restore))
    get_manager = MagicMock(return_value=manager)
    monkeypatch.setattr(backup, "get_backup_manager", get_manager)
    monkeypatch.setattr("ha_mcp.tools.auto_backup.get_backup_manager", get_manager)
    mcp = FastMCP("backup-access-test")
    backup.register_backup_tools(mcp, ha)
    return SimpleNamespace(
        settings=settings,
        ha=ha,
        ws=ws,
        connect=connect,
        manager=manager,
        get_manager=get_manager,
        mcp=mcp,
    )


async def _direct(world: SimpleNamespace, **arguments: Any) -> dict[str, Any]:
    tool = await world.mcp.get_tool("ha_manage_backup")
    return await tool.fn(**arguments)


def _assert_no_io(world: SimpleNamespace) -> None:
    world.connect.assert_not_awaited()
    world.ws.send_command.assert_not_awaited()
    assert world.ha.mock_calls == []
    world.get_manager.assert_not_called()


@pytest.mark.parametrize("action", ["list", "create", "restore", "delete"])
async def test_disabled_snapshots_block_every_action_before_io(
    backup_world: SimpleNamespace,
    action: str,
) -> None:
    backup_world.settings.enable_snapshot_actions = False
    backup_world.settings.enable_snapshot_delete = True
    with pytest.raises(ToolError, match="enable_snapshot_actions"):
        await _direct(
            backup_world,
            scope="snapshot",
            action=action,
            backup_id="snapshot-id",
            confirm=True,
        )
    _assert_no_io(backup_world)


@pytest.mark.parametrize("scope", ["snapshot", "edits"])
@pytest.mark.parametrize("action", ["create", "restore", "delete"])
async def test_backup_read_only_blocks_explicit_writes_before_io(
    backup_world: SimpleNamespace,
    scope: str,
    action: str,
) -> None:
    backup_world.settings.backup_read_only = True
    backup_world.settings.enable_snapshot_delete = True
    with pytest.raises(ToolError, match="backup_read_only"):
        await _direct(
            backup_world,
            scope=scope,
            action=action,
            backup_id="snapshot-id",
            backup_name="automation.kitchen.20261003_120000.yaml",
            confirm=True,
            domain="automation",
            entity_id="kitchen",
        )
    _assert_no_io(backup_world)


@pytest.mark.parametrize(
    ("scope", "action"),
    [("snapshot", "list"), ("edits", "list"), ("edits", "view"), ("edits", "diff")],
)
async def test_backup_read_only_keeps_backup_inspection_available(
    backup_world: SimpleNamespace,
    scope: str,
    action: str,
) -> None:
    path = await backup_world.manager.maybe_snapshot("automation", "kitchen")
    assert path is not None
    backup_world.settings.backup_read_only = True
    # Disabling snapshots must not disable the edits scope.
    backup_world.settings.enable_snapshot_actions = scope == "snapshot"
    result = await _direct(
        backup_world,
        scope=scope,
        action=action,
        domain="automation",
        backup_name=path.name,
    )
    assert result["success"] is True


@pytest.mark.parametrize("restriction", ["global", "request"])
async def test_backup_settings_cannot_lift_global_or_request_read_only(
    backup_world: SimpleNamespace,
    restriction: str,
) -> None:
    if restriction == "global":
        backup_world.settings.read_only_mode = True
        with pytest.raises(ToolError, match="READ_ONLY_MODE"):
            await _direct(backup_world, scope="snapshot", action="create")
    else:
        with read_only_request(), pytest.raises(ToolError, match="READ_ONLY_MODE"):
            await _direct(backup_world, scope="snapshot", action="create")
    _assert_no_io(backup_world)


async def test_snapshot_delete_still_requires_its_separate_opt_in(
    backup_world: SimpleNamespace,
) -> None:
    with pytest.raises(ToolError, match="enable_snapshot_delete"):
        await _direct(
            backup_world,
            scope="snapshot",
            action="delete",
            backup_id="old",
            confirm=True,
        )
    _assert_no_io(backup_world)


async def test_existing_registration_observes_live_backup_controls(
    backup_world: SimpleNamespace,
) -> None:
    for field, blocked_action in (
        ("enable_snapshot_actions", "list"),
        ("backup_read_only", "create"),
    ):
        setattr(backup_world.settings, field, field == "backup_read_only")
        with pytest.raises(ToolError, match=field):
            await _direct(backup_world, scope="snapshot", action=blocked_action)
        setattr(backup_world.settings, field, field != "backup_read_only")
        assert (await _direct(backup_world, scope="snapshot", action="list"))["success"]


@pytest.mark.parametrize(
    ("control", "action", "proxy"),
    [
        ("enable_snapshot_actions", "list", None),
        ("enable_snapshot_actions", "list", "ha_call_read_tool"),
        ("enable_snapshot_actions", "list", "ha_call_write_tool"),
        ("enable_snapshot_actions", "list", "ha_call_delete_tool"),
        ("backup_read_only", "create", None),
        ("backup_read_only", "create", "ha_call_write_tool"),
        ("backup_read_only", "create", "ha_call_delete_tool"),
    ],
)
async def test_registered_transport_and_categorized_proxies_enforce_backup_controls(
    backup_world: SimpleNamespace,
    control: str,
    action: str,
    proxy: str | None,
) -> None:
    setattr(backup_world.settings, control, control == "backup_read_only")
    backup_world.mcp.add_transform(
        CategorizedSearchTransform(always_visible=["ha_manage_backup"])
    )
    arguments = {"scope": "snapshot", "action": action}
    async with Client(backup_world.mcp) as client:
        with pytest.raises(ToolError, match=control):
            await client.call_tool(
                proxy or "ha_manage_backup",
                {"name": "ha_manage_backup", "arguments": arguments}
                if proxy
                else arguments,
            )
    _assert_no_io(backup_world)


async def test_backup_read_only_keeps_automatic_pre_edit_capture(
    backup_world: SimpleNamespace,
) -> None:
    backup_world.settings.backup_read_only = True
    backup_world.settings.enable_snapshot_actions = False
    wrote: list[str] = []

    @with_auto_backup(domain="automation", id_param="entity_id", client=backup_world.ha)
    async def edit(*, entity_id: str) -> None:
        entries = backup_world.manager.list_snapshots(domain="automation")
        assert len(entries) == 1  # Capture must precede the write.
        saved = backup_world.manager.read_snapshot(entries[0]["name"])
        assert saved["config"]["alias"] == "Before edit"
        wrote.append(entity_id)

    await edit(entity_id="kitchen")
    assert wrote == ["kitchen"]


@pytest.mark.parametrize(
    ("field", "env", "value"),
    [
        ("enable_snapshot_actions", "ENABLE_SNAPSHOT_ACTIONS", False),
        ("backup_read_only", "BACKUP_READ_ONLY", True),
    ],
)
async def test_env_and_persisted_backup_controls_reach_runtime_settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field: str,
    env: str,
    value: bool,
) -> None:
    monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(tmp_path))
    for row in BACKUP_OVERRIDE_FIELDS:
        monkeypatch.delenv(row.env, raising=False)
    monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr("ha_mcp.config_backup.is_running_in_addon", lambda: False)
    (tmp_path / "backup_settings.json").write_text(json.dumps({field: value}))
    reset_global_settings()
    assert getattr(get_global_settings(), field) is value
    assert get_backup_setting_origin(env) == "file"
    monkeypatch.setenv(env, str(not value).lower())
    reset_global_settings()
    assert getattr(get_global_settings(), field) is not value
    assert get_backup_setting_origin(env) == "env"


@pytest.mark.parametrize(
    ("field", "value"), [("enable_snapshot_actions", True), ("backup_read_only", False)]
)
async def test_developer_backup_write_cannot_change_human_controls(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: bool,
) -> None:
    apply_config = AsyncMock()
    monkeypatch.setattr(
        "ha_mcp.settings_ui._handlers_backups.apply_backup_config", apply_config
    )
    dev = DevTools(MagicMock())
    with pytest.raises(ToolError, match="human"):
        await dev.ha_dev_manage_settings(
            action="set_backup_config",
            backup={field: value, "enable_auto_backup": False},
        )
    apply_config.assert_not_awaited()
    for action in ("set", "reset"):
        with pytest.raises(ToolError, match="Unknown setting"):
            await dev.ha_dev_manage_settings(action=action, setting=field, value=True)


async def test_developer_reads_explain_human_control_editability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for row in BACKUP_OVERRIDE_FIELDS:
        monkeypatch.delenv(row.env, raising=False)
    reset_global_settings()
    fields = (
        await DevTools(MagicMock()).ha_dev_manage_settings(action="get_backup_config")
    )["data"]["fields"]
    rows = {row["field"]: row for row in fields}
    for field in ("enable_snapshot_actions", "backup_read_only"):
        assert rows[field]["editable"] is False
        assert rows[field]["locked_reason"] == "human_only"
    assert rows["enable_auto_backup"]["editable"] is True


async def test_default_controls_preserve_snapshot_creation(
    backup_world: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for row in BACKUP_OVERRIDE_FIELDS:
        monkeypatch.delenv(row.env, raising=False)
    defaults = Settings()
    monkeypatch.setattr(backup, "get_global_settings", lambda: defaults)
    create = AsyncMock(return_value={"success": True, "backup_id": "created"})
    monkeypatch.setattr(backup, "create_backup", create)
    result = await _direct(
        backup_world, scope="snapshot", action="create", name="Before edit"
    )
    assert result["backup_id"] == "created"
    create.assert_awaited_once_with(backup_world.ha, "Before edit", ctx=None)


async def test_snapshot_toggle_does_not_block_explicit_edit_capture(
    backup_world: SimpleNamespace,
) -> None:
    backup_world.settings.enable_snapshot_actions = False
    backup_world.settings.enable_auto_backup = False
    result = await _direct(
        backup_world,
        scope="edits",
        action="create",
        domain="automation",
        entity_id="kitchen",
    )
    assert result["success"] is True
    entries = backup_world.manager.list_snapshots(domain="automation")
    assert len(entries) == 1
    assert (
        backup_world.manager.read_snapshot(entries[0]["name"])["config"]["alias"]
        == "Before edit"
    )
