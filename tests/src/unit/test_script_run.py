"""Script start/stop through ha_config_set_script(run=...).

The script lives in real Core (``script.morning``); the tool talks to it
through the server's own client.
"""

import json
from contextlib import suppress
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import async_capture_events

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.exceptions import HomeAssistantCommandTimeout
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.tools import auto_backup, entity_registration
from ha_mcp.tools.tools_config_scripts import ConfigScriptTools


@pytest.fixture
async def tools(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> ConfigScriptTools:
    """The script tools on a ``hass`` that has the script ``morning``."""
    monkeypatch.setattr(entity_registration, "RESOLVE_TIMEOUT", 0)
    assert await async_setup_component(hass, "config", {})
    assert await async_setup_component(
        hass,
        "script",
        {"script": {"morning": {"sequence": [{"event": "morning_ran"}]}}},
    )
    return ConfigScriptTools(core_client)


@pytest.mark.unit
async def test_run_start_passes_variables(
    hass: HomeAssistant, tools: ConfigScriptTools, core_client: HomeAssistantClient
) -> None:
    with (
        patch.object(
            core_client, "call_service", wraps=core_client.call_service
        ) as call_service,
        patch.object(
            core_client,
            "upsert_script_config",
            wraps=core_client.upsert_script_config,
        ) as upsert,
    ):
        result = await tools.ha_config_set_script(
            script_id="script.morning",
            run="start",
            variables={"brightness": 50},
            MandatoryBPS=False,
        )
        await hass.async_block_till_done()

    assert result["action"] == "start"
    assert result["entity_id"] == "script.morning"
    call_service.assert_awaited_once_with(
        "script",
        "turn_on",
        {"entity_id": "script.morning", "variables": {"brightness": 50}},
    )
    upsert.assert_not_called()


@pytest.mark.unit
async def test_run_stop_calls_turn_off(
    tools: ConfigScriptTools, core_client: HomeAssistantClient
) -> None:
    with patch.object(
        core_client, "call_service", wraps=core_client.call_service
    ) as call_service:
        result = await tools.ha_config_set_script(
            script_id="morning", run="stop", MandatoryBPS=False
        )

    assert result["action"] == "stop"
    call_service.assert_awaited_once_with(
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
        {"run": "start", "run_sequence": [{"event": "x"}]},
        {"run_sequence": [{"event": "x"}], "config": {"sequence": []}},
        {"run_sequence": [{"event": "x"}], "category": "evening"},
    ],
)
async def test_run_rejects_invalid_combinations(
    tools: ConfigScriptTools, core_client: HomeAssistantClient, kwargs: dict[str, Any]
) -> None:
    with (
        patch.object(
            core_client, "call_service", wraps=core_client.call_service
        ) as call_service,
        patch.object(
            core_client,
            "upsert_script_config",
            wraps=core_client.upsert_script_config,
        ) as upsert,
        pytest.raises(ToolError) as exc_info,
    ):
        await tools.ha_config_set_script(script_id="morning", **kwargs)

    error = json.loads(str(exc_info.value))["error"]
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    call_service.assert_not_called()
    upsert.assert_not_called()


@pytest.mark.unit
async def test_run_unknown_script_is_not_found(
    tools: ConfigScriptTools, core_client: HomeAssistantClient
) -> None:
    with (
        patch.object(
            core_client, "call_service", wraps=core_client.call_service
        ) as call_service,
        pytest.raises(ToolError) as exc_info,
    ):
        await tools.ha_config_set_script(script_id="missing", run="start")

    assert json.loads(str(exc_info.value))["error"]["code"] == "RESOURCE_NOT_FOUND"
    call_service.assert_not_called()


@pytest.mark.unit
async def test_run_service_failure_raises(
    tools: ConfigScriptTools, core_client: HomeAssistantClient
) -> None:
    with (
        patch.object(
            core_client,
            "call_service",
            AsyncMock(side_effect=HomeAssistantConnectionError("connection lost")),
        ),
        pytest.raises(ToolError) as exc_info,
    ):
        await tools.ha_config_set_script(script_id="morning", run="stop")

    assert json.loads(str(exc_info.value))["error"]["code"] == "CONNECTION_FAILED"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"script_id": "morning", "run": "start"}, True),
        ({"script_id": "morning", "run_sequence": [{"event": "x"}]}, True),
        # No sequence: refused before any write, after the backup decorator ran.
        ({"script_id": "morning", "config": {"alias": "x"}}, False),
    ],
)
async def test_script_backup_skips_run(
    hass: HomeAssistant,
    tools: ConfigScriptTools,
    kwargs: dict[str, Any],
    expected: bool,
) -> None:
    """A run start/stop takes no config snapshot: the backup decorator skips it
    before it even reads the backup settings; a config write reads them."""
    with (
        patch.object(
            auto_backup,
            "get_global_settings",
            wraps=auto_backup.get_global_settings,
        ) as settings,
        suppress(ToolError),
    ):
        await tools.ha_config_set_script(**kwargs, MandatoryBPS=False)
    await hass.async_block_till_done()

    assert settings.called is not expected


# --- run_sequence: the script editor's Run on one action -----------------------


@pytest.mark.unit
async def test_run_sequence_runs_the_actions_with_their_variables(
    hass: HomeAssistant, tools: ConfigScriptTools
) -> None:
    ran = async_capture_events(hass, "step_ran")

    result = await tools.ha_config_set_script(
        script_id="morning",
        run_sequence=[{"event": "step_ran", "event_data": {"level": "{{ level }}"}}],
        variables={"level": 3},
    )

    assert result["action"] == "run_sequence"
    assert [event.data for event in ran] == [{"level": 3}]


@pytest.mark.unit
async def test_run_sequence_returns_the_response_of_a_stop_step(
    tools: ConfigScriptTools,
) -> None:
    result = await tools.ha_config_set_script(
        script_id="morning",
        run_sequence=[
            {"variables": {"answer": {"ok": True}}},
            {"stop": "done", "response_variable": "answer"},
        ],
    )

    assert result["response"] == {"ok": True}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("sequence", "code"),
    [
        # Core's schema refuses it before the run starts.
        ([{"event": "step_ran"}, {"delay": "not a time"}], "VALIDATION_FAILED"),
        # The step runs and Core has no such action.
        ([{"event": "step_ran"}, {"action": "nope.nothing"}], "SERVICE_CALL_FAILED"),
    ],
    ids=["refused", "failed-while-running"],
)
async def test_run_sequence_reports_cores_refusal_and_what_ran(
    hass: HomeAssistant,
    tools: ConfigScriptTools,
    sequence: list[dict[str, Any]],
    code: str,
) -> None:
    ran = async_capture_events(hass, "step_ran")

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_config_set_script(script_id="morning", run_sequence=sequence)

    assert json.loads(str(exc_info.value))["error"]["code"] == code
    assert len(ran) == (0 if code == "VALIDATION_FAILED" else 1)


@pytest.mark.unit
async def test_run_sequence_without_an_answer_says_it_may_still_run(
    tools: ConfigScriptTools, core_client: HomeAssistantClient
) -> None:
    """Core answers when the run ends; a run past the wait is not a failure,
    and sending it again would repeat its steps."""
    with patch.object(
        core_client,
        "send_websocket_message",
        AsyncMock(side_effect=HomeAssistantCommandTimeout("Command timeout")),
    ):
        result = await tools.ha_config_set_script(
            script_id="morning", run_sequence=[{"delay": {"minutes": 5}}]
        )

    assert result["partial"] is True
    assert "Do not send the actions again" in result["warnings"][0]
