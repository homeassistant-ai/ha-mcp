"""Bounded, passive Home Assistant event capture tools."""

import asyncio
import json
from typing import Annotated, Any

from pydantic import Field

from ha_mcp._vendor.fastmcp.tools import tool

from ..client.rest_client import HomeAssistantClient
from ..client.websocket_client import get_websocket_client
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    register_tool_methods,
)
from .tool_hints import read_only_hints

MAX_CAPTURED_EVENT_BYTES = 1024 * 1024


def _serialized_size(value: Any) -> int:
    """Return the compact UTF-8 JSON size of a capture response value."""
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


def _limit_capture_response(result: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    """Drop trailing events until the complete serialized result fits."""
    events = result["events"]
    while events and _serialized_size(result) > max_bytes:
        events.pop()
        result["events_omitted_due_to_size"] += 1
        result["stop_reason"] = "response_size_limit"
    return result


def _append_event_within_budget(
    event: dict[str, Any], events: list[dict[str, Any]], captured_bytes: int
) -> tuple[int, bool]:
    """Append an event only when the aggregate event payload budget allows it."""
    event_size = _serialized_size(event)
    new_size = captured_bytes + event_size
    if new_size > MAX_CAPTURED_EVENT_BYTES:
        return captured_bytes, False
    events.append(event)
    return new_size, True


class EventTools:
    """Read-only tools for observing Home Assistant event streams."""

    def __init__(self, client: HomeAssistantClient) -> None:
        self._client = client

    @tool(
        name="ha_capture_events",
        tags={"System"},
        annotations=read_only_hints("Capture Events", open_world=False),
    )
    @log_tool_usage
    async def ha_capture_events(
        self,
        duration_seconds: Annotated[
            int,
            Field(
                ge=1,
                le=120,
                description="Stop after this many seconds even if no event arrives.",
            ),
        ] = 30,
        max_events: Annotated[
            int,
            Field(
                ge=1,
                le=100,
                description=(
                    "Maximum number of event frames to inspect, including frames "
                    "excluded by the optional data filter."
                ),
            ),
        ] = 20,
        event_data_filter: Annotated[
            dict[str, Any] | None,
            Field(
                description=(
                    "Optional exact-match filter on top-level keys in event.data. "
                    "Every supplied key and value must match; non-matching frames "
                    "still count toward max_events."
                ),
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Capture only ZHA events for short-term button-event inspection.

        Do not use this tool to publish events; use ``ha_call_event`` instead.
        Use it to inspect events without controlling entities or persisting
        payloads. The event type is fixed to ``zha_event`` to prevent reading
        unrelated event streams; the serialized response is capped at 1 MiB,
        and the subscription is released on completion or cancellation.
        """
        event_type = "zha_event"

        try:
            websocket = await get_websocket_client(
                url=self._client.base_url,
                token=self._client.token,
                verify_ssl=getattr(self._client, "verify_ssl", None),
            )
            subscription_id, queue = await websocket.subscribe_command(
                "subscribe_events",
                event_type=event_type,
                queue_maxsize=1,
            )
        except Exception as exc:  # noqa: BLE001
            exception_to_structured_error(
                exc,
                context={
                    "operation": "capture Home Assistant events",
                    "event_type": event_type,
                },
                suggestions=[
                    "Verify the Home Assistant WebSocket connection",
                    "Check that the event type is valid",
                ],
            )

        loop = asyncio.get_running_loop()
        started_at = loop.time()
        deadline = started_at + duration_seconds
        events: list[dict[str, Any]] = []
        captured_event_bytes = 0
        events_seen = 0
        events_omitted_due_to_size = 0
        stop_reason = "time_limit"

        try:
            while events_seen < max_events:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    stop_reason = "time_limit"
                    break
                try:
                    frame = await asyncio.wait_for(queue.get(), timeout=remaining)
                except TimeoutError:
                    stop_reason = "time_limit"
                    break
                except asyncio.QueueShutDown:
                    stop_reason = "subscription_closed"
                    break

                events_seen += 1
                event = frame.get("event")
                if not isinstance(event, dict) or event.get("event_type") != event_type:
                    continue
                data = event.get("data")
                if event_data_filter and not (
                    isinstance(data, dict)
                    and all(
                        key in data and data[key] == expected
                        for key, expected in event_data_filter.items()
                    )
                ):
                    continue
                captured_event_bytes, event_fits = _append_event_within_budget(
                    event, events, captured_event_bytes
                )
                if not event_fits:
                    events_omitted_due_to_size += 1
                    stop_reason = "response_size_limit"
                    break

            if events_seen >= max_events and stop_reason != "response_size_limit":
                stop_reason = "event_limit"

            dropped_count = getattr(queue, "dropped_count", 0)
            result = {
                "success": True,
                "event_type": event_type,
                "events": events,
                "events_seen": events_seen,
                "events_dropped": dropped_count,
                "events_omitted_due_to_size": events_omitted_due_to_size,
                "stop_reason": stop_reason,
                "elapsed_seconds": round(loop.time() - started_at, 3),
            }
            return _limit_capture_response(result, MAX_CAPTURED_EVENT_BYTES)
        finally:
            await websocket.release_subscription(subscription_id)


def register_events_tools(mcp: Any, client: HomeAssistantClient, **kwargs: Any) -> None:
    """Register passive event-observation tools."""
    register_tool_methods(mcp, EventTools(client))
