"""Schema completeness must reflect strict serialization, including nested unions."""

from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from .test_component_ws_search import FakeHass


def test_successful_strict_description_has_no_loss_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.ha_mcp_tools import core_contract as module

    schema = Mock()
    hass = FakeHass()
    hass.data["websocket_api"] = {"history/history_during_period": (Mock(), schema)}
    serializer = Mock(return_value={"type": "object"})
    monkeypatch.setattr(module, "_TO_JSON_SCHEMA", serializer)
    result = module.describe_contract(hass, "history/history_during_period")
    assert serializer.call_args.kwargs["strict"] is True
    assert result["description_complete"] is True
    assert "warnings" not in result


def test_nested_unsupported_validator_cannot_be_marked_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.ha_mcp_tools import core_contract as module

    nested = object()

    def union(child: Any) -> Any:
        value_schemas = {"future": child}
        default_schema = None

        def validator(value: Any) -> Any:
            return value_schemas.get(value, default_schema)

        validator.__module__ = "homeassistant.helpers.config_validation"
        validator.__qualname__ = "key_value_schemas.<locals>.key_value_validator"
        return validator

    validator = union(union(nested))
    strict_attempts: list[bool] = []

    def serialize(node: Any, *, strict: bool = False, custom_serializer: Any) -> Any:
        if node is nested:
            strict_attempts.append(strict)
            if strict:
                raise ValueError("Future callable has no schema serializer")
            return {}
        return custom_serializer(node)

    monkeypatch.setattr(module, "_TO_JSON_SCHEMA", serialize)
    hass = FakeHass()
    hass.data["websocket_api"] = {"energy/save_prefs": (Mock(), validator)}
    result = module.describe_contract(hass, "energy/save_prefs")
    assert strict_attempts == [True, False]
    assert result["description_complete"] is False
    assert result["warnings"]


@pytest.mark.asyncio
async def test_info_advertises_contract_for_the_server_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.ha_mcp_tools import websocket_api as wsapi
    from ha_mcp.tools import component_api, core_contract

    info = wsapi._do_info(FakeHass())
    ws = Mock(
        send_command=AsyncMock(
            side_effect=[
                {"success": True, "result": info},
                {"success": True, "result": {"status": "available", "schema": {}}},
            ]
        )
    )
    monkeypatch.setattr(
        component_api, "get_websocket_client", AsyncMock(return_value=ws)
    )
    monkeypatch.setattr(
        core_contract, "get_websocket_client", AsyncMock(return_value=ws)
    )
    result = await core_contract.core_contract(
        Mock(base_url="http://test", token="test", verify_ssl=True),
        "history/history_during_period",
    )
    assert result["status"] == "available"
    assert ws.send_command.call_args.args[0] == "ha_mcp_tools/core_contract"
