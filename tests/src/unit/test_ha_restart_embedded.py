"""ha_restart replies before restarting when the server runs inside HA (#2691)."""

import asyncio
import logging
from unittest.mock import AsyncMock

import httpx
import pytest

from ha_mcp.client.rest_client import HomeAssistantConnectionError
from ha_mcp.tools import system_restart
from ha_mcp.tools.tools_system import SystemTools


def _client() -> AsyncMock:
    client = AsyncMock()
    client.check_config.return_value = {"result": "valid"}
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
    client.call_service.assert_awaited_once_with("homeassistant", "restart", {})


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


def _connection_error(cause: Exception) -> HomeAssistantConnectionError:
    # Same chain the REST client builds when httpx fails (raise ... from e).
    try:
        try:
            raise cause
        except httpx.HTTPError as e:
            raise HomeAssistantConnectionError(f"HTTP error: {e}") from e
    except HomeAssistantConnectionError as wrapped:
        return wrapped


async def _run_deferred_restart(client: AsyncMock) -> None:
    await SystemTools(client).ha_restart(confirm=True)
    await asyncio.gather(*system_restart._RESTART_TASKS)


async def test_embedded_lost_reply_logs_one_line(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("HA_MCP_EMBEDDED", "1")
    client = _client()
    client.call_service.side_effect = _connection_error(
        httpx.RemoteProtocolError("Server disconnected without sending a response.")
    )

    with caplog.at_level(logging.WARNING, logger=system_restart.__name__):
        await _run_deferred_restart(client)

    (record,) = caplog.records
    assert "dispatched" in record.getMessage()
    assert record.exc_info is None


async def test_embedded_connect_error_keeps_traceback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("HA_MCP_EMBEDDED", "1")
    client = _client()
    client.call_service.side_effect = _connection_error(
        httpx.ConnectError("Connection refused")
    )

    with caplog.at_level(logging.WARNING, logger=system_restart.__name__):
        await _run_deferred_restart(client)

    (record,) = caplog.records
    assert record.getMessage() == "Deferred Home Assistant restart failed"
    assert record.exc_info is not None


async def test_external_awaits_restart_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HA_MCP_EMBEDDED", raising=False)
    client = _client()

    result = await SystemTools(client).ha_restart(confirm=True)

    assert result["success"] is True
    client.call_service.assert_awaited_once_with("homeassistant", "restart", {})
    assert not system_restart._RESTART_TASKS
