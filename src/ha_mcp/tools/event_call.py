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
    HomeAssistantCommandNotSent,
    HomeAssistantConnectionError,
    HomeAssistantError,
)
from ..errors import ErrorCode, create_error_response, create_validation_error
from .coercion import JSON_STRING_COERCION
from .component_api import (
    component_supports,
    get_component_caps,
    invalidate_caps,
)
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

# The component bridge and Core's own WebSocket command. The REST route is
# retained only for state_changed, whose dict payloads APIEventView rebuilds
# into State objects; the WebSocket routes do not do that conversion.
WS_CALL_EVENT = "ha_mcp_tools/call_event"
CORE_FIRE_EVENT = "fire_event"
STATE_CHANGED_EVENT = "state_changed"


def _parse_event_data(data: str | dict[str, Any] | None) -> dict[str, Any] | None:
    return _parse_json_dict_param(
        data, type_error_message="Event data must be a JSON object (dict)"
    )


def _fire_message(
    command: str, event_type: str, data: dict[str, Any] | None
) -> dict[str, Any]:
    """Build a WebSocket fire message, omitting an absent payload.

    Core's ``fire_event`` schema types ``event_data`` as a dict, so an explicit
    null is refused as an invalid payload; the key is simply absent when the
    caller passed no data.
    """
    message: dict[str, Any] = {"type": command, "event_type": event_type}
    if data is not None:
        message["event_data"] = data
    return message


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
        ha_config_set_automation(entity_id=..., run_actions=True).

        Use this to publish custom event types consumed by event-triggered automations,
        Node-RED flows, or custom integrations that subscribe to specific event types.

        Caveats: Events are fire-and-forget; a successful dispatch reports the
        bus accepted the event and how many listeners were registered for it,
        not that any of them acted. A timeout means dispatch is unconfirmed.
        listener_count covers the exact event type; wildcard_listener_count
        covers match-all listeners (the recorder is one, and it only stores the
        event). Both counts are returned only when the optional listener
        lookup succeeds.

        The response carries the event's context_id (and context) when Home
        Assistant reports it. An automation triggered by this event records that
        id as the parent_id of its run's context, so looking up that automation
        trace proves whether a listener acted — which the counts cannot show,
        because an event trigger that filters on event_data still registers as a
        listener when the payload does not match.

        Events fire in-process through the ha_mcp_tools component when it is
        installed, otherwise over Home Assistant's own WebSocket fire_event
        command; both carry the event type in the message body, so any type Core
        accepts fires as written and Core's rejection comes back as the error
        text (for example its 64-character event-type limit). state_changed
        alone keeps the REST route, because only that route rebuilds its
        old_state/new_state payloads into State objects; there event_type must
        be a plain name with no / \\ ? # or % characters.
        """
        if not event_type or not event_type.strip():
            raise_tool_error(
                create_validation_error(
                    "event_type cannot be empty or whitespace",
                    parameter="event_type",
                )
            )

        parsed_data = _parse_event_data(data)

        # state_changed is the one route that must keep REST: APIEventView
        # rebuilds its old_state/new_state dicts into State objects, which the
        # WebSocket fire_event command does not do.
        if event_type == STATE_CHANGED_EVENT:
            return await self._fire_event_rest(event_type, parsed_data)

        component_result = await self._fire_event_component(event_type, parsed_data)
        if component_result is not None:
            return component_result

        ws_result = await self._fire_event_ws(event_type, parsed_data)
        if ws_result is not None:
            return ws_result

        return await self._fire_event_rest(event_type, parsed_data)

    async def _listener_fields(self, event_type: str) -> dict[str, Any]:
        """Read the optional listener counts, never blocking the fire."""
        listener_counts: dict[str, int] | None = None
        try:
            async with asyncio.timeout(EVENT_LISTENER_LOOKUP_BUDGET_SECONDS):
                listener_counts = await get_event_listeners(self._client, event_type)
        except (HomeAssistantError, TimeoutError) as err:
            logger.debug("Event listener lookup failed: %s", err)
        return event_listener_fields(listener_counts, event_type)

    async def _fire_event_component(
        self, event_type: str, data: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """Fire through the component bridge when it advertises call_event.

        A command that the component does not know is safe to fall back from:
        it was refused before dispatch. Other component errors are surfaced, not
        retried through another path, because the event may already have fired.
        """
        caps = await get_component_caps(self._client)
        if not component_supports(caps, "call_event"):
            return None
        try:
            raw = await self._client.send_websocket_message(
                _fire_message(WS_CALL_EVENT, event_type, data)
            )
        except HomeAssistantCommandNotSent:
            return None
        except Exception as err:  # noqa: BLE001
            exception_to_structured_error(
                err,
                context={"event_type": event_type},
                suggestions=["Check the Home Assistant connection"],
            )
        if not isinstance(raw, dict):
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "The component returned an invalid call_event result",
                    context={"event_type": event_type},
                )
            )
        if raw.get("success") is False:
            if raw.get("error_code") == "unknown_command":
                invalidate_caps(self._client)
                return None
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    str(raw.get("error") or "The component rejected the event"),
                    context={"event_type": event_type},
                )
            )
        context = raw.get("context")
        if not isinstance(context, dict):
            context = {}
        fields = event_listener_fields(
            {
                event_type: raw.get("listener_count", 0),
                "*": raw.get("wildcard_listener_count", 0),
            },
            event_type,
        )
        return {
            "success": True,
            "event_type": event_type,
            "message": f"Event {event_type} fired.",
            "context": context,
            "context_id": context.get("id"),
            **fields,
        }

    async def _fire_event_ws(
        self, event_type: str, data: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """Fire through Core's WebSocket fire_event command."""
        fields = await self._listener_fields(event_type)
        try:
            raw = await self._client.send_websocket_message(
                _fire_message(CORE_FIRE_EVENT, event_type, data)
            )
        except HomeAssistantCommandNotSent:
            return None
        except Exception as err:  # noqa: BLE001
            exception_to_structured_error(
                err,
                context={"event_type": event_type},
                suggestions=["Check the Home Assistant connection"],
            )
        if not isinstance(raw, dict):
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Home Assistant returned an invalid fire_event result",
                    context={"event_type": event_type},
                )
            )
        if raw.get("success") is False:
            if raw.get("error_code") == "unknown_command":
                invalidate_caps(self._client)
                return None
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    str(raw.get("error") or "Home Assistant rejected the event"),
                    context={"event_type": event_type},
                )
            )
        context = raw.get("context")
        if not isinstance(context, dict):
            context = {}
        return {
            "success": True,
            "event_type": event_type,
            "message": f"Event {event_type} fired.",
            "context": context,
            "context_id": context.get("id"),
            **fields,
        }

    async def _fire_event_rest(
        self, event_type: str, data: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Fire through the REST view; required for state_changed and older HA."""
        if any(separator in event_type for separator in _EVENT_TYPE_SEPARATORS):
            raise_tool_error(
                create_validation_error(
                    "event_type cannot contain path separators or URL delimiters "
                    "on the REST fallback",
                    parameter="event_type",
                    details=f"Received: {event_type!r}",
                )
            )
        listener_fields = await self._listener_fields(event_type)
        try:
            response = await self._client.fire_event(event_type, data)
        except HomeAssistantConnectionError as error:
            if isinstance(error.__cause__, httpx.ReadTimeout):
                warnings = list(listener_fields.get("warnings", ()))
                warnings.append(
                    "Response timed out. The event may have been dispatched; "
                    "delivery to subscribers is unconfirmed."
                )
                return {
                    "success": True,
                    "partial": True,
                    "event_type": event_type,
                    "message": (
                        f"Event {event_type} may have been dispatched, but Home Assistant "
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
