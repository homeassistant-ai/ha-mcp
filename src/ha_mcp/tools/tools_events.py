"""Bounded, passive Home Assistant event capture tools."""

import asyncio
import json
from typing import Annotated, Any

from pydantic import Field

from ha_mcp._vendor.fastmcp.tools import tool

from ..client.rest_client import HomeAssistantClient
from ..client.websocket_client import get_websocket_client
from ..errors import create_validation_error
from ..policy.decisions import APPROVAL_RESPONSE_EVENT
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)
from .tool_hints import read_only_hints

MAX_CAPTURED_EVENT_BYTES = 1024 * 1024


def _validate_event_type(event_type: str) -> str:
    """Normalize a capture event type and refuse the secret-bearing approval event."""
    event_type = event_type.strip()
    if not event_type:
        raise_tool_error(
            create_validation_error(
                "event_type cannot be empty or whitespace",
                parameter="event_type",
            )
        )
    if event_type.casefold() == APPROVAL_RESPONSE_EVENT.casefold():
        raise_tool_error(
            create_validation_error(
                "This event type cannot be captured because it contains a "
                "sensitive approval PIN",
                parameter="event_type",
            )
        )
    return event_type


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
        event_type: Annotated[
            str,
            Field(
                description=(
                    "Home Assistant event type, e.g. 'zha_event'. Sensitive "
                    "approval-response events are blocked."
                )
            ),
        ],
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
        """Capture one Home Assistant event stream for short-term inspection.

        Do not use this tool to publish events; use ``ha_call_event`` instead.
        Use it to inspect events without controlling entities or persisting
        payloads. Sensitive approval-response events are blocked, returned data
        is capped at 1 MiB, and the subscription is released on completion or
        cancellation.
        """
        event_type = _validate_event_type(event_type)

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
                if not isinstance(event, dict):
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
                serialized_event = json.dumps(
                    event, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
                if (
                    captured_event_bytes + len(serialized_event)
                    > MAX_CAPTURED_EVENT_BYTES
                ):
                    events_omitted_due_to_size += 1
                    stop_reason = "response_size_limit"
                    break
                captured_event_bytes += len(serialized_event)
                events.append(event)

            if events_seen >= max_events and stop_reason != "response_size_limit":
                stop_reason = "event_limit"

            dropped_count = getattr(queue, "dropped_count", 0)
            return {
                "success": True,
                "event_type": event_type,
                "events": events,
                "events_seen": events_seen,
                "events_dropped": dropped_count,
                "events_omitted_due_to_size": events_omitted_due_to_size,
                "stop_reason": stop_reason,
                "elapsed_seconds": round(loop.time() - started_at, 3),
            }
        finally:
            await websocket.release_subscription(subscription_id)


def register_events_tools(mcp: Any, client: HomeAssistantClient, **kwargs: Any) -> None:
    """Register passive event-observation tools."""
    register_tool_methods(mcp, EventTools(client))
