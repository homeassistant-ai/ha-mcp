"""ha_restart replies before restarting when the server runs inside HA (#2691).

Replying first means Core's own refusals reach nobody, so anything Core would
refuse the restart for is read before the reply (#2632).
"""

import asyncio
import logging
from unittest.mock import AsyncMock

import httpx
import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantConnectionError
from ha_mcp.tools import system_restart
from ha_mcp.tools.tools_system import SystemTools

_RECORDER_IDLE = {
    "success": True,
    "result": {"migration_in_progress": False, "migration_is_live": False},
}
_BACKUP_IDLE = {"success": True, "result": {"state": "idle"}}


def _client(recorder: dict | None = None, backup: dict | None = None) -> AsyncMock:
    client = AsyncMock()
    client.check_config.return_value = {"result": "valid"}
    replies = {
        "recorder/info": recorder or _RECORDER_IDLE,
        "backup/info": backup or _BACKUP_IDLE,
    }
    client.send_websocket_message.side_effect = lambda msg: replies[msg["type"]]
    return client


@pytest.fixture(autouse=True)
def _no_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(system_restart, "_EMBEDDED_RESTART_DELAY_S", 0)


async def test_embedded_replies_before_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HA_MCP_EMBEDDED", "1")
    client = _client()

    result = await SystemTools(client).ha_restart(confirm=True)

    assert result["success"] is True
    client.call_service.assert_not_awaited()
    await asyncio.gather(*system_restart._RESTART_TASKS)
    client.call_service.assert_awaited_once_with(
        "homeassistant", "restart", {"safe_mode": False}
    )


def _raise_from(cause: Exception) -> HomeAssistantConnectionError:
    try:
        raise HomeAssistantConnectionError(f"HTTP error: {cause}") from cause
    except HomeAssistantConnectionError as err:
        return err


async def _run_failing_restart(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    monkeypatch.setenv("HA_MCP_EMBEDDED", "1")
    client = _client()
    client.call_service.side_effect = error
    await SystemTools(client).ha_restart(confirm=True)
    await asyncio.gather(*system_restart._RESTART_TASKS)


async def test_embedded_lost_reply_logs_one_line(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    error = _raise_from(
        httpx.RemoteProtocolError("Server disconnected without sending a response.")
    )

    with caplog.at_level(logging.WARNING, logger=system_restart.__name__):
        await _run_failing_restart(monkeypatch, error)

    (record,) = caplog.records
    assert record.levelno == logging.WARNING
    assert "dispatched" in record.getMessage()
    assert record.exc_info is None


async def test_embedded_connect_error_keeps_traceback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    error = _raise_from(httpx.ConnectError("Connection refused"))

    with caplog.at_level(logging.WARNING, logger=system_restart.__name__):
        await _run_failing_restart(monkeypatch, error)

    (record,) = caplog.records
    assert "failed" in record.getMessage()
    assert record.exc_info is not None


async def test_embedded_invalid_config_does_not_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HA_MCP_EMBEDDED", "1")
    client = _client()
    client.check_config.return_value = {"result": "invalid", "errors": ["bad"]}

    with pytest.raises(Exception, match="Configuration is invalid"):
        await SystemTools(client).ha_restart(confirm=True)

    assert not system_restart._RESTART_TASKS
    client.call_service.assert_not_awaited()


async def test_external_awaits_restart_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HA_MCP_EMBEDDED", raising=False)
    client = _client()

    result = await SystemTools(client).ha_restart(confirm=True)

    assert result["success"] is True
    client.call_service.assert_awaited_once_with(
        "homeassistant", "restart", {"safe_mode": False}
    )
    assert not system_restart._RESTART_TASKS


async def test_embedded_db_upgrade_is_refused_not_reported_as_scheduled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HA_MCP_EMBEDDED", "1")
    client = _client(
        recorder={
            "success": True,
            "result": {"migration_in_progress": True, "migration_is_live": False},
        }
    )

    with pytest.raises(ToolError, match="database upgrade"):
        await SystemTools(client).ha_restart(confirm=True)

    assert not system_restart._RESTART_TASKS
    client.call_service.assert_not_awaited()


@pytest.mark.parametrize(
    "recorder",
    [
        {"success": False, "error": "Unknown command."},
        {"success": True, "result": {"recording": True}},
    ],
    ids=["command-failed", "no-migration-field"],
)
async def test_embedded_unreadable_recorder_restarts_and_says_unchecked(
    monkeypatch: pytest.MonkeyPatch, recorder: dict
) -> None:
    monkeypatch.setenv("HA_MCP_EMBEDDED", "1")
    client = _client(recorder=recorder)

    result = await SystemTools(client).ha_restart(confirm=True)

    assert result["success"] is True
    assert any("database upgrade" in w for w in result["warnings"])
    await asyncio.gather(*system_restart._RESTART_TASKS)
    client.call_service.assert_awaited_once()


async def test_safe_mode_reaches_core(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HA_MCP_EMBEDDED", raising=False)
    client = _client()

    await SystemTools(client).ha_restart(confirm=True, safe_mode=True)

    client.call_service.assert_awaited_once_with(
        "homeassistant", "restart", {"safe_mode": True}
    )


async def test_embedded_safe_mode_warns_server_stays_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HA_MCP_EMBEDDED", "1")
    client = _client()

    result = await SystemTools(client).ha_restart(confirm=True, safe_mode=True)

    assert any("next restarted normally" in w for w in result["warnings"])
    await asyncio.gather(*system_restart._RESTART_TASKS)
    client.call_service.assert_awaited_once_with(
        "homeassistant", "restart", {"safe_mode": True}
    )


@pytest.mark.parametrize(
    "state", ["create_backup", "receive_backup", "restore_backup", "blocked"]
)
async def test_embedded_busy_backup_is_refused_not_reported_as_scheduled(
    monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    monkeypatch.setenv("HA_MCP_EMBEDDED", "1")
    client = _client(backup={"success": True, "result": {"state": state}})

    with pytest.raises(ToolError, match=state):
        await SystemTools(client).ha_restart(confirm=True)

    assert not system_restart._RESTART_TASKS
    client.call_service.assert_not_awaited()


@pytest.mark.parametrize(
    "backup",
    [
        {"success": False, "error": "Unknown command."},
        {"success": True, "result": {"backups": []}},
    ],
    ids=["command-failed", "no-state-field"],
)
async def test_embedded_unreadable_backup_restarts_and_says_unchecked(
    monkeypatch: pytest.MonkeyPatch, backup: dict
) -> None:
    monkeypatch.setenv("HA_MCP_EMBEDDED", "1")
    client = _client(backup=backup)

    result = await SystemTools(client).ha_restart(confirm=True)

    assert result["success"] is True
    assert any("running backup" in w for w in result["warnings"])
    await asyncio.gather(*system_restart._RESTART_TASKS)
    client.call_service.assert_awaited_once()
