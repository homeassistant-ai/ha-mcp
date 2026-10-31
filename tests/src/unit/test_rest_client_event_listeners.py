"""Unit tests for HomeAssistantClient.get_event_listeners."""

from unittest.mock import AsyncMock

import pytest

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

        counts = await mock_client.get_event_listeners()

        assert counts == {"my_event": 2, "*": 5}
        mock_client._request.assert_called_once_with("GET", "/events")

    @pytest.mark.asyncio
    async def test_returns_empty_mapping_for_unexpected_reply_shape(self, mock_client):
        mock_client._request = AsyncMock(return_value={"unexpected": True})

        assert await mock_client.get_event_listeners() == {}

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

        assert await mock_client.get_event_listeners() == {"good": 1}

    @pytest.mark.asyncio
    async def test_propagates_lookup_failure_to_the_caller(self, mock_client):
        mock_client._request = AsyncMock(side_effect=HomeAssistantAPIError("boom"))

        with pytest.raises(HomeAssistantAPIError):
            await mock_client.get_event_listeners()
