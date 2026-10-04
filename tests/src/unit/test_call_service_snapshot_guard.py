"""Generic routes apply the snapshot backup controls (#2581).

Service calls, backup/* WebSocket commands and Supervisor /backups requests
sent through ha_call_service or the Code Mode ws_send/api_post bridges that do
what ha_manage_backup(scope="snapshot") does must honour enable_snapshot_actions
and backup_read_only, and snapshot deletion must go through ha_manage_backup so
enable_snapshot_delete and its protections apply.
"""

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools import backup_access
from ha_mcp.tools.tools_code import _SandboxBridge
from ha_mcp.tools.tools_service import ServiceTools

pytestmark = pytest.mark.asyncio

SNAPSHOT_SERVICES = [
    ("hassio", "backup_partial"),
    ("backup", "create_automatic"),
    # HA lowercases the domain on lookup, so a mixed-case spelling still runs.
    ("HASSIO", " Restore_Full "),
]
SNAPSHOT_WRITE_WS = [
    ("backup/generate", None),
    ("Backup/Restore", {"backup_id": "abc12345", "agent_id": "backup.local"}),
    ("supervisor/api", {"endpoint": "/backups/new/full", "method": "post"}),
    (
        "supervisor/api",
        {"endpoint": "/backups/abc12345/restore/full", "method": "post"},
    ),
    (
        "supervisor/api",
        {"endpoint": "//addons/../backups/new/partial", "method": "POST"},
    ),
    ("supervisor/api", {"endpoint": "/%62ackups/new/full", "method": "post"}),
]
SNAPSHOT_READ_WS = [
    ("backup/info", None),
    ("supervisor/api", {"endpoint": "/backups", "method": "get"}),
]
SNAPSHOT_DELETE_WS = [
    ("backup/delete", {"backup_id": "abc12345"}),
    ("supervisor/api", {"endpoint": "/backups/abc12345", "method": "delete"}),
]


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    values = SimpleNamespace(
        enable_snapshot_actions=True,
        backup_read_only=False,
        enable_snapshot_delete=True,
    )
    monkeypatch.setattr(backup_access, "get_global_settings", lambda: values)
    return values


def _make_tools() -> ServiceTools:
    client = MagicMock()
    client.send_websocket_message = AsyncMock(return_value={"success": True})
    client.call_service = AsyncMock(return_value=[])
    return ServiceTools(client, MagicMock())


def _error(excinfo: pytest.ExceptionInfo[ToolError]) -> dict[str, Any]:
    return json.loads(str(excinfo.value))["error"]


class TestServiceMode:
    @pytest.mark.parametrize(("domain", "service"), SNAPSHOT_SERVICES)
    async def test_disabled_snapshot_actions_refuse_service(
        self, settings: SimpleNamespace, domain: str, service: str
    ) -> None:
        settings.enable_snapshot_actions = False
        tools = _make_tools()

        with pytest.raises(ToolError) as excinfo:
            await tools.ha_call_service(domain=domain, service=service)

        assert "enable_snapshot_actions=false" in _error(excinfo)["message"]
        tools._client.call_service.assert_not_awaited()
        tools._client.send_websocket_message.assert_not_awaited()

    async def test_backup_read_only_refuses_service(
        self, settings: SimpleNamespace
    ) -> None:
        settings.backup_read_only = True
        tools = _make_tools()

        with pytest.raises(ToolError) as excinfo:
            await tools.ha_call_service(domain="hassio", service="restore_full")

        assert "backup_read_only=true" in _error(excinfo)["message"]
        tools._client.call_service.assert_not_awaited()

    async def test_other_hassio_services_are_not_gated(
        self, settings: SimpleNamespace
    ) -> None:
        settings.enable_snapshot_actions = False
        settings.backup_read_only = True

        backup_access.guard_snapshot_route(domain="hassio", service="addon_restart")


