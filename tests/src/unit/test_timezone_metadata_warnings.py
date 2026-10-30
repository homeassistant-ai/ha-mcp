"""``add_timezone_metadata`` returns a payload's ``warnings`` beside ``data``.

A tool's ``warnings`` is a top-level list, never nested in a payload key, so
the wrapper lifts a ``warnings`` list out of the payload it wraps.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp.client.rest_client import HomeAssistantConnectionError
from ha_mcp.tools.response_helpers import add_timezone_metadata
from ha_mcp.tools.tools_history import HistoryTools

from .test_tools_history import _HISTORY_INNER, _no_real_caps_probe  # noqa: F401


@pytest.fixture
def tz_client() -> MagicMock:
    client = MagicMock()
    client.get_config = AsyncMock(return_value={"time_zone": "America/New_York"})
    return client


@pytest.mark.asyncio
async def test_a_payload_warning_reaches_the_top_level_not_data(
    tz_client: MagicMock,
) -> None:
    """A warnings list in the payload is returned beside data, not inside it."""
    data = {"success": True, "warnings": ["attribute_keys was ignored"]}
    result = await add_timezone_metadata(tz_client, data)
    assert result["warnings"] == ["attribute_keys was ignored"]
    assert result["data"] == {"success": True}


@pytest.mark.asyncio
async def test_payload_and_timezone_warnings_come_back_as_one_list(
    tz_client: MagicMock,
) -> None:
    """Payload warnings and the wrapper's own fetch warning share one list.

    Without conversion the wrapper hands the caller's own dict through as
    ``data``, so this path also shows the lift leaves that dict untouched.
    """
    tz_client.get_config.side_effect = OSError("connection refused")
    data = {"success": True, "warnings": ["payload note"]}
    result = await add_timezone_metadata(tz_client, data, convert_timestamps=False)
    payload_note, tz_warning = result["warnings"]
    assert payload_note == "payload note"
    assert "timezone" in tz_warning
    assert data == {"success": True, "warnings": ["payload note"]}


@pytest.mark.asyncio
async def test_a_non_list_warnings_field_stays_payload_data(
    tz_client: MagicMock,
) -> None:
    """Only a list is lifted; any other warnings value is payload data."""
    result = await add_timezone_metadata(tz_client, {"success": True, "warnings": 3})
    assert result["data"]["warnings"] == 3
    assert "warnings" not in result


@pytest.mark.asyncio
async def test_an_empty_warnings_list_leaves_no_warnings_key(
    tz_client: MagicMock,
) -> None:
    """An empty payload list is dropped, not returned as ``warnings: []``."""
    result = await add_timezone_metadata(tz_client, {"success": True, "warnings": []})
    assert "warnings" not in result
    assert "warnings" not in result["data"]


@pytest.mark.asyncio
async def test_history_warnings_are_one_top_level_list_in_order(
    tz_client: MagicMock,
) -> None:
    """``ha_get_history`` returns payload, timezone and ``fields`` warnings as
    one top-level list, in the order they arise, with none left in ``data``."""
    tz_client.get_config.side_effect = HomeAssistantConnectionError("down")
    inner = {**_HISTORY_INNER, "warnings": ["payload warning"]}
    with patch(
        "ha_mcp.tools.tools_history._fetch_history",
        new_callable=AsyncMock,
        return_value=inner,
    ):
        result = await HistoryTools(tz_client).ha_get_history(
            entity_ids="sensor.temp", fields=["nonexistent"]
        )
    assert "warnings" not in result["data"]
    payload, timezone, typo = result["warnings"]
    assert payload == "payload warning"
    assert "timezone" in timezone
    assert "nonexistent" in typo
