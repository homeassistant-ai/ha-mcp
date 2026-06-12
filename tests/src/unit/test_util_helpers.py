"""Unit tests for add_timezone_metadata in util_helpers."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp.tools.util_helpers import add_timezone_metadata


def _make_client(tz: str = "America/Denver") -> MagicMock:
    client = MagicMock()
    client.get_config = AsyncMock(return_value={"time_zone": tz})
    return client


# ---------------------------------------------------------------------------
# include_metadata=False passthrough
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.asyncio
async def test_passthrough_when_metadata_disabled():
    data = {"key": "value"}
    result = await add_timezone_metadata(_make_client(), data, include_metadata=False)
    assert result == data


# ---------------------------------------------------------------------------
# Metadata shape
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.asyncio
async def test_metadata_shape_and_timezone_label():
    result = await add_timezone_metadata(_make_client("America/Denver"), {})
    assert "data" in result
    assert "metadata" in result
    meta = result["metadata"]
    assert meta["home_assistant_timezone"] == "America/Denver"
    assert "America/Denver" in meta["timestamp_format"]
    assert "America/Denver" in meta["note"]
    assert "UTC" not in meta["note"]


# ---------------------------------------------------------------------------
# Config-fetch failure falls back to UTC
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.asyncio
async def test_config_fetch_failure_falls_back_to_utc():
    client = MagicMock()
    client.get_config = AsyncMock(side_effect=OSError("network error"))
    result = await add_timezone_metadata(client, {})
    assert result["metadata"]["home_assistant_timezone"] == "UTC"


# ---------------------------------------------------------------------------
# Timestamp conversion — offset-aware UTC string
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.asyncio
async def test_last_changed_converted_from_utc_to_denver():
    data = {"last_changed": "2026-06-12T00:06:00+00:00"}
    result = await add_timezone_metadata(_make_client("America/Denver"), data)
    converted = result["data"]["last_changed"]
    # 00:06 UTC = 18:06 MDT (UTC-6)
    assert "18:06" in converted
    assert "+00:00" not in converted


@pytest.mark.unit
@pytest.mark.asyncio
async def test_last_updated_and_last_reported_converted():
    data = {
        "last_updated": "2026-06-12T02:00:00+00:00",
        "last_reported": "2026-06-12T03:00:00+00:00",
    }
    result = await add_timezone_metadata(_make_client("America/Denver"), data)
    assert "20:00" in result["data"]["last_updated"]
    assert "21:00" in result["data"]["last_reported"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_logbook_when_field_converted():
    data = {"when": "2026-06-12T01:00:00+00:00"}
    result = await add_timezone_metadata(_make_client("America/Denver"), data)
    assert "19:00" in result["data"]["when"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_last_triggered_field_converted():
    data = {"last_triggered": "2026-06-12T04:00:00+00:00"}
    result = await add_timezone_metadata(_make_client("America/Denver"), data)
    assert "22:00" in result["data"]["last_triggered"]


# ---------------------------------------------------------------------------
# Naive string guard — treated as UTC, not system local
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.asyncio
async def test_naive_string_assumed_utc():
    data = {"last_changed": "2026-06-12T06:00:00"}  # no offset
    result = await add_timezone_metadata(_make_client("America/Denver"), data)
    converted = result["data"]["last_changed"]
    # 06:00 UTC naive → treated as UTC → 00:00 MDT
    assert "00:00" in converted


# ---------------------------------------------------------------------------
# Nested and list structures converted recursively
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.asyncio
async def test_nested_dict_converted():
    data = {"entity": {"last_changed": "2026-06-12T00:00:00+00:00"}}
    result = await add_timezone_metadata(_make_client("America/Denver"), data)
    assert "+00:00" not in result["data"]["entity"]["last_changed"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_of_state_objects_converted():
    data = [
        {"last_changed": "2026-06-12T00:06:00+00:00", "state": "heat"},
        {"last_changed": "2026-06-12T04:01:00+00:00", "state": "off"},
    ]
    result = await add_timezone_metadata(_make_client("America/Denver"), data)
    states = result["data"]
    assert "18:06" in states[0]["last_changed"]
    assert "22:01" in states[1]["last_changed"]


# ---------------------------------------------------------------------------
# Non-timestamp fields are not modified
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_timestamp_fields_untouched():
    data = {"state": "on", "entity_id": "light.kitchen", "friendly_name": "Kitchen"}
    result = await add_timezone_metadata(_make_client("America/Denver"), data)
    assert result["data"] == data


# ---------------------------------------------------------------------------
# Malformed timestamp strings don't raise
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.asyncio
async def test_malformed_timestamp_passed_through():
    data = {"last_changed": "not-a-date"}
    result = await add_timezone_metadata(_make_client("America/Denver"), data)
    assert result["data"]["last_changed"] == "not-a-date"


# ---------------------------------------------------------------------------
# DST boundary — winter vs summer offset
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.asyncio
async def test_dst_summer_offset():
    # June: MDT = UTC-6
    data = {"last_changed": "2026-06-12T12:00:00+00:00"}
    result = await add_timezone_metadata(_make_client("America/Denver"), data)
    assert "06:00" in result["data"]["last_changed"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dst_winter_offset():
    # January: MST = UTC-7
    data = {"last_changed": "2026-01-15T12:00:00+00:00"}
    result = await add_timezone_metadata(_make_client("America/Denver"), data)
    assert "05:00" in result["data"]["last_changed"]
