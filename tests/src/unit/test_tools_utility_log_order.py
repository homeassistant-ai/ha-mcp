"""Unit tests for the ``order`` parameter of ``ha_get_logs`` across sources.

Covers newest-first (default) vs oldest-first ordering for every time-ordered
source, plus the warning emitted when ``order`` is supplied to the non
time-ordered ``logger`` source. The logbook windowing mirrors the traces
newest-first fix (#1178).
"""

from unittest.mock import AsyncMock

import pytest

from ha_mcp.tools.tools_utility import UtilityTools


def _call_kwargs(**overrides):
    """Full keyword set for ``UtilityTools.get_logs`` with sensible defaults."""
    base = {
        "source": "logbook",
        "limit": None,
        "search": None,
        "hours_back": 1,
        "entity_id": None,
        "end_time": None,
        "offset": 0,
        "compact": True,
        "level": None,
        "slug": None,
    }
    base.update(overrides)
    return base


class TestLogbookOrder:
    """source='logbook' — offset pagination over a reversible window."""

    @staticmethod
    def _client(entries):
        client = AsyncMock()
        client.get_logbook = AsyncMock(return_value=entries)
        client.get_config = AsyncMock(return_value={"time_zone": "UTC"})
        return client

    @staticmethod
    def _entries():
        # Oldest-first, exactly as HA's /logbook REST endpoint returns them.
        return [
            {
                "when": f"2026-06-16T00:0{i}:00+00:00",
                "entity_id": "light.x",
                "state": f"s{i}",  # s0 oldest ... s4 newest
            }
            for i in range(5)
        ]

    @pytest.mark.asyncio
    async def test_newest_is_default_and_reverses_window(self):
        tools = UtilityTools(self._client(self._entries()))
        result = await tools.get_logs(**_call_kwargs(source="logbook", limit=2))
        data = result["data"]
        assert data["order"] == "newest"
        assert [e["state"] for e in data["entries"]] == ["s4", "s3"]
        assert data["offset"] == 0
        assert data["total_entries"] == 5
        assert data["has_more"] is True

    @pytest.mark.asyncio
    async def test_oldest_returns_chronological_window(self):
        tools = UtilityTools(self._client(self._entries()))
        result = await tools.get_logs(
            **_call_kwargs(source="logbook", limit=2, order="oldest")
        )
        data = result["data"]
        assert data["order"] == "oldest"
        assert [e["state"] for e in data["entries"]] == ["s0", "s1"]
        assert data["has_more"] is True

    @pytest.mark.asyncio
    async def test_newest_offset_pages_into_older_entries(self):
        tools = UtilityTools(self._client(self._entries()))
        result = await tools.get_logs(
            **_call_kwargs(source="logbook", limit=2, offset=2)
        )
        data = result["data"]
        # total=5, offset=2 -> end=3, start=1, response[1:3]=[s1,s2] reversed.
        assert [e["state"] for e in data["entries"]] == ["s2", "s1"]
        assert data["has_more"] is True  # offset(2) + len(2) = 4 < 5

    @pytest.mark.asyncio
    async def test_oldest_pagination_hint_carries_order(self):
        tools = UtilityTools(self._client(self._entries()))
        result = await tools.get_logs(
            **_call_kwargs(source="logbook", limit=2, order="oldest")
        )
        assert "order=oldest" in result["data"]["pagination_hint"]

    @pytest.mark.asyncio
    async def test_newest_default_hint_omits_order(self):
        tools = UtilityTools(self._client(self._entries()))
        result = await tools.get_logs(**_call_kwargs(source="logbook", limit=2))
        assert "order=" not in result["data"]["pagination_hint"]


