"""``ha_call_event``: fire custom events on the Home Assistant event bus.

The method moved out of ``tools_service.py`` (already over the module-size
ratchet line) into this mixin so that module shrinks rather than grows.
``ServiceTools`` composes it alongside its other responsibilities.
"""

import asyncio
import logging
from typing import Annotated, Any

import httpx
from pydantic import Field

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool

from ..client.event_listeners import get_event_listeners
from ..client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
    HomeAssistantError,
)
from ..errors import create_validation_error
from .coercion import JSON_STRING_COERCION
from .event_listener_fields import event_listener_fields
from .helpers import (
    _parse_json_dict_param,
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
)
from .tool_hints import write_hints

logger = logging.getLogger(__name__)

# Characters that would break, truncate, or decode the
# ``POST /api/events/{event_type}`` path.
_EVENT_TYPE_SEPARATORS = ("/", "\\", "?", "#", "%")

# Upper bound for the optional listener-count lookup, including its retries: a
# stalled GET /api/events must not hold back the fire.
EVENT_LISTENER_LOOKUP_BUDGET_SECONDS = 5.0


def _parse_event_data(data: str | dict[str, Any] | None) -> dict[str, Any] | None:
    return _parse_json_dict_param(
        data, type_error_message="Event data must be a JSON object (dict)"
    )


class EventCallMixin:
    """The ``ha_call_event`` tool method."""

    # Set by ``ServiceTools.__init__``; declared here so the mixin type-checks
    # in isolation (same pattern as smart_search._base._SearchBase).
    _client: HomeAssistantClient

    @tool(
        name="ha_call_event",
        tags={"Service & Device Control"},
        annotations=write_hints(
            "Call Event", destructive=True, idempotent=False, open_world=False
        ),
    )
    @log_tool_usage
    async def ha_call_event(
        self,
        event_type: Annotated[
            str,
            Field(
                description="Event type to fire, e.g. 'my_custom_event'.",
            ),
        ],
        data: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Optional event payload, delivered to subscribers as the "
                    "event's data (trigger.event.data in an automation)."
                ),
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Execute a custom event on the Home Assistant event bus.

        When NOT to use: for controlling entities (lights, switches, climate) — use
        ha_call_service instead. To run an automation now, use
        ha_config_set_automation(identifier=..., run_actions=True).

        Use this to publish custom event types consumed by event-triggered automations,
        Node-RED flows, or custom integrations that subscribe to specific event types.

        Caveats: Events are fire-and-forget; a dispatch confirms the bus accepted
        the event and reports how many listeners were registered for it, not that
        any of them acted. listener_count covers the exact event type;
        wildcard_listener_count covers match-all listeners. Both counts are
        returned only when the optional listener lookup succeeds.
        """
        # Validate event_type before hitting the wire — empty strings, path
        # separators, or URL delimiters produce malformed URLs at
        # POST /api/events/{event_type}.
        if not event_type or not event_type.strip():
            raise_tool_error(
                create_validation_error(
                    "event_type cannot be empty or whitespace",
                    parameter="event_type",
                )
            )
        if any(separator in event_type for separator in _EVENT_TYPE_SEPARATORS):
            raise_tool_error(
                create_validation_error(
                    "event_type cannot contain path separators or URL delimiters",
                    parameter="event_type",
                    details=f"Received: {event_type!r}",
                )
            )

        parsed_data = _parse_event_data(data)

        listener_counts: dict[str, int] | None = None
        try:
            async with asyncio.timeout(EVENT_LISTENER_LOOKUP_BUDGET_SECONDS):
                listener_counts = await get_event_listeners(self._client, event_type)
        except (HomeAssistantError, TimeoutError) as err:
            # Counts are optional enrichment; never block the fire on them.
            logger.debug("Event listener lookup failed: %s", err)

        listener_fields = event_listener_fields(listener_counts, event_type)

        try:
            response = await self._client.fire_event(event_type, parsed_data)
        except HomeAssistantConnectionError as error:
            if isinstance(error.__cause__, httpx.ReadTimeout):
                warnings = list(listener_fields.get("warnings", ()))
                warnings.append(
                    "Response timed out. The event was dispatched and may still "
                    "have been delivered to subscribers."
                )
                return {
                    "success": True,
                    "partial": True,
                    "event_type": event_type,
                    "message": (
                        f"Event {event_type} was dispatched but Home Assistant "
                        "did not respond within the timeout period."
                    ),
                    **{
                        key: value
                        for key, value in listener_fields.items()
                        if key != "warnings"
                    },
                    "warnings": warnings,
                }
            exception_to_structured_error(
                error,
                context={"event_type": event_type},
                suggestions=["Check Home Assistant connection"],
            )
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            exception_to_structured_error(
                e,
                context={"event_type": event_type},
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify event_type is a valid identifier",
                ],
            )

        return {
            "success": True,
            "event_type": event_type,
            "message": response.get("message", f"Event {event_type} fired."),
            **listener_fields,
        }
