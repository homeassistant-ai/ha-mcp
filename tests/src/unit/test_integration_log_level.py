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
    # NOTSET clears the override; it does not restore a configuration.yaml level.
    assert ("inherited" in result["note"]) is (level == "DEFAULT")


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


@pytest.mark.unit
@pytest.mark.parametrize(
    ("persistence", "note"),
    [("permanent", "every Home Assistant restart"), ("none", "until the next")],
)
async def test_log_persistence_reaches_core(persistence: str, note: str) -> None:
    client = _client()

    result = await IntegrationTools(client).ha_set_integration(
        domain="zha", log_level="DEBUG", log_persistence=persistence
    )

    sent = client.send_websocket_message.await_args.args[0]
    assert sent["persistence"] == persistence
    assert note in result["note"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "kwargs",
    [
        {"domain": "zha"},
        {"entry_id": "abc123", "enabled": True},
        {"entry_id": "abc123", "config": {"x": 1}},
    ],
)
async def test_log_persistence_needs_a_log_level(kwargs: dict[str, Any]) -> None:
    client = _client()

    with pytest.raises(ToolError) as exc_info:
        await IntegrationTools(client).ha_set_integration(
            log_persistence="permanent", **kwargs
        )

    error = json.loads(str(exc_info.value))["error"]
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "log_level" in error["message"]
    client.send_websocket_message.assert_not_called()


@pytest.mark.unit
async def test_core_refusing_a_persistence_value_surfaces_its_error() -> None:
    core_error = "expected LogPersistance for dictionary value @ data['persistence']"
    client = _client(
        {"success": False, "error": core_error, "error_code": "invalid_format"}
    )

    with pytest.raises(ToolError) as exc_info:
        await IntegrationTools(client).ha_set_integration(
            domain="zha", log_level="DEBUG", log_persistence="forever"
        )

    error = json.loads(str(exc_info.value))["error"]
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert core_error in error["message"]
    assert client.send_websocket_message.await_args.args[0]["persistence"] == "forever"
