"""The human backup controls refuse explicit backup operations before Home Assistant
is touched, and leave inspection, automatic capture and the Backups tab working.

Every reply is Core's: per-edit backups capture a label in Core's registry
(``_backup_core``), full snapshots go to Core's backup integration
(``_backup_snapshot_core``). The controls are the server's real settings.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import label_registry as lr
from homeassistant.setup import async_setup_component
from starlette.requests import Request

from ha_mcp._vendor.fastmcp import Client, FastMCP
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.backup_manager import BackupManager, get_backup_manager
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient
from ha_mcp.config import (
    BACKUP_OVERRIDE_FIELDS,
    Settings,
    get_backup_setting_origin,
    get_global_settings,
    reset_global_settings,
)
from ha_mcp.read_only import read_only_request
from ha_mcp.settings_ui import build_settings_handlers
from ha_mcp.tools import backup
from ha_mcp.tools.auto_backup import with_auto_backup
from ha_mcp.tools.tools_dev import DevTools
from ha_mcp.transforms.categorized_search import CategorizedSearchTransform

from ._backup_core import add_label, manage_backup, snapshot_of
from ._backup_core import backup_settings as backup_settings
from ._backup_core import snapshot_manager as snapshot_manager
from ._backup_snapshot_core import PASSWORD, BackupInstall
from ._backup_snapshot_core import backup_install as backup_install
from ._backup_snapshot_core import tool_sockets as tool_sockets
from ._client_spies import sent_messages
from ._real_server import build_server

BEFORE = "Before edit"


@pytest.fixture
def label_id(label_registry: lr.LabelRegistry) -> str:
    """A label in Core's registry, named ``BEFORE``."""
    return add_label(label_registry, BEFORE)


@pytest.fixture
async def captured(snapshot_manager: BackupManager, label_id: str) -> Path:
    """A per-edit backup of the label, taken before the test changes a control."""
    return await snapshot_of(snapshot_manager, label_id)


def _registered(client: HomeAssistantClient) -> FastMCP:
    mcp = FastMCP("backup-access-test")
    backup.register_backup_tools(mcp, client)
    return mcp


def _held(manager: BackupManager) -> list[str]:
    return [entry["name"] for entry in manager.list_snapshots()]


def _assert_untouched(
    sockets: list[HomeAssistantWebSocketClient],
    manager: BackupManager,
    held: list[str],
) -> None:
    """No snapshot socket was opened and the per-edit backups are as they were."""
    assert sockets == []
    assert _held(manager) == held