class TestSystemOrder:
    """source='system' — sorted deterministically by entry timestamp."""

    @staticmethod
    def _client():
        client = AsyncMock()
        client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": [
                    {
                        "name": "a",
                        "message": ["m"],
                        "level": "ERROR",
                        "timestamp": 100.0,
                    },
                    {
                        "name": "b",
                        "message": ["m"],
                        "level": "ERROR",
                        "timestamp": 300.0,
                    },
                    {
                        "name": "c",
                        "message": ["m"],
                        "level": "ERROR",
                        "timestamp": 200.0,
                    },
                ],
            }
        )
        return client

    @pytest.mark.asyncio
    async def test_newest_sorts_timestamp_descending(self):
        tools = UtilityTools(self._client())
        result = await tools.get_logs(**_call_kwargs(source="system"))
        assert result["order"] == "newest"
        assert [e["timestamp"] for e in result["entries"]] == [300.0, 200.0, 100.0]

    @pytest.mark.asyncio
    async def test_oldest_sorts_timestamp_ascending(self):
        tools = UtilityTools(self._client())
        result = await tools.get_logs(**_call_kwargs(source="system", order="oldest"))
        assert result["order"] == "oldest"
        assert [e["timestamp"] for e in result["entries"]] == [100.0, 200.0, 300.0]


class TestRawTextOrder:
    """error_log / supervisor / system_service — reversible most-recent window."""

    @pytest.mark.asyncio
    async def test_error_log_newest_reverses_recent_window(self):
        client = AsyncMock()
        client.get_error_log = AsyncMock(return_value="l0\nl1\nl2\nl3")
        tools = UtilityTools(client)
        result = await tools.get_logs(**_call_kwargs(source="error_log", limit=2))
        assert result["order"] == "newest"
        assert result["log"] == "l3\nl2"  # newest line first
        assert result["returned_lines"] == 2
        assert result["total_lines"] == 4

    @pytest.mark.asyncio
    async def test_error_log_oldest_keeps_chronological_window(self):
        client = AsyncMock()
        client.get_error_log = AsyncMock(return_value="l0\nl1\nl2\nl3")
        tools = UtilityTools(client)
        result = await tools.get_logs(
            **_call_kwargs(source="error_log", limit=2, order="oldest")
        )
        assert result["order"] == "oldest"
        assert result["log"] == "l2\nl3"  # recent window, ascending

    @pytest.mark.asyncio
    async def test_supervisor_newest_reverses(self):
        client = AsyncMock()
        client.get_addon_logs = AsyncMock(return_value="a\nb\nc\nd")
        tools = UtilityTools(client)
        result = await tools.get_logs(
            **_call_kwargs(source="supervisor", slug="core_x", limit=2)
        )
        assert result["order"] == "newest"
        assert result["log"] == "d\nc"
        assert result["slug"] == "core_x"

    @pytest.mark.asyncio
    async def test_system_service_oldest_keeps_chronological(self):
        client = AsyncMock()
        client._get_system_service_logs = AsyncMock(return_value="a\nb\nc\nd")
        tools = UtilityTools(client)
        result = await tools.get_logs(
            **_call_kwargs(
                source="system_service", slug="supervisor", limit=2, order="oldest"
            )
        )
        assert result["order"] == "oldest"
        assert result["log"] == "c\nd"


class TestLoggerOrderWarning:
    """source='logger' is not time-ordered: 'order' is ignored, with a warning."""

    @staticmethod
    def _client():
        client = AsyncMock()
        client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": [{"domain": "homeassistant", "level": 20}],
            }
        )
        return client

    @pytest.mark.asyncio
    async def test_non_default_order_warns_and_is_ignored(self):
        tools = UtilityTools(self._client())
        result = await tools.get_logs(**_call_kwargs(source="logger", order="oldest"))
        assert "order" not in result  # logger response has no order field
        assert any("order" in w for w in result.get("warnings", []))

    @pytest.mark.asyncio
    async def test_default_order_emits_no_warning(self):
        tools = UtilityTools(self._client())
        result = await tools.get_logs(**_call_kwargs(source="logger"))
        assert "warnings" not in result
