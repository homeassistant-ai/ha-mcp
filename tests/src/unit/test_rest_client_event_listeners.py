"""Unit tests for the event-listener count lookup, against real Core's ``/api/events``."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant, callback

from ha_mcp.client.event_listeners import get_event_listeners
from ha_mcp.client.rest_client import HomeAssistantAPIError, HomeAssistantClient

EVENTS_VIEW_ROWS = "homeassistant.components.api.async_events_json"


@callback
def _noop(_event: Any) -> None:
    """Listener body; only its registration matters."""


class TestGetEventListeners:
    async def test_returns_counts_keyed_by_event_type(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        for _ in range(2):
            hass.bus.async_listen("my_event", _noop)
        for _ in range(5):
            hass.bus.async_listen("*", _noop)

        counts = await get_event_listeners(core_client, "my_event")

        assert counts is not None
        assert counts["my_event"] == 2
        assert counts["*"] == 5

    async def test_unreadable_reply_reports_unknown_not_zero(
        self, core_client: HomeAssistantClient
    ) -> None:
        """A non-list reply must not be reported as 'no listeners registered'."""
        with patch(EVENTS_VIEW_ROWS, return_value={"unexpected": True}):
            assert await get_event_listeners(core_client, "my_event") is None

    async def test_skips_rows_without_a_string_event_and_integer_count(
        self, core_client: HomeAssistantClient
    ) -> None:
        rows = [
            {"event": "good", "listener_count": 1},
            {"event": 7, "listener_count": 2},
            {"event": "bad_count", "listener_count": "3"},
            "not-a-row",
        ]
        with patch(EVENTS_VIEW_ROWS, return_value=rows):
            assert await get_event_listeners(core_client, "my_event") == {"good": 1}

    @pytest.mark.parametrize("bad_count", [True, -1])
    async def test_rejects_counts_that_cannot_be_listener_counts(
        self, core_client: HomeAssistantClient, bad_count: Any
    ) -> None:
        """A boolean is an int subclass and negatives are impossible; neither is a count."""
        rows = [
            {"event": "bad", "listener_count": bad_count},
            {"event": "good", "listener_count": 0},
        ]
        with patch(EVENTS_VIEW_ROWS, return_value=rows):
            assert await get_event_listeners(core_client, "my_event") == {"good": 0}

    async def test_all_unreadable_rows_report_unknown_not_zero(
        self, core_client: HomeAssistantClient
    ) -> None:
        """A non-empty reply with no valid row is unknown, not zero listeners."""
        rows = [
            {"event": 7, "listener_count": 2},
            {"event": "bad_count", "listener_count": "3"},
            "not-a-row",
        ]
        with patch(EVENTS_VIEW_ROWS, return_value=rows):
            assert await get_event_listeners(core_client, "my_event") is None

    @pytest.mark.parametrize("unreadable_event", ["my_event", "*"])
    async def test_unreadable_row_for_a_reported_event_reports_unknown(
        self, core_client: HomeAssistantClient, unreadable_event: str
    ) -> None:
        """A row this call reports on must not silently become zero listeners."""
        rows = [
            {"event": "other_event", "listener_count": 4},
            {"event": unreadable_event, "listener_count": "3"},
        ]
        with patch(EVENTS_VIEW_ROWS, return_value=rows):
            assert await get_event_listeners(core_client, "my_event") is None

    async def test_propagates_lookup_failure_to_the_caller(
        self, core_client: HomeAssistantClient
    ) -> None:
        with (
            patch(EVENTS_VIEW_ROWS, side_effect=RuntimeError("boom")),
            pytest.raises(HomeAssistantAPIError),
        ):
            await get_event_listeners(core_client, "my_event")
