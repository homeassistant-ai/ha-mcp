"""Unit tests for statistic_types parameter validation in _fetch_statistics."""

from datetime import UTC
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_history import _fetch_statistics


def _make_ws_client(result: dict | None = None) -> MagicMock:
    """Create a mock WebSocket client for _fetch_statistics."""
    ws = MagicMock()
    ws.send_command = AsyncMock(
        return_value={"success": True, "result": result or {}}
    )
    return ws


def _make_client() -> MagicMock:
    client = MagicMock()
    client.base_url = "http://localhost:8123"
    client.token = "test-token"
    return client


def _make_dt(days_ago: int = 7):
    from datetime import datetime, timedelta
    return datetime.now(UTC) - timedelta(days=days_ago)


class TestStatisticTypesValidation:
    """Tests for statistic_types parameter edge cases in _fetch_statistics."""

    @pytest.mark.asyncio
    async def test_empty_list_raises_tool_error(self):
        """statistic_types=[] must raise ToolError, not silently use HA defaults.

        Verified live: types=[] causes HA to return rows with only start/end keys,
        discarding all value fields (sum, mean, state, etc.). This is never useful.
        """
        ws = _make_ws_client()
        client = _make_client()
        start = _make_dt(7)
        end = _make_dt(0)

        with pytest.raises(ToolError):
            await _fetch_statistics(
                ws_client=ws,
                client=client,
                entity_id_list=["sensor.test"],
                start_dt=start,
                end_dt=end,
                period="day",
                statistic_types=[],
                limit=None,
                offset=None,
            )

    @pytest.mark.asyncio
    async def test_none_does_not_set_types_in_command(self):
        """statistic_types=None must not include 'types' in the WS command (HA returns all)."""
        ws = _make_ws_client()
        client = _make_client()
        start = _make_dt(7)
        end = _make_dt(0)

        await _fetch_statistics(
            ws_client=ws,
            client=client,
            entity_id_list=["sensor.test"],
            start_dt=start,
            end_dt=end,
            period="day",
            statistic_types=None,
            limit=None,
            offset=None,
        )

        call_kwargs = ws.send_command.call_args[1]
        assert "types" not in call_kwargs, (
            "statistic_types=None must not send 'types' to HA — "
            "omitting 'types' lets HA return all available types."
        )

    @pytest.mark.asyncio
    async def test_valid_list_sets_types_in_command(self):
        """statistic_types=['mean', 'sum'] must include types in the WS command."""
        ws = _make_ws_client()
        client = _make_client()
        start = _make_dt(7)
        end = _make_dt(0)

        await _fetch_statistics(
            ws_client=ws,
            client=client,
            entity_id_list=["sensor.test"],
            start_dt=start,
            end_dt=end,
            period="day",
            statistic_types=["mean", "sum"],
            limit=None,
            offset=None,
        )

        call_kwargs = ws.send_command.call_args[1]
        assert "types" in call_kwargs
        assert set(call_kwargs["types"]) == {"mean", "sum"}

    @pytest.mark.asyncio
    async def test_empty_string_list_notation_raises(self):
        """statistic_types='[]' (string) must also raise ToolError."""
        ws = _make_ws_client()
        client = _make_client()
        start = _make_dt(7)
        end = _make_dt(0)

        with pytest.raises(ToolError):
            await _fetch_statistics(
                ws_client=ws,
                client=client,
                entity_id_list=["sensor.test"],
                start_dt=start,
                end_dt=end,
                period="day",
                statistic_types="[]",
                limit=None,
                offset=None,
            )

    @pytest.mark.asyncio
    async def test_invalid_type_raises(self):
        """Invalid type name must raise ToolError."""
        ws = _make_ws_client()
        client = _make_client()
        start = _make_dt(7)
        end = _make_dt(0)

        with pytest.raises(ToolError):
            await _fetch_statistics(
                ws_client=ws,
                client=client,
                entity_id_list=["sensor.test"],
                start_dt=start,
                end_dt=end,
                period="day",
                statistic_types=["invalid_type"],
                limit=None,
                offset=None,
            )
