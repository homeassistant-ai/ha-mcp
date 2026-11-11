"""``add_timezone_metadata`` returns a payload's ``warnings`` beside ``data``.

A tool's ``warnings`` is a top-level list, never nested in a payload key, so
the wrapper lifts a ``warnings`` list out of the payload it wraps.
"""

from unittest.mock import AsyncMock

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.tools.response_helpers import add_timezone_metadata
from ha_mcp.tools.tools_history import HistoryTools


@pytest.mark.asyncio
async def test_a_payload_warning_reaches_the_top_level_not_data(
    core_client: HomeAssistantClient,
) -> None:
    """A warnings list in the payload is returned beside data, not inside it."""
    data = {"success": True, "warnings": ["attribute_keys was ignored"]}
    result = await add_timezone_metadata(core_client, data)
    assert result["warnings"] == ["attribute_keys was ignored"]
    assert result["data"] == {"success": True}


@pytest.mark.asyncio
async def test_payload_and_timezone_warnings_come_back_as_one_list(
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Payload warnings and the wrapper's own fetch warning share one list.

    Without conversion the wrapper hands the caller's own dict through as
    ``data``, so this path also shows the lift leaves that dict untouched.
    """
    monkeypatch.setattr(
        core_client, "get_config", AsyncMock(side_effect=OSError("connection refused"))
    )
    data = {"success": True, "warnings": ["payload note"]}
    result = await add_timezone_metadata(core_client, data, convert_timestamps=False)
    payload_note, tz_warning = result["warnings"]
    assert payload_note == "payload note"
    assert "timezone" in tz_warning
    assert data == {"success": True, "warnings": ["payload note"]}


@pytest.mark.asyncio
async def test_a_non_list_warnings_field_stays_payload_data(
    core_client: HomeAssistantClient,
) -> None:
    """Only a list is lifted; any other warnings value is payload data."""
    result = await add_timezone_metadata(core_client, {"success": True, "warnings": 3})
    assert result["data"]["warnings"] == 3
    assert "warnings" not in result


@pytest.mark.asyncio
async def test_an_empty_warnings_list_leaves_no_warnings_key(
    core_client: HomeAssistantClient,
) -> None:
    """An empty payload list is dropped, not returned as ``warnings: []``."""
    result = await add_timezone_metadata(core_client, {"success": True, "warnings": []})
    assert "warnings" not in result
    assert "warnings" not in result["data"]


@pytest.mark.asyncio
async def test_history_warnings_are_one_top_level_list_in_order(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ha_get_history`` returns payload, timezone and ``fields`` warnings as
    one top-level list, in the order they arise, with none left in ``data``."""
    assert await async_setup_component(hass, "history", {})
    monkeypatch.setattr(
        core_client,
        "get_config",
        AsyncMock(side_effect=HomeAssistantConnectionError("down")),
    )
    result = await HistoryTools(core_client).ha_get_history(
        entity_ids="sensor.temp", summary=True, fields=["nonexistent"]
    )
    assert "warnings" not in result["data"]
    payload, timezone, typo = result["warnings"]
    assert payload.startswith("No recorded states for: sensor.temp")
    assert "timezone" in timezone
    assert "nonexistent" in typo
