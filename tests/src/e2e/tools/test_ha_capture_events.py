"""End-to-end tests for bounded Home Assistant event capture."""

import asyncio
import uuid

import pytest

from ha_mcp._vendor.fastmcp import Client

from ..utilities.assertions import MCPAssertions


@pytest.mark.asyncio
async def test_capture_events_returns_matching_event_and_stops(
    mcp_client: Client,
) -> None:
    """A bounded capture observes a ZHA event without retaining it."""
    suffix = uuid.uuid4().hex[:8]
    marker = f"capture-{suffix}"
    async with MCPAssertions(mcp_client) as mcp:
        capture = asyncio.create_task(
            mcp.call_tool_success(
                "ha_capture_events",
                {
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
                    {"event_type": "zha_event", "data": {"marker": marker}},
                )
                await asyncio.sleep(0.05)

            result = await asyncio.wait_for(
                capture, timeout=max(0.1, deadline - loop.time())
            )
            assert result["success"] is True
            assert result["event_type"] == "zha_event"
            assert result["events_seen"] == 1
            assert result["stop_reason"] == "event_limit"
            assert result["events"][0]["data"]["marker"] == marker
        finally:
            if not capture.done():
                capture.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await capture
