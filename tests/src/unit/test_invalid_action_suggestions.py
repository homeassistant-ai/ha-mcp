"""A rejected device action must only be told to try actions its domain accepts (#3024).

A read-only domain used to answer ``Valid actions for sensor: `` and a toggle
hint; a cover, lock, scene or vacuum was told to use toggle even though its
own table has no on/off vocabulary.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools.action_suggestions import invalid_action_suggestions
from ha_mcp.tools.bulk_selector import BulkSelectorValidationError, _validate_selector
from ha_mcp.tools.device_control import DeviceControlTools


def test_read_only_domain_gets_no_action_list_and_no_toggle_hint():
    suggestions = invalid_action_suggestions("sensor", "sensor.temp", [])

    assert (
        suggestions[0] == "Domain 'sensor' is read-only and accepts no device actions"
    )
    assert all("Valid actions" not in suggestion for suggestion in suggestions)
    assert all("toggle" not in suggestion for suggestion in suggestions)
    assert any("ha_get_state" in suggestion for suggestion in suggestions)


def test_domain_without_on_off_keeps_only_its_own_actions():
    suggestions = invalid_action_suggestions(
        "cover", "cover.blind", ["open", "close", "stop", "toggle", "set"]
    )

    assert suggestions == ["Valid actions for cover: open, close, stop, toggle, set"]


def test_domain_with_on_off_keeps_the_toggle_hint():
    suggestions = invalid_action_suggestions(
        "light", "light.kitchen", ["on", "off", "toggle", "set"]
    )

    assert suggestions[-1] == "Use 'toggle' for simple on/off control"


def _tools() -> DeviceControlTools:
    tools = DeviceControlTools(client=MagicMock())
    tools._ensure_websocket_listener = AsyncMock(return_value=None)
    tools._validate_entity_exists = AsyncMock(
        return_value={"entity_id": "target.entity", "state": "off"}
    )
    tools.client.call_service = AsyncMock(return_value=None)
    return tools


@pytest.mark.asyncio
async def test_control_device_names_a_read_only_target_as_read_only():
    tools = _tools()

    with pytest.raises(ToolError) as exc_info:
        await tools.control_device_smart("sensor.temp", "off")

    error = json.loads(str(exc_info.value))["error"]
    assert error["code"] == "SERVICE_INVALID_ACTION"
    assert error["suggestions"][0] == (
        "Domain 'sensor' is read-only and accepts no device actions"
    )
    assert all("Valid actions for sensor" not in s for s in error["suggestions"])


@pytest.mark.asyncio
async def test_control_device_cover_rejection_drops_the_toggle_hint():
    tools = _tools()

    with pytest.raises(ToolError) as exc_info:
        await tools.control_device_smart("cover.blind", "off")

    payload = json.dumps(json.loads(str(exc_info.value)))
    assert "Valid actions for cover: open, close, stop, toggle, set" in payload
    assert "for simple on/off control" not in payload


def test_selector_rejection_calls_a_read_only_domain_read_only():
    with pytest.raises(BulkSelectorValidationError) as exc_info:
        _validate_selector({"domain": "sensor", "area_ids": ["kitchen"]}, "off")

    assert "read-only and accepts no device actions" in str(exc_info.value)
