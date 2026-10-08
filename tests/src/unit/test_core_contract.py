"""Core contracts must follow the registered validator, including future fields."""

from typing import Any
from unittest.mock import Mock

import pytest

from .test_component_ws_search import FakeHass


def test_proposal_uses_registered_schema_without_executing_handler() -> None:
    from custom_components.ha_mcp_tools.core_contract import validate_request

    hass = FakeHass()
    handler = Mock(side_effect=AssertionError("validation must never execute a write"))
    seen: list[dict[str, Any]] = []

    def schema(value: dict[str, Any]) -> dict[str, Any]:
        seen.append(value)
        if "future_required" not in value:
            raise ValueError("future_required is required")
        return value

    hass.data["websocket_api"] = {"energy/save_prefs": (handler, schema)}
    invalid = validate_request(hass, "energy/save_prefs", {})
    assert invalid["valid"] is False
    assert "future_required" in str(invalid["errors"])
    assert (
        validate_request(hass, "energy/save_prefs", {"future_required": []})["valid"]
        is True
    )
    assert seen[-1]["type"] == "energy/save_prefs"
    handler.assert_not_called()


@pytest.mark.parametrize("payload", [{"id": 27}, {"type": "call_service"}])
def test_contract_validation_cannot_override_transport(payload: dict[str, Any]) -> None:
    from custom_components.ha_mcp_tools.core_contract import validate_request

    with pytest.raises(ValueError, match="reserved"):
        validate_request(FakeHass(), "energy/save_prefs", payload)


def test_no_validator_means_unavailable_instead_of_success() -> None:
    from custom_components.ha_mcp_tools.core_contract import validate_request

    assert (
        validate_request(FakeHass(), "energy/save_prefs", {})["status"] == "unavailable"
    )


def test_bridge_cannot_validate_arbitrary_commands() -> None:
    from custom_components.ha_mcp_tools.core_contract import validate_request

    with pytest.raises(ValueError, match="Unsupported"):
        validate_request(FakeHass(), "call_service", {})


@pytest.mark.asyncio
async def test_uncertain_capability_discovery_is_not_reported_as_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    from ha_mcp._vendor.fastmcp.exceptions import ToolError
    from ha_mcp.tools import core_contract as module

    monkeypatch.setattr(
        module, "get_component_caps", AsyncMock(side_effect=RuntimeError("offline"))
    )
    with pytest.raises(ToolError, match="offline"):
        await module.core_contract(Mock(), "energy/save_prefs", {})


@pytest.mark.asyncio
async def test_invalid_native_result_is_a_tool_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    from ha_mcp._vendor.fastmcp.exceptions import ToolError
    from ha_mcp.tools import core_contract as module

    monkeypatch.setattr(
        module,
        "core_contract",
        AsyncMock(
            return_value={
                "status": "validated",
                "valid": False,
                "errors": [{"path": ["future"], "message": "required"}],
            }
        ),
    )
    with pytest.raises(ToolError, match="future"):
        await module.validate_energy_proposal(Mock(), {"future": []})


def test_missing_core_defaults_does_not_hide_available_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.ha_mcp_tools import core_contract as module

    from .test_component_ws_search import _REAL_VOL

    monkeypatch.setattr(
        module, "_defaults", Mock(side_effect=AttributeError("Core changed"))
    )
    hass = FakeHass()
    hass.data["websocket_api"] = {"energy/save_prefs": (Mock(), lambda value: value)}
    execute = module.command_specs(_REAL_VOL)[0][1]
    result = execute(hass, {"command": "energy/save_prefs"})
    assert result["status"] == "available"
    assert "default_preferences" not in result


@pytest.mark.asyncio
async def test_defaults_discovery_failure_keeps_unconfigured_prefs_readable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    from ha_mcp._vendor.fastmcp.exceptions import ToolError
    from ha_mcp.tools import energy_preferences as module

    client = Mock()
    client.send_websocket_message = AsyncMock(
        return_value={"success": False, "error": "Command failed: No prefs"}
    )
    monkeypatch.setattr(
        module,
        "core_contract",
        AsyncMock(side_effect=ToolError("discovery unavailable")),
    )
    result = await module.get_energy_prefs(client)
    assert result["success"] is True
    assert result["config"] == {}
    assert "unavailable" in result["note"]


@pytest.mark.parametrize(
    "command", ["ha_mcp_tools/core_contract", "ha_mcp_tools/statistics_units"]
)
@pytest.mark.parametrize(
    "is_admin,has_user", [(False, True), (True, False), (True, True)]
)
def test_contract_commands_require_admin_before_preparation(
    monkeypatch: pytest.MonkeyPatch, command: str, is_admin: bool, has_user: bool
) -> None:
    from unittest.mock import AsyncMock

    from custom_components.ha_mcp_tools import websocket_api as wsapi

    from .test_component_ws_search import (
        _REAL_VOL,
        _FakeConnection,
        _FakeWSApi,
        _Unauthorized,
    )

    fake = _FakeWSApi()
    monkeypatch.setattr(wsapi, "websocket_api", fake)
    monkeypatch.setattr(wsapi, "vol", _REAL_VOL)
    preparation = AsyncMock(return_value={"records": []})
    monkeypatch.setattr(wsapi.core_contract, "statistics_metadata", preparation)
    for schema, execute, prep in wsapi.core_contract.command_specs(_REAL_VOL):
        fake.async_register_command(
            FakeHass(), wsapi._build_handler(schema, execute, prep)
        )
    connection = _FakeConnection(is_admin=is_admin, has_user=has_user)
    params = (
        {"command": "history/history_during_period"}
        if command == "ha_mcp_tools/core_contract"
        else {"statistic_ids": ["sensor.energy"], "units": {"energy": "kWh"}}
    )

    def call() -> None:
        fake.registered[command](
            FakeHass(), connection, {"id": 1, "type": command, **params}
        )

    if is_admin and has_user:
        call()
        assert 1 in connection.results
        if command.endswith("statistics_units"):
            preparation.assert_awaited_once()
    else:
        with pytest.raises(_Unauthorized):
            call()
        preparation.assert_not_called()
