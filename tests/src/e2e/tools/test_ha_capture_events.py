"""End-to-end tests for bounded Home Assistant event capture."""

import asyncio
import uuid

import pytest

from ..utilities.assertions import MCPAssertions


@pytest.mark.asyncio
async def test_capture_events_returns_matching_event_and_stops(mcp_client):
    """A bounded capture observes an event without publishing or retaining it."""
    suffix = uuid.uuid4().hex[:8]
    event_type = f"test_mcp_capture_{suffix}"
    marker = f"capture-{suffix}"
    async with MCPAssertions(mcp_client) as mcp:
        capture = asyncio.create_task(
            mcp.call_tool_success(
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
            # Keep publishing until the subscription is ready or this bounded
            # test deadline expires; setup latency must not lose the only event.
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 15
            while not capture.done() and loop.time() < deadline:
                await mcp.call_tool_success(
                    "ha_call_event",
                    {"event_type": event_type, "data": {"marker": marker}},
                )
                await asyncio.sleep(0.05)

            result = await asyncio.wait_for(
                capture, timeout=max(0.1, deadline - loop.time())
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


@pytest.mark.asyncio
async def test_capture_events_rejects_sensitive_approval_event(mcp_client):
    """The event-capture API never exposes the approval PIN event."""
    async with MCPAssertions(mcp_client) as mcp:
        await mcp.call_tool_failure(
            "ha_capture_events",
            {
                "event_type": "ha_mcp_approval_response",
                "duration_seconds": 1,
                "max_events": 1,
            },
            expected_error="sensitive approval PIN",
        )
