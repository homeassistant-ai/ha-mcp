"""``add_timezone_metadata`` returns a payload's ``warnings`` beside ``data``.

A tool's ``warnings`` is a top-level list, never nested in a payload key, so
the wrapper lifts a ``warnings`` list out of the payload it wraps.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp.tools.response_helpers import add_timezone_metadata


@pytest.fixture
def tz_client():
    client = MagicMock()
    client.get_config = AsyncMock(return_value={"time_zone": "America/New_York"})
    return client


@pytest.mark.asyncio
async def test_a_payload_warning_reaches_the_top_level_not_data(tz_client):
    """A warnings list in the payload is returned beside data, not inside it."""
    data = {"success": True, "warnings": ["attribute_keys was ignored"]}
    result = await add_timezone_metadata(tz_client, data)
    assert result["warnings"] == ["attribute_keys was ignored"]
    assert result["data"] == {"success": True}


@pytest.mark.asyncio
async def test_payload_and_timezone_warnings_come_back_as_one_list(tz_client):
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
async def test_a_non_list_warnings_field_stays_payload_data(tz_client):
    """Only a list is lifted; any other warnings value is payload data."""
    result = await add_timezone_metadata(tz_client, {"success": True, "warnings": 3})
    assert result["data"]["warnings"] == 3
    assert "warnings" not in result


@pytest.mark.asyncio
async def test_an_empty_warnings_list_leaves_no_warnings_key(tz_client):
    """An empty payload list is dropped, not returned as ``warnings: []``."""
    result = await add_timezone_metadata(tz_client, {"success": True, "warnings": []})
    assert "warnings" not in result
    assert "warnings" not in result["data"]
