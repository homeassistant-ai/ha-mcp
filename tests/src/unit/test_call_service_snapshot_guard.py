"""Generic routes apply the snapshot backup controls (#2581).

Service calls, backup/* WebSocket commands and Supervisor /backups requests
sent through ha_call_service or the Code Mode ws_send/api_post bridges that do
what ha_manage_backup(scope="snapshot") does must honour enable_snapshot_actions
and backup_read_only, and snapshot deletion must go through ha_manage_backup so
enable_snapshot_delete and its protections apply.

The server's client is connected to the real ``hass``; spies on its methods
show what was sent while Core still answers.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, NamedTuple
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.setup import async_setup_component
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.config import Settings, get_global_settings
from ha_mcp.tools.tools_service import ServiceTools

from ._code_mode import run_custom_tool
from ._supervisor_core import load_supervisor_api, supervisor_requests
from ._tool_errors import _error

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
    # Supervisor's /v2 mount, reachable once supervisor_v2_api is switched on
    ("supervisor/api", {"endpoint": "/v2/backups/new/full", "method": "post"}),
    (
        "supervisor/api",
        {"endpoint": "/v2/backups/abc12345/restore/full", "method": "post"},
    ),
]
SNAPSHOT_READ_WS = [
    ("backup/info", None),
    ("supervisor/api", {"endpoint": "/backups", "method": "get"}),
    ("supervisor/api", {"endpoint": "/v2/backups", "method": "get"}),
]
SNAPSHOT_DELETE_WS = [
    ("backup/delete", {"backup_id": "abc12345"}),
    ("supervisor/api", {"endpoint": "/backups/abc12345", "method": "delete"}),
    ("supervisor/api", {"endpoint": "/v2/backups/abc12345", "method": "delete"}),
]


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """The server's settings, snapshot actions on and writes allowed."""
    current = get_global_settings()
    monkeypatch.setattr(current, "enable_snapshot_actions", True)
    monkeypatch.setattr(current, "backup_read_only", False)
    monkeypatch.setattr(current, "enable_snapshot_delete", True)
    return current


@pytest.fixture
async def backup_core(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    monkeypatch: pytest.MonkeyPatch,
) -> AiohttpClientMocker:
    """Core's backup integration and Core's own ``supervisor/api``, in front of
    a Supervisor that lists no backups (``_supervisor_core``)."""
    assert await async_setup_component(hass, "backup", {})
    load_supervisor_api(
        hass,
        aioclient_mock,
        monkeypatch,
        {
            ("get", path): {"backups": []}
            for path in ("/backups", "/v2/backups", "/backups_report")
        },
    )
    return aioclient_mock


class Spies(NamedTuple):
    """Call-through spies on the client: Core still answers every call."""

    ws: AsyncMock
    services: AsyncMock
    requests: AsyncMock


@pytest.fixture
def sent(core_client: HomeAssistantClient) -> Iterator[Spies]:
    with (
        patch.object(
            core_client,
            "send_websocket_message",
            wraps=core_client.send_websocket_message,
        ) as ws,
        patch.object(
            core_client, "call_service", wraps=core_client.call_service
        ) as services,
        patch.object(
            core_client, "guarded_request", wraps=core_client.guarded_request
        ) as requests,
    ):
        yield Spies(ws, services, requests)


class TestServiceMode:
    @pytest.mark.parametrize(("domain", "service"), SNAPSHOT_SERVICES)
    async def test_disabled_snapshot_actions_refuse_service(
        self,
        settings: Settings,
        core_client: HomeAssistantClient,
        sent: Spies,
        monkeypatch: pytest.MonkeyPatch,
        domain: str,
        service: str,
    ) -> None:
        monkeypatch.setattr(settings, "enable_snapshot_actions", False)

        with pytest.raises(ToolError) as excinfo:
            await ServiceTools(core_client, None).ha_call_service(
                domain=domain, service=service
            )

        assert "enable_snapshot_actions=false" in _error(excinfo)["message"]
        sent.services.assert_not_awaited()
        sent.ws.assert_not_awaited()

    async def test_backup_read_only_refuses_service(
        self,
        settings: Settings,
        core_client: HomeAssistantClient,
        sent: Spies,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "backup_read_only", True)

        with pytest.raises(ToolError) as excinfo:
            await ServiceTools(core_client, None).ha_call_service(
                domain="hassio", service="restore_full"
            )

        assert "backup_read_only=true" in _error(excinfo)["message"]
        sent.services.assert_not_awaited()

    async def test_other_hassio_services_are_not_gated(
        self,
        hass: HomeAssistant,
        settings: Settings,
        core_client: HomeAssistantClient,
        sent: Spies,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "enable_snapshot_actions", False)
        monkeypatch.setattr(settings, "backup_read_only", True)
        restarted: list[ServiceCall] = []
        hass.services.async_register("hassio", "addon_restart", restarted.append)

        result = await ServiceTools(core_client, None).ha_call_service(
            domain="hassio", service="addon_restart", data={"addon": "core_ssh"}
        )

        assert result["success"] is True
        sent.services.assert_awaited_once()
        assert [call.data["addon"] for call in restarted] == ["core_ssh"]


