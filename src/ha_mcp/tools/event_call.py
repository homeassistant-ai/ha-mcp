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

from ..client.event_listeners import MATCH_ALL_EVENT_KEY, get_event_listeners
from ..client.rest_client import (
    HomeAssistantAdminRequiredError,
    HomeAssistantAuthError,
    HomeAssistantClient,
    HomeAssistantCommandError,
    HomeAssistantCommandNotSent,
    HomeAssistantConnectionError,
    HomeAssistantError,
)
from ..errors import ErrorCode, create_error_response, create_validation_error
from .coercion import JSON_STRING_COERCION
from .component_api import (
    UNKNOWN_COMMAND_CODE,
    component_probe_auth_kind,
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
# used for state_changed, whose dict payloads APIEventView rebuilds into State
# objects (the WebSocket routes do not), and as the fallback when the WebSocket
# fire_event command provably was not sent.
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


_UNKNOWN_COMMAND: dict[str, Any] = {}

_EVENT_REJECTION_SUGGESTIONS = [
    "Check the event_type spelling; Home Assistant limits it to 64 characters",
    "Verify the event type is accepted by Home Assistant",
]

_ADMIN_EVENT_MESSAGE = (
    "Firing events is admin-only in Home Assistant and this token's user is "
    "not an administrator"
)

_TOKEN_REJECTED_MESSAGE = "Home Assistant rejected the access token"

_AMBIGUOUS_DISPATCH_WARNING = (
    "Home Assistant did not confirm the event. It may have been dispatched; "
    "delivery to subscribers is unconfirmed."
)


def _as_admin_required(err: Exception) -> Exception:
    """Map Core's WebSocket ``unauthorized`` refusal to the admin-required type."""
    if (
        isinstance(err, HomeAssistantCommandError)
        and getattr(err, "code", None) == "unauthorized"
    ):
        return HomeAssistantAdminRequiredError(_ADMIN_EVENT_MESSAGE)
    return err


def _stop_on_auth(exc: HomeAssistantCommandNotSent, event_type: str) -> None:
    """Stop before a fallback when the rejected token caused the failure."""
    if isinstance(exc.__cause__, HomeAssistantAuthError):
        exception_to_structured_error(
            exc,
            context={"event_type": event_type},
            suggestions=["Check the Home Assistant token"],
        )


def _merge_warnings(
    listener_fields: dict[str, Any], extra: list[str]
) -> dict[str, Any]:
    warnings = list(listener_fields.get("warnings", ())) + extra
    merged = {key: value for key, value in listener_fields.items() if key != "warnings"}
    if warnings:
        merged["warnings"] = warnings
    return merged


def _component_listener_counts(
    payload: dict[str, Any], event_type: str
) -> dict[str, int] | None:
    exact = payload.get("listener_count")
    wildcard = payload.get("wildcard_listener_count")
    if type(exact) is not int or exact < 0 or type(wildcard) is not int or wildcard < 0:
        return None
    return {event_type: exact, MATCH_ALL_EVENT_KEY: wildcard}


def _success_result(
    event_type: str, payload: dict[str, Any], listener_fields: dict[str, Any]
) -> dict[str, Any]:
    context = payload.get("context")
    if not isinstance(context, dict):
        context = {}
    return {
        "success": True,
        "event_type": event_type,
        "message": f"Event {event_type} fired.",
        "context": context,
        "context_id": context.get("id"),
        **listener_fields,
    }


def _partial_result(event_type: str, listener_fields: dict[str, Any]) -> dict[str, Any]:
    return {
        "success": True,
        "partial": True,
        "event_type": event_type,
        "message": (
            f"Event {event_type} may have been dispatched, but Home Assistant "
            "did not confirm it."
        ),
        "context": {},
        "context_id": None,
        **_merge_warnings(listener_fields, [_AMBIGUOUS_DISPATCH_WARNING]),
    }


def _fire_reply_payload(
    raw: Any,
    event_type: str,
    *,
    invalid_result: str,
    fallback_on_unknown: bool,
) -> dict[str, Any]:
    """Validate a fire reply and return its result payload.

    ``_UNKNOWN_COMMAND`` is returned only when ``fallback_on_unknown`` is set;
    the component route uses that to fall back, while Core's own ``fire_event``
    is registered in every supported Core and a rejection is surfaced.
    """
    if not isinstance(raw, dict):
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                invalid_result,
                context={"event_type": event_type},
                suggestions=_EVENT_REJECTION_SUGGESTIONS,
            )
        )
    if raw.get("success") is False:
        if fallback_on_unknown and raw.get("error_code") == UNKNOWN_COMMAND_CODE:
            return _UNKNOWN_COMMAND
        if raw.get("error_code") == "unauthorized":
            exception_to_structured_error(
                HomeAssistantAdminRequiredError(_ADMIN_EVENT_MESSAGE),
                context={"event_type": event_type},
                suggestions=_EVENT_REJECTION_SUGGESTIONS,
            )
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                str(raw.get("error") or "Home Assistant rejected the event"),
                context={"event_type": event_type},
                suggestions=_EVENT_REJECTION_SUGGESTIONS,
            )
        )
    payload = raw.get("result")
    if not isinstance(payload, dict):
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                invalid_result,
                context={"event_type": event_type},
                suggestions=_EVENT_REJECTION_SUGGESTIONS,
            )
        )
    return payload


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
        event). The counts come from the component's dispatch reply when it
        serves the fire, otherwise from a GET /api/events lookup taken before
        dispatch; a route that cannot report them omits both fields.

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

        return await self._fire_event_ws(event_type, parsed_data)

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
        it was refused before dispatch. Other component errors are surfaced,
        except a connection loss after the send, which returns ``partial``
        because the event may already have fired.
        """
        caps = await get_component_caps(self._client)
        auth_kind = component_probe_auth_kind(self._client)
        if auth_kind:
            error: Exception = (
                HomeAssistantAdminRequiredError(_ADMIN_EVENT_MESSAGE)
                if auth_kind == "permission"
                else HomeAssistantAuthError(_TOKEN_REJECTED_MESSAGE)
            )
            exception_to_structured_error(
                error,
                context={"event_type": event_type},
                suggestions=["Check the Home Assistant token"],
            )
        if not component_supports(caps, "call_event"):
            return None
        try:
            raw = await self._client.send_websocket_message(
                _fire_message(WS_CALL_EVENT, event_type, data)
            )
        except HomeAssistantCommandNotSent as exc:
            _stop_on_auth(exc, event_type)
            return None
        except HomeAssistantConnectionError:
            return _partial_result(event_type, {})
        except Exception as err:  # noqa: BLE001
            exception_to_structured_error(
                _as_admin_required(err),
                context={"event_type": event_type},
                suggestions=_EVENT_REJECTION_SUGGESTIONS,
            )
        payload = _fire_reply_payload(
            raw,
            event_type,
            invalid_result="The component returned an invalid call_event result",
            fallback_on_unknown=True,
        )
        if payload is _UNKNOWN_COMMAND:
            invalidate_caps(self._client)
            return None
        fields = event_listener_fields(
            _component_listener_counts(payload, event_type), event_type
        )
        return _success_result(event_type, payload, fields)

    async def _fire_event_ws(
        self, event_type: str, data: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Fire through Core's WebSocket fire_event command."""
        fields = await self._listener_fields(event_type)
        try:
            raw = await self._client.send_websocket_message(
                _fire_message(CORE_FIRE_EVENT, event_type, data)
            )
        except HomeAssistantCommandNotSent as exc:
            _stop_on_auth(exc, event_type)
            return await self._fire_event_rest(
                event_type,
                data,
                fields,
                fallback_reason=(
                    f"the WebSocket fire_event command was not sent: {exc}"
                ),
            )
        except HomeAssistantConnectionError:
            # The frame may already have been sent; never turn this into an
            # error that invites a retry.
            return _partial_result(event_type, fields)
        except Exception as err:  # noqa: BLE001
            exception_to_structured_error(
                _as_admin_required(err),
                context={"event_type": event_type},
                suggestions=_EVENT_REJECTION_SUGGESTIONS,
            )
        payload = _fire_reply_payload(
            raw,
            event_type,
            invalid_result="Home Assistant returned an invalid fire_event result",
            fallback_on_unknown=False,
        )
        return _success_result(event_type, payload, fields)

    async def _fire_event_rest(
        self,
        event_type: str,
        data: dict[str, Any] | None,
        listener_fields: dict[str, Any] | None = None,
        *,
        fallback_reason: str | None = None,
    ) -> dict[str, Any]:
        """Fire through the REST view, for state_changed or a WS fallback.

        ``listener_fields`` reuses a lookup the caller already made, so a
        WebSocket-to-REST fallback does not wait for the counts twice.
        ``fallback_reason`` records why the WebSocket route was unavailable, so
        the response says the context is missing rather than silent.
        """
        if any(separator in event_type for separator in _EVENT_TYPE_SEPARATORS):
            route = "the REST fallback" if fallback_reason else "the REST route"
            because = f" because {fallback_reason}" if fallback_reason else ""
            raise_tool_error(
                create_validation_error(
                    "event_type cannot contain path separators or URL delimiters "
                    f"on {route}{because}",
                    parameter="event_type",
                    details=f"Received: {event_type!r}",
                )
            )
        if listener_fields is None:
            listener_fields = await self._listener_fields(event_type)
        try:
            response = await self._client.fire_event(event_type, data)
        except HomeAssistantConnectionError as error:
            if isinstance(error.__cause__, httpx.ReadTimeout):
                return _partial_result(event_type, listener_fields)
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
        extra_warnings: list[str] = []
        if fallback_reason:
            extra_warnings.append(
                "WebSocket fire_event was unavailable; the event used the REST "
                "route, so context_id is not reported."
            )
        return {
            "success": True,
            "event_type": event_type,
            "message": response.get("message", f"Event {event_type} fired."),
            "context": {},
            "context_id": None,
            **_merge_warnings(listener_fields, extra_warnings),
        }