class TestWsCommandMode:
    @pytest.mark.parametrize(("command", "data"), SNAPSHOT_WRITE_WS)
    async def test_disabled_snapshot_actions_refuse_write(
        self, settings: SimpleNamespace, command: str, data: dict[str, Any] | None
    ) -> None:
        settings.enable_snapshot_actions = False
        tools = _make_tools()

        with pytest.raises(ToolError) as excinfo:
            await tools.ha_call_service(ws_command=command, data=data)

        assert "enable_snapshot_actions=false" in _error(excinfo)["message"]
        tools._client.send_websocket_message.assert_not_awaited()

    async def test_backup_read_only_refuses_write(
        self, settings: SimpleNamespace
    ) -> None:
        settings.backup_read_only = True
        tools = _make_tools()

        with pytest.raises(ToolError) as excinfo:
            await tools.ha_call_service(ws_command="backup/generate")

        assert "backup_read_only=true" in _error(excinfo)["message"]
        tools._client.send_websocket_message.assert_not_awaited()

    @pytest.mark.parametrize(("command", "data"), SNAPSHOT_READ_WS)
    async def test_disabled_snapshot_actions_refuse_listing(
        self, settings: SimpleNamespace, command: str, data: dict[str, Any] | None
    ) -> None:
        settings.enable_snapshot_actions = False
        tools = _make_tools()

        with pytest.raises(ToolError):
            await tools.ha_call_service(ws_command=command, data=data)

        tools._client.send_websocket_message.assert_not_awaited()

    @pytest.mark.parametrize(("command", "data"), SNAPSHOT_READ_WS)
    async def test_backup_read_only_allows_listing(
        self, settings: SimpleNamespace, command: str, data: dict[str, Any] | None
    ) -> None:
        settings.backup_read_only = True
        tools = _make_tools()

        result = await tools.ha_call_service(ws_command=command, data=data)

        assert result["success"] is True
        tools._client.send_websocket_message.assert_awaited_once()

    @pytest.mark.parametrize(("command", "data"), SNAPSHOT_DELETE_WS)
    async def test_snapshot_delete_always_redirects_to_ha_manage_backup(
        self, settings: SimpleNamespace, command: str, data: dict[str, Any]
    ) -> None:
        tools = _make_tools()

        with pytest.raises(ToolError) as excinfo:
            await tools.ha_call_service(ws_command=command, data=data)

        error = _error(excinfo)
        assert "deletion is only available" in error["message"]
        assert "ha_manage_backup(scope='snapshot'" in error["suggestion"]
        tools._client.send_websocket_message.assert_not_awaited()

    async def test_non_backup_supervisor_endpoint_is_not_gated(
        self, settings: SimpleNamespace
    ) -> None:
        settings.enable_snapshot_actions = False
        settings.backup_read_only = True
        tools = _make_tools()

        result = await tools.ha_call_service(
            ws_command="supervisor/api",
            data={"endpoint": "/backups_report", "method": "get"},
        )

        assert result["success"] is True


def _make_bridge() -> _SandboxBridge:
    client = MagicMock()
    client.send_websocket_message = AsyncMock(return_value={"success": True})
    client.guarded_request = AsyncMock()
    return _SandboxBridge(
        MagicMock(), client, SimpleNamespace(code_mode_max_invocations=10)
    )


class TestCodeModeBridges:
    @pytest.mark.parametrize(
        "message",
        [
            {"type": "backup/restore", "backup_id": "abc12345"},
            {"type": "call_service", "domain": "hassio", "service": "restore_full"},
        ],
    )
    async def test_ws_send_refuses_snapshot_write_when_disabled(
        self, settings: SimpleNamespace, message: dict[str, Any]
    ) -> None:
        settings.enable_snapshot_actions = False
        bridge = _make_bridge()

        result = await bridge.ws_send(message)

        assert "enable_snapshot_actions=false" in result["error"]
        bridge.client.send_websocket_message.assert_not_awaited()

    async def test_ws_send_refuses_snapshot_delete(
        self, settings: SimpleNamespace
    ) -> None:
        bridge = _make_bridge()

        result = await bridge.ws_send({"type": "backup/delete", "backup_id": "x"})

        assert "ha_manage_backup(scope='snapshot'" in result["error"]
        bridge.client.send_websocket_message.assert_not_awaited()

    async def test_ws_send_allows_snapshot_listing_under_backup_read_only(
        self, settings: SimpleNamespace
    ) -> None:
        settings.backup_read_only = True
        bridge = _make_bridge()

        await bridge.ws_send({"type": "backup/info"})

        bridge.client.send_websocket_message.assert_awaited_once()

    @pytest.mark.parametrize(
        "endpoint",
        [
            "/api/services/hassio/backup_full",
            "services/%68assio/restore_full",
            "hassio/backups/abc12345/restore/full",
        ],
    )
    async def test_api_post_refuses_snapshot_write_under_backup_read_only(
        self, settings: SimpleNamespace, endpoint: str
    ) -> None:
        settings.backup_read_only = True
        bridge = _make_bridge()

        result = await bridge.api_post(endpoint, {})

        assert "backup_read_only=true" in result["error"]
        bridge.client.guarded_request.assert_not_awaited()
