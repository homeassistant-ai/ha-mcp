"""Unit tests for the event-listener count lookup."""

from unittest.mock import AsyncMock

import pytest

from ha_mcp.client.event_listeners import get_event_listeners
from ha_mcp.client.rest_client import HomeAssistantAPIError


class TestGetEventListeners:
    @pytest.mark.asyncio
    async def test_returns_counts_keyed_by_event_type(self, mock_client):
        mock_client._request = AsyncMock(
            return_value=[
                {"event": "my_event", "listener_count": 2},
                {"event": "*", "listener_count": 5},
            ]
        )

        counts = await get_event_listeners(mock_client)

        assert counts == {"my_event": 2, "*": 5}
        mock_client._request.assert_called_once_with("GET", "/events")

    @pytest.mark.asyncio
    async def test_unreadable_reply_reports_unknown_not_zero(self, mock_client):
        """A non-list reply must not be reported as 'no listeners registered'."""
        mock_client._request = AsyncMock(return_value={"unexpected": True})

        assert await get_event_listeners(mock_client) is None

    @pytest.mark.asyncio
    async def test_skips_rows_without_a_string_event_and_integer_count(
        self, mock_client
    ):
        mock_client._request = AsyncMock(
            return_value=[
                {"event": "good", "listener_count": 1},
                {"event": 7, "listener_count": 2},
                {"event": "bad_count", "listener_count": "3"},
                "not-a-row",
            ]
        )

        assert await get_event_listeners(mock_client) == {"good": 1}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_count", [True, False, -1])
    async def test_rejects_counts_that_cannot_be_listener_counts(
        self, mock_client, bad_count
    ):
        """Booleans are ints and negatives are impossible; neither is a count."""
        mock_client._request = AsyncMock(
            return_value=[
                {"event": "bad", "listener_count": bad_count},
                {"event": "good", "listener_count": 0},
            ]
        )

        assert await get_event_listeners(mock_client) == {"good": 0}

    @pytest.mark.asyncio
    async def test_all_unreadable_rows_report_unknown_not_zero(self, mock_client):
        """A non-empty reply with no valid row is unknown, not zero listeners."""
        mock_client._request = AsyncMock(
            return_value=[
                {"event": 7, "listener_count": 2},
                {"event": "bad_count", "listener_count": "3"},
                "not-a-row",
            ]
        )

        assert await get_event_listeners(mock_client) is None

    @pytest.mark.asyncio
    async def test_propagates_lookup_failure_to_the_caller(self, mock_client):
        mock_client._request = AsyncMock(side_effect=HomeAssistantAPIError("boom"))

        with pytest.raises(HomeAssistantAPIError):
            await get_event_listeners(mock_client)
