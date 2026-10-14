"""End-to-end tests for bounded Home Assistant event capture."""

import asyncio
import uuid

import pytest

from ..utilities.assertions import assert_mcp_success


@pytest.mark.asyncio
async def test_capture_events_returns_matching_event_and_stops(mcp_client):
    """A bounded capture observes an event without publishing or retaining it."""
    suffix = uuid.uuid4().hex[:8]
    event_type = f"test_mcp_capture_{suffix}"
    marker = f"capture-{suffix}"
    capture = asyncio.create_task(
        mcp_client.call_tool(
            "ha_capture_events",
            {
                "event_type": event_type,
                "duration_seconds": 10,
                "max_events": 1,
                "event_data_filter": {"marker": marker},
            },
        )
    )

    try:
        # The tool call opens the subscription before it can complete. Repeat a
        # harmless custom event until the concurrent capture has observed one.
        for _ in range(20):
            await mcp_client.call_tool(
                "ha_call_event",
                {"event_type": event_type, "data": {"marker": marker}},
            )
            if capture.done():
                break
            await asyncio.sleep(0.05)

        result = assert_mcp_success(
            await asyncio.wait_for(capture, timeout=5), "capture custom event"
        )
        assert result["success"] is True
        assert result["event_type"] == event_type
        assert result["events_seen"] == 1
        assert result["stop_reason"] == "event_limit"
        assert result["events"][0]["data"]["marker"] == marker
    finally:
        if not capture.done():
            capture.cancel()
            with pytest.raises(asyncio.CancelledError):
                await capture