@pytest.mark.parametrize("action", ["list", "delete"])
async def test_disabled_snapshots_block_every_action_before_io(
    core_client: HomeAssistantClient,
    backup_settings: Settings,
    snapshot_manager: BackupManager,
    tool_sockets: list[HomeAssistantWebSocketClient],
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    """Listing is blocked too, and so is a delete the delete opt-in would allow."""
    monkeypatch.setattr(backup_settings, "enable_snapshot_actions", False)
    monkeypatch.setattr(backup_settings, "enable_snapshot_delete", True)
    with (
        sent_messages(core_client) as sent,
        pytest.raises(ToolError, match="enable_snapshot_actions"),
    ):
        await manage_backup(
            core_client,
            scope="snapshot",
            action=action,
            backup_id="snapshot-id",
            confirm=True,
        )
    sent.assert_not_awaited()
    _assert_untouched(tool_sockets, snapshot_manager, [])


@pytest.mark.parametrize("scope", ["snapshot", "edits"])
@pytest.mark.parametrize("action", ["create", "restore", "delete"])
async def test_backup_read_only_blocks_explicit_writes_before_io(
    core_client: HomeAssistantClient,
    backup_settings: Settings,
    snapshot_manager: BackupManager,
    tool_sockets: list[HomeAssistantWebSocketClient],
    captured: Path,
    label_id: str,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
    action: str,
) -> None:
    monkeypatch.setattr(backup_settings, "backup_read_only", True)
    monkeypatch.setattr(backup_settings, "enable_snapshot_delete", True)
    with (
        sent_messages(core_client) as sent,
        pytest.raises(ToolError, match="backup_read_only"),
    ):
        await manage_backup(
            core_client,
            scope=scope,
            action=action,
            backup_id="snapshot-id",
            backup_name=captured.name,
            confirm=True,
            domain="label",
            entity_id=label_id,
        )
    sent.assert_not_awaited()
    _assert_untouched(tool_sockets, snapshot_manager, [captured.name])


@pytest.mark.parametrize(
    ("scope", "action"),
    [("snapshot", "list"), ("edits", "list"), ("edits", "view"), ("edits", "diff")],
)
async def test_backup_read_only_keeps_backup_inspection_available(
    core_client: HomeAssistantClient,
    backup_settings: Settings,
    backup_install: BackupInstall,
    captured: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
    action: str,
) -> None:
    await backup_install.core()
    monkeypatch.setattr(backup_settings, "backup_read_only", True)
    # Disabling snapshots must not disable the edits scope.
    monkeypatch.setattr(backup_settings, "enable_snapshot_actions", scope == "snapshot")
    result = await manage_backup(
        core_client,
        scope=scope,
        action=action,
        domain="label",
        backup_name=captured.name,
    )
    assert result["success"] is True


@pytest.mark.parametrize("restriction", ["global", "request"])
async def test_backup_settings_cannot_lift_global_or_request_read_only(
    core_client: HomeAssistantClient,
    backup_settings: Settings,
    snapshot_manager: BackupManager,
    tool_sockets: list[HomeAssistantWebSocketClient],
    monkeypatch: pytest.MonkeyPatch,
    restriction: str,
) -> None:
    if restriction == "global":
        monkeypatch.setattr(backup_settings, "read_only_mode", True)
        with pytest.raises(ToolError, match="READ_ONLY_MODE"):
            await manage_backup(core_client, scope="snapshot", action="create")
    else:
        with read_only_request(), pytest.raises(ToolError, match="READ_ONLY_MODE"):
            await manage_backup(core_client, scope="snapshot", action="create")
    _assert_untouched(tool_sockets, snapshot_manager, [])


async def test_snapshot_delete_still_requires_its_separate_opt_in(
    core_client: HomeAssistantClient,
    backup_settings: Settings,
    snapshot_manager: BackupManager,
    tool_sockets: list[HomeAssistantWebSocketClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backup_settings, "enable_snapshot_delete", False)
    with pytest.raises(ToolError, match="enable_snapshot_delete"):
        await manage_backup(
            core_client,
            scope="snapshot",
            action="delete",
            backup_id="old",
            confirm=True,
        )
    _assert_untouched(tool_sockets, snapshot_manager, [])


async def test_existing_registration_observes_live_backup_controls(
    core_client: HomeAssistantClient,
    backup_settings: Settings,
    backup_install: BackupInstall,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A control a human changes applies to the tool already registered, both ways."""
    await backup_install.core()
    tool = await _registered(core_client).get_tool("ha_manage_backup")
    for field, blocked_action in (
        ("enable_snapshot_actions", "list"),
        ("backup_read_only", "create"),
    ):
        monkeypatch.setattr(backup_settings, field, field == "backup_read_only")
        with pytest.raises(ToolError, match=field):
            await tool.fn(scope="snapshot", action=blocked_action)
        monkeypatch.setattr(backup_settings, field, field != "backup_read_only")
        with sent_messages(core_client) as sent:
            assert (await tool.fn(scope="snapshot", action="list"))["success"]
        sent.assert_awaited()


@pytest.mark.usefixtures("tool_search_default")
@pytest.mark.parametrize(
    ("control", "action", "proxy"),
    [
        ("enable_snapshot_actions", "list", None),
        ("enable_snapshot_actions", "list", "ha_call_read_tool"),
        ("backup_read_only", "create", "ha_call_write_tool"),
        ("backup_read_only", "create", "ha_call_delete_tool"),
    ],
)
async def test_registered_transport_and_categorized_proxies_enforce_backup_controls(
    core_client: HomeAssistantClient,
    backup_settings: Settings,
    snapshot_manager: BackupManager,
    tool_sockets: list[HomeAssistantWebSocketClient],
    monkeypatch: pytest.MonkeyPatch,
    control: str,
    action: str,
    proxy: str | None,
) -> None:
    monkeypatch.setattr(backup_settings, control, control == "backup_read_only")
    mcp = _registered(core_client)
    mcp.add_transform(CategorizedSearchTransform(always_visible=["ha_manage_backup"]))
    arguments = {"scope": "snapshot", "action": action}
    async with Client(mcp) as client:
        with pytest.raises(ToolError, match=control):
            await client.call_tool(
                proxy or "ha_manage_backup",
                {"name": "ha_manage_backup", "arguments": arguments}
                if proxy
                else arguments,
            )
    _assert_untouched(tool_sockets, snapshot_manager, [])


async def test_restore_selection_reaches_the_restore_as_given(
    core_client: HomeAssistantClient,
    backup_settings: Settings,
    backup_install: BackupInstall,
) -> None:
    """A selection lost on the way would restore no app and back none up.

    Some MCP clients send arrays as JSON strings.
    """
    supervisor = await backup_install.supervisor()
    target = supervisor.add_backup(
        "old", apps=("core_ssh",), folders=("share",), password=None
    )
    arguments = {
        "scope": "snapshot",
        "action": "restore",
        "backup_id": target,
        "restore_addons": '["core_ssh"]',
        "restore_folders": ["share"],
    }
    async with Client(_registered(core_client)) as client:
        await client.call_tool("ha_manage_backup", arguments)
    restore = supervisor.restores[0]
    assert (restore["addons"], restore["folders"]) == (["core_ssh"], ["share"])


async def test_backup_read_only_keeps_automatic_pre_edit_capture(
    core_client: HomeAssistantClient,
    backup_settings: Settings,
    snapshot_manager: BackupManager,
    label_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backup_settings, "backup_read_only", True)
    monkeypatch.setattr(backup_settings, "enable_snapshot_actions", False)
    wrote: list[str] = []

    @with_auto_backup(domain="label", id_param="entity_id", client=core_client)
    async def edit(*, entity_id: str) -> None:
        entries = snapshot_manager.list_snapshots(domain="label")
        assert len(entries) == 1  # Capture must precede the write.
        saved = snapshot_manager.read_snapshot(entries[0]["name"])
        assert saved["config"]["name"] == BEFORE
        wrote.append(entity_id)

    await edit(entity_id=label_id)
    assert wrote == [label_id]


@pytest.mark.parametrize(
    ("field", "env", "value"),
    [
        ("enable_snapshot_actions", "ENABLE_SNAPSHOT_ACTIONS", False),
        ("backup_read_only", "BACKUP_READ_ONLY", True),
    ],
)
def test_env_and_persisted_backup_controls_reach_runtime_settings(
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


@pytest.fixture
def file_backup_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backup settings saved to the override file, with no variable pinning one."""
    for row in BACKUP_OVERRIDE_FIELDS:
        monkeypatch.delenv(row.env, raising=False)
    monkeypatch.setattr("ha_mcp.config_backup.is_running_in_addon", lambda: False)
    reset_global_settings()


@pytest.mark.usefixtures("file_backup_settings")
@pytest.mark.parametrize(
    ("field", "value"), [("enable_snapshot_actions", True), ("backup_read_only", False)]
)
async def test_developer_backup_write_cannot_change_human_controls(
    core_client: HomeAssistantClient,
    field: str,
    value: bool,
) -> None:
    """The whole write is refused: the agent-editable field in it is not saved either."""
    dev = DevTools(core_client)
    with pytest.raises(ToolError, match="human"):
        await dev.ha_dev_manage_settings(
            action="set_backup_config",
            backup={field: value, "enable_auto_backup": False},
        )
    assert get_backup_setting_origin("ENABLE_AUTO_BACKUP") == "default"
    for action in ("set", "reset"):
        with pytest.raises(ToolError, match="Unknown setting"):
            await dev.ha_dev_manage_settings(action=action, setting=field, value=True)


@pytest.mark.usefixtures("file_backup_settings")
async def test_developer_backup_write_saves_agent_editable_settings(
    core_client: HomeAssistantClient,
) -> None:
    await DevTools(core_client).ha_dev_manage_settings(
        action="set_backup_config", backup={"enable_auto_backup": False}
    )
    assert get_backup_setting_origin("ENABLE_AUTO_BACKUP") == "file"
    assert get_global_settings().enable_auto_backup is False


async def test_developer_reads_explain_human_control_editability(
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for row in BACKUP_OVERRIDE_FIELDS:
        monkeypatch.delenv(row.env, raising=False)
    reset_global_settings()
    fields = (
        await DevTools(core_client).ha_dev_manage_settings(action="get_backup_config")
    )["data"]["fields"]
    rows = {row["field"]: row for row in fields}
    for field in ("enable_snapshot_actions", "backup_read_only"):
        assert rows[field]["editable"] is False
        assert rows[field]["locked_reason"] == "human_only"
    assert rows["enable_auto_backup"]["editable"] is True


async def test_default_controls_preserve_snapshot_creation(
    core_client: HomeAssistantClient,
    backup_install: BackupInstall,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for row in BACKUP_OVERRIDE_FIELDS:
        monkeypatch.delenv(row.env, raising=False)
    reset_global_settings()
    await backup_install.core()
    await backup_install.configure(password=PASSWORD)
    result = await manage_backup(
        core_client, scope="snapshot", action="create", name=BEFORE
    )
    assert (await backup_install.listed())[result["backup_id"]]["name"] == BEFORE


async def test_create_parts_reach_the_backup(
    core_client: HomeAssistantClient,
    backup_settings: Settings,
    backup_install: BackupInstall,
) -> None:
    """A dropped part would leave an app, a folder or the database out, or put it in."""
    await backup_install.supervisor()
    await backup_install.configure(password=PASSWORD)
    result = await manage_backup(
        core_client,
        scope="snapshot",
        action="create",
        include_homeassistant=True,
        include_database=False,
        include_addons=["core_ssh"],
        include_folders=["share"],
    )
    made = (await backup_install.listed())[result["backup_id"]]
    assert (made["homeassistant_included"], made["database_included"]) == (True, False)
    assert [app["slug"] for app in made["addons"]] == ["core_ssh"]
    assert "share" in made["folders"]


@pytest.mark.parametrize(
    ("given", "operations"),
    [({}, ["restore"]), ({"safety_backup": True}, ["backup", "restore"])],
    ids=["not-asked", "asked"],
)
async def test_safety_backup_is_made_only_when_asked_for(
    core_client: HomeAssistantClient,
    backup_settings: Settings,
    backup_install: BackupInstall,
    given: dict[str, Any],
    operations: list[str],
) -> None:
    """A dropped or flipped safety_backup would make, or skip, a backup unasked."""
    supervisor = await backup_install.supervisor()
    target = supervisor.add_backup("old")
    await backup_install.configure(password=PASSWORD)
    await manage_backup(
        core_client, scope="snapshot", action="restore", backup_id=target, **given
    )
    assert supervisor.operations == operations


async def test_snapshot_toggle_does_not_block_explicit_edit_capture(
    core_client: HomeAssistantClient,
    backup_settings: Settings,
    snapshot_manager: BackupManager,
    label_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backup_settings, "enable_snapshot_actions", False)
    monkeypatch.setattr(backup_settings, "enable_auto_backup", False)
    result = await manage_backup(
        core_client,
        scope="edits",
        action="create",
        domain="label",
        entity_id=label_id,
    )
    assert result["success"] is True
    entries = snapshot_manager.list_snapshots(domain="label")
    assert len(entries) == 1
    assert (
        snapshot_manager.read_snapshot(entries[0]["name"])["config"]["name"] == BEFORE
    )


@pytest.mark.parametrize("action", ["restore", "delete"])
async def test_human_backup_actions_remain_available_under_tool_restrictions(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    label_registry: lr.LabelRegistry,
    label_id: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    action: str,
) -> None:
    """The Backups tab still restores and deletes per-edit backups."""
    monkeypatch.setenv("ENABLE_SNAPSHOT_ACTIONS", "false")
    monkeypatch.setenv("BACKUP_READ_ONLY", "true")
    monkeypatch.setenv("ENABLE_AUTO_BACKUP", "true")
    monkeypatch.setenv("HAMCP_BACKUP_DIR", str(tmp_path / "backups"))
    server = build_server(monkeypatch, core_client)
    assert await async_setup_component(hass, "config", {})
    manager = get_backup_manager(core_client, get_global_settings())
    path = await snapshot_of(manager, label_id)
    label_registry.async_update(label_id, name="After edit")

    handler = build_settings_handlers(server)[f"{action}_backup"]
    response = await handler(
        Request({"type": "http", "path_params": {"name": path.name}})
    )

    body = json.loads(bytes(response.body))
    assert response.status_code == 200
    assert body["success"] is True
    if action == "restore":
        assert body["data"]["restored_from"] == path.name
        label = label_registry.async_get_label(label_id)
        assert label is not None
        assert label.name == BEFORE
    else:
        assert body["deleted"] == [path.name]
        with pytest.raises(FileNotFoundError):
            manager.read_snapshot(path.name)
