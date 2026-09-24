"""Integration log levels through ha_set_integration(log_level=...)."""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools.tools_integrations import IntegrationTools


def _client(reply: dict[str, Any] | None = None) -> MagicMock:
    client = MagicMock()
    client.send_websocket_message = AsyncMock(
        return_value=reply or {"success": True, "result": None}
    )
    client.get_config_entry = AsyncMock(return_value={"domain": "zha"})
    return client


@pytest.mark.unit
@pytest.mark.parametrize(("level", "sent"), [("DEBUG", "DEBUG"), ("DEFAULT", "NOTSET")])
async def test_sets_level_like_the_integration_page(level: str, sent: str) -> None:
    client = _client()

    result = await IntegrationTools(client).ha_set_integration(
        domain="zha", log_level=level
    )

    client.send_websocket_message.assert_awaited_once_with(
        {
            "type": "logger/integration_log_level",
            "integration": "zha",
            "level": sent,
            "persistence": "once",
        }
    )
    assert result["action"] == "set_log_level"
    assert result["domain"] == "zha"
    assert result["log_level"] == level


@pytest.mark.unit
async def test_entry_id_resolves_to_its_domain() -> None:
    client = _client()

    result = await IntegrationTools(client).ha_set_integration(
        entry_id="abc123", log_level="INFO"
    )

    client.get_config_entry.assert_awaited_once_with("abc123")
    assert client.send_websocket_message.await_args.args[0]["integration"] == "zha"
    assert result["domain"] == "zha"


@pytest.mark.unit
@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"domain": "zha", "entry_id": "abc123"},
        {"domain": "zha", "config": {"x": 1}},
        {"entry_id": "abc123", "enabled": False},
        {"entry_id": "abc123", "reconfigure": True},
        {"domain": "zha", "confirm_token": "sha256:abc"},
        {"domain": "zha", "expected_mac": "aa:bb:cc:dd:ee:ff"},
    ],
)
async def test_rejects_other_modes(kwargs: dict[str, Any]) -> None:
    client = _client()

    with pytest.raises(ToolError) as exc_info:
        await IntegrationTools(client).ha_set_integration(log_level="DEBUG", **kwargs)

    error = json.loads(str(exc_info.value))["error"]
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    client.send_websocket_message.assert_not_called()


@pytest.mark.unit
async def test_unknown_integration_is_not_found() -> None:
    client = _client(
        {
            "success": False,
            "error": "Command failed: Integration not found",
            "error_code": "not_found",
        }
    )

    with pytest.raises(ToolError) as exc_info:
        await IntegrationTools(client).ha_set_integration(
            domain="nope", log_level="DEBUG"
        )

    error = json.loads(str(exc_info.value))["error"]
    assert error["code"] == "RESOURCE_NOT_FOUND"
