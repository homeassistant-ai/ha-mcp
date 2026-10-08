"""ha_restart replies before restarting when the server runs inside HA (#2691)."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from ha_mcp.tools import system_restart
from ha_mcp.tools.tools_system import SystemTools


def _client() -> AsyncMock:
    client = AsyncMock()
    client.check_config.return_value = {"result": "valid"}
    return client


@pytest.fixture(autouse=True)
def _no_delay(monkeypatch):
    monkeypatch.setattr(system_restart, "_EMBEDDED_RESTART_DELAY_S", 0)


async def test_embedded_replies_before_restart(monkeypatch):
    monkeypatch.setenv("HA_MCP_EMBEDDED", "1")
    client = _client()

    result = await SystemTools(client).ha_restart(confirm=True)

    assert result["success"] is True
    client.call_service.assert_not_awaited()
    await asyncio.gather(*system_restart._RESTART_TASKS)
    client.call_service.assert_awaited_once_with("homeassistant", "restart", {})


async def test_embedded_invalid_config_does_not_schedule(monkeypatch):
    monkeypatch.setenv("HA_MCP_EMBEDDED", "1")
    client = _client()
    client.check_config.return_value = {"result": "invalid", "errors": ["bad"]}

    with pytest.raises(Exception, match="Configuration is invalid"):
        await SystemTools(client).ha_restart(confirm=True)

    assert not system_restart._RESTART_TASKS
    client.call_service.assert_not_awaited()


async def test_external_awaits_restart_call(monkeypatch):
    monkeypatch.delenv("HA_MCP_EMBEDDED", raising=False)
    client = _client()

    result = await SystemTools(client).ha_restart(confirm=True)

    assert result["success"] is True
    client.call_service.assert_awaited_once_with("homeassistant", "restart", {})
    assert not system_restart._RESTART_TASKS
