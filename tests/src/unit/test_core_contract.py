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
    assert validate_request(hass, "energy/save_prefs", {"future_required": []})["valid"] is True
    assert seen[-1]["type"] == "energy/save_prefs"
    handler.assert_not_called()


@pytest.mark.parametrize("payload", [{"id": 27}, {"type": "call_service"}])
def test_contract_validation_cannot_override_transport(payload: dict[str, Any]) -> None:
    from custom_components.ha_mcp_tools.core_contract import validate_request

    with pytest.raises(ValueError, match="reserved"):
        validate_request(FakeHass(), "energy/save_prefs", payload)
