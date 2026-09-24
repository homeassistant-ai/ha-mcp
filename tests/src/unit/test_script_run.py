"""Script start/stop through ha_config_set_script(run=...)."""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantConnectionError,
)
from ha_mcp.tools import entity_registration, tools_config_scripts
from ha_mcp.tools.tools_config_scripts import ConfigScriptTools


@pytest.fixture
def mock_client() -> MagicMock:
    client = MagicMock()
    client.base_url = None
    client.token = None
    client.send_websocket_message = AsyncMock(
        return_value={
            "success": True,
            "result": [
                {
                    "entity_id": "script.morning",
                    "unique_id": "morning",
                    "platform": "script",
                }
            ],
        }
    )
    client.get_entity_state = AsyncMock(
        return_value={"entity_id": "script.morning", "state": "off"}
    )
    client.call_service = AsyncMock(return_value=[])
    client.upsert_script_config = AsyncMock()
    return client


@pytest.fixture
def tools(mock_client: MagicMock, monkeypatch: pytest.MonkeyPatch) -> ConfigScriptTools:
    monkeypatch.setattr(entity_registration, "RESOLVE_TIMEOUT", 0)
    return ConfigScriptTools(mock_client)


@pytest.mark.unit
async def test_run_start_passes_variables(
    tools: ConfigScriptTools, mock_client: MagicMock
) -> None:
    result = await tools.ha_config_set_script(
        script_id="script.morning",
        run="start",
        variables={"brightness": 50},
        MandatoryBPS=False,
    )

    assert result["action"] == "start"
    assert result["entity_id"] == "script.morning"
    mock_client.call_service.assert_awaited_once_with(
        "script",
        "turn_on",
        {"entity_id": "script.morning", "variables": {"brightness": 50}},
    )
    mock_client.upsert_script_config.assert_not_called()


@pytest.mark.unit
async def test_run_stop_calls_turn_off(
    tools: ConfigScriptTools, mock_client: MagicMock
) -> None:
    result = await tools.ha_config_set_script(
        script_id="morning", run="stop", MandatoryBPS=False
    )

    assert result["action"] == "stop"
    mock_client.call_service.assert_awaited_once_with(
        "script", "turn_off", {"entity_id": "script.morning"}
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "kwargs",
    [
        {"run": "start", "config": {"sequence": []}},
        {"run": "start", "python_transform": "config['alias'] = 'x'"},
        {"run": "start", "take_control_of_blueprint": True},
        {"run": "start", "category": "evening"},
        {"run": "stop", "variables": {"brightness": 50}},
        {"variables": {"brightness": 50}},
    ],
)
async def test_run_rejects_invalid_combinations(
    tools: ConfigScriptTools, mock_client: MagicMock, kwargs: dict[str, Any]
) -> None:
    with pytest.raises(ToolError) as exc_info:
        await tools.ha_config_set_script(script_id="morning", **kwargs)

    error = json.loads(str(exc_info.value))["error"]
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    mock_client.call_service.assert_not_called()
    mock_client.upsert_script_config.assert_not_called()


@pytest.mark.unit
async def test_run_unknown_script_is_not_found(
    tools: ConfigScriptTools, mock_client: MagicMock
) -> None:
    mock_client.get_entity_state = AsyncMock(
        side_effect=HomeAssistantAPIError("not found", status_code=404)
    )

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_config_set_script(script_id="missing", run="start")

    assert json.loads(str(exc_info.value))["error"]["code"] == "RESOURCE_NOT_FOUND"
    mock_client.call_service.assert_not_called()


@pytest.mark.unit
async def test_run_service_failure_raises(
    tools: ConfigScriptTools, mock_client: MagicMock
) -> None:
    mock_client.call_service = AsyncMock(
        side_effect=HomeAssistantConnectionError("connection lost")
    )

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_config_set_script(script_id="morning", run="stop")

    assert json.loads(str(exc_info.value))["error"]["code"] == "CONNECTION_FAILED"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"script_id": "morning", "run": "start"}, True),
        ({"script_id": "morning", "config": {"sequence": []}}, False),
    ],
)
def test_script_backup_skips_run(kwargs: dict[str, Any], expected: bool) -> None:
    assert tools_config_scripts._skip_script_run_backup(kwargs) is expected