class TestWsCommandMode:
    @pytest.mark.parametrize(
        ("command", "data"), [*SNAPSHOT_WRITE_WS, *SNAPSHOT_READ_WS]
    )
    async def test_disabled_snapshot_actions_refuse_write_and_listing(
        self,
        settings: Settings,
        core_client: HomeAssistantClient,
        sent: Spies,
        monkeypatch: pytest.MonkeyPatch,
        command: str,
        data: dict[str, Any] | None,
    ) -> None:
        monkeypatch.setattr(settings, "enable_snapshot_actions", False)

        with pytest.raises(ToolError) as excinfo:
            await ServiceTools(core_client, None).ha_call_service(
                ws_command=command, data=data
            )

        assert "enable_snapshot_actions=false" in _error(excinfo)["message"]
        sent.ws.assert_not_awaited()

    async def test_backup_read_only_refuses_write(
        self,
        settings: Settings,
        core_client: HomeAssistantClient,
        sent: Spies,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "backup_read_only", True)

        with pytest.raises(ToolError) as excinfo:
            await ServiceTools(core_client, None).ha_call_service(
                ws_command="backup/generate"
            )

        assert "backup_read_only=true" in _error(excinfo)["message"]
        sent.ws.assert_not_awaited()

    @pytest.mark.parametrize(("command", "data"), SNAPSHOT_READ_WS)
    async def test_backup_read_only_allows_listing(
        self,
        settings: Settings,
        core_client: HomeAssistantClient,
        backup_core: AiohttpClientMocker,
        sent: Spies,
        monkeypatch: pytest.MonkeyPatch,
        command: str,
        data: dict[str, Any] | None,
    ) -> None:
        monkeypatch.setattr(settings, "backup_read_only", True)

        result = await ServiceTools(core_client, None).ha_call_service(
            ws_command=command, data=data
        )

        assert result["success"] is True
        sent.ws.assert_awaited_once_with({"type": command, **(data or {})})

    @pytest.mark.parametrize(("command", "data"), SNAPSHOT_DELETE_WS)
    async def test_snapshot_delete_always_redirects_to_ha_manage_backup(
        self,
        settings: Settings,
        core_client: HomeAssistantClient,
        sent: Spies,
        command: str,
        data: dict[str, Any],
    ) -> None:
        with pytest.raises(ToolError) as excinfo:
            await ServiceTools(core_client, None).ha_call_service(
                ws_command=command, data=data
            )

        error = _error(excinfo)
        assert "deletion is only available" in error["message"]
        assert "ha_manage_backup(scope='snapshot'" in error["suggestion"]
        sent.ws.assert_not_awaited()

    async def test_non_backup_supervisor_endpoint_is_not_gated(
        self,
        settings: Settings,
        core_client: HomeAssistantClient,
        backup_core: AiohttpClientMocker,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "enable_snapshot_actions", False)
        monkeypatch.setattr(settings, "backup_read_only", True)

        result = await ServiceTools(core_client, None).ha_call_service(
            ws_command="supervisor/api",
            data={"endpoint": "/backups_report", "method": "get"},
        )

        assert result["success"] is True
        assert supervisor_requests(backup_core) == [("get", "/backups_report")]


class TestCodeModeBridges:
    @pytest.mark.parametrize(
        "message",
        [
            {"type": "backup/restore", "backup_id": "abc12345"},
            {"type": "call_service", "domain": "hassio", "service": "restore_full"},
        ],
    )
    async def test_ws_send_refuses_snapshot_write_when_disabled(
        self,
        settings: Settings,
        core_client: HomeAssistantClient,
        sent: Spies,
        monkeypatch: pytest.MonkeyPatch,
        message: dict[str, Any],
    ) -> None:
        monkeypatch.setattr(settings, "enable_snapshot_actions", False)

        result = await run_custom_tool(
            core_client, monkeypatch, f"await ws_send({message!r})"
        )

        assert "enable_snapshot_actions=false" in result["error"]
        sent.ws.assert_not_awaited()

    @pytest.mark.parametrize(
        "message",
        [
            {"type": "backup/delete", "backup_id": "x"},
            {
                "type": "supervisor/api",
                "endpoint": "/backups/abc12345",
                "method": "delete",
            },
            {
                "type": "supervisor/api",
                "endpoint": "/v2/backups/abc12345",
                "method": "delete",
            },
        ],
    )
    async def test_ws_send_refuses_snapshot_delete(
        self,
        settings: Settings,
        core_client: HomeAssistantClient,
        sent: Spies,
        monkeypatch: pytest.MonkeyPatch,
        message: dict[str, Any],
    ) -> None:
        result = await run_custom_tool(
            core_client, monkeypatch, f"await ws_send({message!r})"
        )

        assert "ha_manage_backup(scope='snapshot'" in result["error"]
        sent.ws.assert_not_awaited()

    async def test_ws_send_allows_snapshot_listing_under_backup_read_only(
        self,
        settings: Settings,
        core_client: HomeAssistantClient,
        backup_core: AiohttpClientMocker,
        sent: Spies,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "backup_read_only", True)

        result = await run_custom_tool(
            core_client, monkeypatch, 'await ws_send({"type": "backup/info"})'
        )

        sent.ws.assert_awaited_once_with({"type": "backup/info"})
        assert result["success"] is True

    @pytest.mark.parametrize(
        "endpoint",
        [
            "/api/services/hassio/backup_full",
            "services/%68assio/restore_full",
            "hassio/backups/abc12345/restore/full",
            "hassio/v2/backups/abc12345/restore/full",
        ],
    )
    async def test_api_post_refuses_snapshot_write_under_backup_read_only(
        self,
        settings: Settings,
        core_client: HomeAssistantClient,
        sent: Spies,
        monkeypatch: pytest.MonkeyPatch,
        endpoint: str,
    ) -> None:
        monkeypatch.setattr(settings, "backup_read_only", True)

        result = await run_custom_tool(
            core_client, monkeypatch, f"await api_post({endpoint!r}, {{}})"
        )

        assert "backup_read_only=true" in result["error"]
        sent.requests.assert_not_awaited()
