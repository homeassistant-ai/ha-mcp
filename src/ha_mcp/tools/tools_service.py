"""
Service call and device operation tools for Home Assistant MCP server.

This module provides service execution and WebSocket-enabled operation monitoring tools.
"""

import logging
from typing import Annotated, Any, NoReturn, cast

import httpx
from fastmcp import Context
from fastmcp.exceptions import ToolError
from fastmcp.tools import tool
from pydantic import Field

from ..client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantConnectionError,
)
from ..client.websocket_client import get_websocket_client
from ..errors import (
    ErrorCode,
    create_error_response,
    create_validation_error,
)
from .component_api import (
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)
from .util_helpers import (
    BLOCKED_WS_WRITE_COMMANDS,
    JSON_STRING_COERCION,
    compact_service_result,
    parse_json_param,
    parse_string_list_param,
    project_entity_record,
    wait_for_state_change,
)

# The ha_mcp_tools/call_service WS command: the first WRITE capability (Phase 3,
# issue #1813). When the component advertises ``call_service`` the consumer routes a
# single service call through this one in-process frame, which fires exactly one
# ``async_call`` and returns the REAL pre->post transition, replacing the legacy
# REST POST + hardcoded ``_SERVICE_TO_STATE`` guess + WS-subscribe verification.
# Named once so the routing helper and its tests agree on the wire string.
WS_CALL_SERVICE = "ha_mcp_tools/call_service"


class _AmbiguousDispatch:
    """Sentinel type for a post-send-ambiguous component write (see below)."""


# Returned by ``_call_service_via_component`` when the component frame was SENT but
# its response/confirmation never arrived (a response-wait timeout or a post-send
# transport drop): the write MAY have landed, so the caller reports it as ``partial``
# and MUST NOT re-POST via the legacy path (D9 at-most-once — an ambiguous post-send
# outcome is never retried). Distinct from ``None`` (the component provably never
# dispatched → a safe legacy first fire).
_COMPONENT_DISPATCH_AMBIGUOUS = _AmbiguousDispatch()


def _parse_json_dict_param(
    data: str | dict[str, Any] | None,
    *,
    type_error_message: str,
) -> dict[str, Any] | None:
    if data is None:
        return None
    raw: Any = None
    try:
        raw = parse_json_param(data, "data")
    except ValueError as e:
        raise_tool_error(
            create_validation_error(
                f"Invalid data parameter: {e}",
                parameter="data",
                invalid_json=True,
            )
        )
    if raw is not None and not isinstance(raw, dict):
        raise_tool_error(
            create_validation_error(
                type_error_message,
                parameter="data",
                details=f"Received type: {type(raw).__name__}",
            )
        )
    return raw if isinstance(raw, dict) else None


def _parse_event_data(data: str | dict[str, Any] | None) -> dict[str, Any] | None:
    return _parse_json_dict_param(
        data, type_error_message="Event data must be a JSON object (dict)"
    )


logger = logging.getLogger(__name__)

# Services that produce observable state changes on entities
_STATE_CHANGING_SERVICES = {
    "turn_on",
    "turn_off",
    "toggle",
    "open",
    "close",
    "lock",
    "unlock",
    "set_temperature",
    "set_hvac_mode",
    "set_fan_mode",
    # fan.set_speed was removed in the HA percentage migration (gone in 2026.6);
    # its state-changing successors are set_percentage / set_preset_mode.
    "set_percentage",
    "set_preset_mode",
    "select_option",
    "set_value",
    "set_datetime",
    "set_cover_position",
    "set_position",
    "play_media",
    "media_play",
    "media_pause",
    "media_stop",
}

# Domains where a service call does not move the TARGET entity's own primary
# state to a value the verifier can wait for. ``scene`` belongs here with
# ``automation``/``script``: activating a scene changes the member entities,
# but the scene entity's own state is a last-activated timestamp that never
# becomes "on"/"off" — so waiting for ``turn_on`` -> "on" always times out and
# only appends a spurious "could not be verified" warning (~10s wasted).
_NON_STATE_CHANGING_DOMAINS = {
    "automation",
    "script",
    "scene",
    "homeassistant",
    "notify",
    "tts",
    "persistent_notification",
    "logbook",
    "system_log",
}

# Mapping from service name to the expected resulting state
_SERVICE_TO_STATE: dict[str, str] = {
    "turn_on": "on",
    "turn_off": "off",
    "open": "open",
    "close": "closed",
    "lock": "locked",
    "unlock": "unlocked",
}


# WebSocket commands that stream events or reply in two phases (an initial ack
# then the real payload as a follow-up event) rather than resolving to a single
# terminal result. ha_call_service's ws_command escape hatch only supports
# one-shot request/response commands, so these are rejected: the "subscribe"
# substring catches subscription commands, and the set names known two-phase /
# streaming commands whose names don't contain "subscribe". The set is a floor,
# not an exhaustive list -- HA's WS command naming isn't consistent enough for a
# name check alone to be fully reliable.
_WS_COMMAND_EVENT_BLOCKLIST = frozenset(
    {
        "render_template",  # event-based; use ha_eval_template instead
        "system_health/info",  # two-phase (see tools_system._fetch_health_info)
        "assist_pipeline/run",  # streams pipeline events
    }
)

# Substrings that mark a WS command as streaming / subscription-based even when
# its name isn't in the blocklist above. Such commands ack once and then push
# follow-up events on the same id; the one-shot send_command path returns the
# ack and leaks the subscription. "subscribe" covers subscribe_* and */subscribe;
# "stream" covers history/stream, logbook/event_stream, camera/stream, ...;
# "start_preview" covers template/start_preview and the config-flow preview family.
_WS_STREAMING_SUBSTRINGS = ("subscribe", "stream", "start_preview")

# One-shot WS commands that re-enter Home Assistant's service invocation. Routing
# them through the escape hatch would bypass the service-mode guards (notably the
# reserved ha_mcp_tools domain block), so they are rejected -- use the
# domain/service parameters for service calls instead.
_WS_COMMAND_SERVICE_INVOKERS = frozenset({"call_service", "execute_script"})

# Reserved WebSocket envelope keys the transport owns. Allowing them inside data
# would let a caller override the validated command type (defeating every check
# below) or collide with the transport's message id, so they are rejected.
_WS_RESERVED_ENVELOPE_KEYS = frozenset({"type", "id"})


def _is_streaming_ws_command(command_type: str) -> bool:
    """Return True for subscription / streaming / two-phase WS commands."""
    lowered = command_type.lower()
    if lowered in _WS_COMMAND_EVENT_BLOCKLIST:
        return True
    return any(sub in lowered for sub in _WS_STREAMING_SUBSTRINGS)


def _build_service_suggestions(
    domain: str, service: str, entity_id: str | None
) -> list[str]:
    """Build common error suggestions for service call failures."""
    return [
        f"Verify {entity_id} exists using ha_get_state()"
        if entity_id
        else "Specify an entity_id for targeted service calls",
        f"Check available services for {domain} domain using ha_get_skill_guide",
        "Use ha_search() to find correct entity IDs",
    ]


class ServiceTools:
    """Service call and device operation tools for Home Assistant."""

    def __init__(self, client: Any, device_tools: Any) -> None:
        self._client = client
        self._device_tools = device_tools

    @staticmethod
    def _parse_service_data(
        data: str | dict[str, Any] | None,
        entity_id: str | None,
    ) -> dict[str, Any]:
        """Parse and validate the data parameter into a service_data dict."""
        service_data: dict[str, Any] = (
            _parse_json_dict_param(
                data, type_error_message="Data parameter must be a JSON object"
            )
            or {}
        )
        if entity_id:
            service_data["entity_id"] = entity_id
        return service_data

    @staticmethod
    def _validate_service_call_params(
        domain: str | None, service: str | None
    ) -> tuple[str, str]:
        """Validate service-mode params and return the (domain, service) pair.

        Raises a structured ToolError when domain/service are missing (the caller
        likely wants the ws_command escape hatch) or when the domain targets the
        reserved ha_mcp_tools namespace.
        """
        if not domain or not service:
            raise_tool_error(
                create_validation_error(
                    "domain and service are required for a service call. To send "
                    "a raw WebSocket command instead, pass ws_command.",
                    parameter="domain" if not domain else "service",
                )
            )
        # ha_mcp_tools.* services are restricted to the ha-mcp server's dedicated
        # wrappers (which inject the required caller token). Block ha_call_service
        # from forwarding to that domain — it would otherwise be a bypass path
        # around the dedicated tools. HA core's service registry lowercases the
        # domain on fallback lookup (homeassistant/core.py
        # ServiceRegistry.async_call), so normalise here to make sure a mixed-case
        # `HA_MCP_TOOLS` can't slip past this exact-string check and still resolve
        # downstream.
        if domain.strip().lower() == "ha_mcp_tools":
            raise_tool_error(
                create_validation_error(
                    (
                        "ha_call_service cannot invoke services in the "
                        "'ha_mcp_tools' domain. Use the dedicated MCP tool "
                        "instead: ha_list_files, ha_read_file, ha_write_file, "
                        "ha_delete_file, or ha_config_set_yaml."
                    ),
                    parameter="domain",
                )
            )
        return domain, service

    @staticmethod
    def _reject_incompatible_ws_params(
        entity_id: str | None,
        return_response: bool,
        verbose: bool,
        result_fields: str | list[str] | None,
        result_attribute_keys: str | list[str] | None,
    ) -> None:
        """Reject service-mode-only params when the ws_command escape hatch is used.

        These shape a registered-service call and have no meaning for a raw
        WebSocket command; silently ignoring them would be a confusing no-op, so
        (mirroring the domain/service "not both" guard) they must be omitted.
        """
        offenders = [
            name
            for name, is_set in (
                ("entity_id", entity_id is not None),
                ("return_response", return_response),
                ("verbose", verbose),
                ("result_fields", result_fields is not None),
                ("result_attribute_keys", result_attribute_keys is not None),
            )
            if is_set
        ]
        if offenders:
            raise_tool_error(
                create_validation_error(
                    "These parameters apply only to service calls and must be "
                    f"omitted when ws_command is set: {', '.join(offenders)}.",
                    parameter="ws_command",
                )
            )

    @staticmethod
    def _parse_result_projection_params(
        result_fields: str | list[str] | None,
        result_attribute_keys: str | list[str] | None,
    ) -> tuple[list[str] | None, list[str] | None]:
        """Parse and validate result_fields / result_attribute_keys into lists.

        Raises a structured VALIDATION_INVALID_PARAMETER ToolError on malformed
        input for either parameter.
        """
        try:
            parsed_result_fields = parse_string_list_param(
                result_fields, "result_fields", allow_csv=True
            )
        except ValueError as e:
            raise_tool_error(create_validation_error(str(e), parameter="result_fields"))
        try:
            parsed_result_attribute_keys = parse_string_list_param(
                result_attribute_keys, "result_attribute_keys", allow_csv=True
            )
        except ValueError as e:
            raise_tool_error(
                create_validation_error(str(e), parameter="result_attribute_keys")
            )
        return parsed_result_fields, parsed_result_attribute_keys

    @staticmethod
    def _build_timeout_response(
        domain: str,
        service: str,
        entity_id: str | None,
        data: str | dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Build a partial-success response for service call timeouts."""
        return {
            "success": True,
            "partial": True,
            "domain": domain,
            "service": service,
            "entity_id": entity_id,
            "parameters": data,
            "message": (
                f"Service {domain}.{service} was dispatched but Home Assistant "
                f"did not respond within the timeout period. The operation is likely "
                f"still running in the background."
            ),
            "warnings": [
                "Response timed out. This is normal for long-running services "
                f"like updates or firmware installs. Use ha_get_state('{entity_id}') "
                "to check the current status."
                if entity_id
                else "Response timed out. This is normal for long-running services. "
                "The service was dispatched and may still be executing."
            ],
        }

    async def _capture_initial_state(self, entity_id: str | None) -> str | None:
        """Capture the current state of an entity before a service call."""
        try:
            state_data = await self._client.get_entity_state(entity_id)
            return state_data.get("state") if state_data else None
        except Exception as e:
            logger.debug(
                f"Could not fetch initial state for {entity_id}: {e} — state verification may be degraded"
            )
            return None

    async def _verify_state_change(
        self,
        entity_id: str,
        service: str,
        initial_state: str | None,
        response: dict[str, Any],
    ) -> None:
        """Wait for and verify entity state change after a service call, updating response in place."""
        try:
            expected = _SERVICE_TO_STATE.get(service)
            new_state = await wait_for_state_change(
                self._client,
                entity_id,
                expected_state=expected,
                initial_state=initial_state,
                timeout=10.0,
            )
            if new_state:
                response["verified_state"] = new_state.get("state")
            else:
                response.setdefault("warnings", []).append(
                    "Service executed but state change could not be verified within timeout."
                )
        except Exception as e:
            response.setdefault("warnings", []).append(
                f"Service executed but state verification failed: {e}"
            )

    @staticmethod
    def _project_service_result(
        result: Any,
        *,
        entity_id: str | None,
        verbose: bool,
        fields: list[str] | None,
        attribute_keys: list[str] | None,
    ) -> tuple[Any, list[str]]:
        """Apply compact / explicit projection to a service-call ``result``.

        Issue #1446. Precedence:

        - ``verbose=True``: bypass every transformation; return ``result`` as-is.
        - Explicit ``fields`` or ``attribute_keys``: apply per-record projection
          via ``project_entity_record`` to every record. No compaction; this is
          the power-user path.
        - Default: apply ``compact_service_result`` (filter to ``entity_id``
          record when single string, drop top-level metadata + heavy lists).

        Returns ``(projected, warnings)``. ``warnings`` collects per-record
        typo-guard diagnostics from ``project_entity_record`` (e.g. all-empty
        ``attribute_keys`` filter) — deduplicated so an N-record list with the
        same typo doesn't emit N copies of the same warning.
        """
        if verbose:
            return result, []
        if fields is None and attribute_keys is None:
            return compact_service_result(result, entity_id), []
        if not isinstance(result, list):
            return result, []
        warnings: list[str] = []
        # ``result_attribute_keys`` only takes effect when ``attributes`` is in
        # the projected ``result_fields`` (or ``result_fields`` is None). Surface
        # a warning rather than silently ignoring the parameter — mirrors
        # ha_get_state's attribute_keys_no_effect handling.
        if (
            attribute_keys is not None
            and fields is not None
            and "attributes" not in fields
        ):
            warnings.append(
                "result_attribute_keys was ignored because 'attributes' is not "
                "in result_fields. Add 'attributes' to result_fields (or omit "
                "result_fields) to apply result_attribute_keys."
            )
        projected: list[Any] = []
        seen_warnings: set[str] = set()
        for record in result:
            new_record, warn = project_entity_record(record, fields, attribute_keys)
            projected.append(new_record)
            if warn and warn not in seen_warnings:
                seen_warnings.add(warn)
                warnings.append(warn)
        return projected, warnings

    def _handle_connection_error(
        self,
        error: HomeAssistantConnectionError,
        *,
        domain: str,
        service: str,
        entity_id: str | None,
        data: str | dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Handle a HomeAssistantConnectionError raised while calling a service.

        Timeouts are treated as partial success (the service was dispatched but
        Home Assistant did not respond in time) and return a partial-success
        response. Non-timeout connection errors raise a structured ToolError.
        """
        # Check if this is a timeout - for service calls, timeouts typically
        # mean the service was dispatched but HA didn't respond in time.
        # The operation is likely still running (e.g., update.install, long automations).
        if isinstance(error.__cause__, httpx.TimeoutException):
            return self._build_timeout_response(domain, service, entity_id, data)
        # Non-timeout connection errors are real failures
        exception_to_structured_error(
            error,
            context={
                "domain": domain,
                "service": service,
                "entity_id": entity_id,
            },
            suggestions=_build_service_suggestions(domain, service, entity_id),
        )
        return None  # unreachable: exception_to_structured_error always raises

    @staticmethod
    def _raise_unexpected_call_service_error(
        error: Exception,
        *,
        domain: str,
        service: str,
        entity_id: str | None,
    ) -> NoReturn:
        """Raise a structured ToolError for an unexpected ha_call_service failure."""
        suggestions = _build_service_suggestions(domain, service, entity_id)
        if entity_id:
            suggestions.extend(
                [
                    f"For automation: ha_call_service('automation', 'trigger', entity_id='{entity_id}')",
                    f"For universal control: ha_call_service('homeassistant', 'toggle', entity_id='{entity_id}')",
                ]
            )
        exception_to_structured_error(
            error,
            context={
                "domain": domain,
                "service": service,
                "entity_id": entity_id,
            },
            suggestions=suggestions,
        )

    async def _call_ws_command(
        self,
        ws_command: str,
        data: str | dict[str, Any] | None,
        *,
        domain: str | None,
        service: str | None,
    ) -> dict[str, Any]:
        """Send a one-shot WebSocket command via ha_call_service's escape hatch.

        Reaches Home Assistant WebSocket commands that are not registered
        services (e.g. ``repairs/ignore_issue``). Only one-shot
        request/response commands are supported — streaming / subscription
        commands are rejected up front.
        """
        command_type = ws_command.strip()
        if domain is not None or service is not None:
            raise_tool_error(
                create_validation_error(
                    "Provide either domain + service (a registered service call) "
                    "OR ws_command (a raw WebSocket command), not both.",
                    parameter="ws_command",
                )
            )
        if not command_type:
            raise_tool_error(
                create_validation_error(
                    "ws_command must be a non-empty WebSocket command type, "
                    "e.g. 'repairs/ignore_issue'.",
                    parameter="ws_command",
                )
            )
        if _is_streaming_ws_command(command_type):
            raise_tool_error(
                create_validation_error(
                    f"ws_command '{command_type}' is a streaming or two-phase "
                    "command; ha_call_service only sends one-shot "
                    "request/response commands. For template rendering use "
                    "ha_eval_template.",
                    parameter="ws_command",
                )
            )
        if command_type.lower() in _WS_COMMAND_SERVICE_INVOKERS:
            raise_tool_error(
                create_validation_error(
                    f"ws_command '{command_type}' invokes Home Assistant services "
                    "and would bypass ha_call_service's safeguards. Use the "
                    "domain/service parameters for service calls instead.",
                    parameter="ws_command",
                )
            )
        if command_type.lower().startswith("ha_mcp_tools/"):
            raise_tool_error(
                create_validation_error(
                    "ha_call_service cannot invoke 'ha_mcp_tools/*' WebSocket "
                    "commands. Use the dedicated ha-mcp tools instead.",
                    parameter="ws_command",
                )
            )
        if command_type.lower() in BLOCKED_WS_WRITE_COMMANDS:
            raise_tool_error(
                create_validation_error(
                    f"ws_command '{command_type}' mutates persistent state that a "
                    "dedicated tool guards with backups and conflict checks. Use "
                    "the corresponding ha-mcp tool (e.g. ha_config_set_dashboard, "
                    "ha_set_area_or_floor, ha_remove_entity) instead.",
                    parameter="ws_command",
                )
            )
        command_params = (
            _parse_json_dict_param(
                data, type_error_message="ws_command data must be a JSON object"
            )
            or {}
        )
        reserved = _WS_RESERVED_ENVELOPE_KEYS & command_params.keys()
        if reserved:
            raise_tool_error(
                create_validation_error(
                    "data must not contain the reserved WebSocket envelope key(s) "
                    f"{', '.join(sorted(reserved))}; the command type is set by "
                    "ws_command, and the message id is managed by the transport.",
                    parameter="data",
                )
            )
        # send_websocket_message catches its own transport errors and always
        # returns a {"success": ...} dict (never raises), so the failure is
        # handled by the result-shape check below -- matching the other
        # send_websocket_message call sites in the codebase.
        result = await self._client.send_websocket_message(
            {"type": command_type, **command_params}
        )

        if not isinstance(result, dict) or not result.get("success", False):
            error_msg = (
                result.get("error") if isinstance(result, dict) else None
            ) or "WebSocket command failed"
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    str(error_msg),
                    context={"ws_command": command_type},
                    suggestions=[
                        "Verify the command type and its parameters (e.g. "
                        + "repairs/ignore_issue needs domain, issue_id, ignore)",
                        "Confirm the target still exists (a repair must be "
                        + "present to ignore it)",
                    ],
                )
            )

        return {
            "success": True,
            "ws_command": command_type,
            "parameters": command_params or None,
            "result": result.get("result"),
            "message": f"Successfully executed WebSocket command '{command_type}'",
        }

    async def _call_service_via_component(
        self,
        *,
        domain: str,
        service: str,
        service_data: dict[str, Any],
        entity_ids: list[str],
        wait: bool,
        timeout: float,
        return_response: bool,
    ) -> dict[str, Any] | _AmbiguousDispatch | None:
        """Route one service call through the component ``call_service`` capability.

        Returns one of three outcomes:

        * the component's frozen result envelope — ``{domain, service, dispatched,
          confirmed, partial, transitions, service_response?}`` — when the component
          advertises ``call_service``, the frame lands, and its response arrives (the
          caller maps it and does NOT re-POST, even for a ``partial`` confirmation);
        * ``None`` when the component provably never dispatched, so the caller runs
          its legacy REST POST as a safe first fire;
        * ``_COMPONENT_DISPATCH_AMBIGUOUS`` when the frame WAS sent but its
          response/confirmation never arrived — the caller reports ``partial`` and
          does NOT re-POST.

        Verb resolution stays server-side (D6): the fully-formed ``domain`` /
        ``service`` / ``service_data`` / ``entity_ids`` are handed to the component,
        which fires exactly what it is given and never guesses a service name.

        **D9 — at-most-once (correctness-critical).** The boundary is PRE-SEND vs
        POST-SEND, NOT "error vs success":

        * PRE-SEND → ``None`` (safe legacy first fire): a capability miss, an
          ``unknown_command``, or a connection-ESTABLISHMENT failure
          (``get_websocket_client`` raising before the frame is sent) all mean the
          component never dispatched. A command-ERROR RESPONSE is also ``None``: it is
          pre-dispatch for the component's own guards (the D1 domain block and
          ``ServiceNotFound`` raise before any ``async_call``), and for a non-unknown
          command error it is the ONE documented residual — an ``async_call`` that
          mutated state and THEN raised could double-apply on the legacy re-POST
          (accepted per the approved design; no idempotency token exists anywhere in
          the write path).
        * POST-SEND → never retried: a confirmation that lapsed comes back as a
          normal result dict (``partial=True`` / ``dispatched=True``). A response-wait
          TIMEOUT (``HomeAssistantCommandTimeout`` — the frame was sent) or any
          post-send transport drop is AMBIGUOUS-dispatched: the component's
          ``@async_response`` handler is a background HA task, so the client
          abandoning the message id does NOT cancel the write, and
          ``async_call(blocking=True)`` is itself unbounded (a long ``update.install``
          / script legitimately outlives the 30s response-wait). These return the
          sentinel so the caller reports ``partial`` and MUST NOT re-POST — re-POSTing
          here is the double-fire this split exists to prevent.

        **Security (layered defense-in-depth).** The server-side reserved-domain guard
        (``_validate_service_call_params``) gates BOTH this component route AND the
        legacy REST fallback, refusing ``ha_mcp_tools`` before either runs — it is the
        authoritative single-call gate. The component's own D1 block is the
        authoritative refusal AT the component for any future consumer that reaches it
        directly; here a component D1 refusal would surface as a command-error
        response → ``None`` → legacy REST, and that REST POST is itself gated by the
        same server guard, so no ``ha_mcp_tools`` invocation can reach REST.
        """
        caps = await get_component_caps(self._client)
        if not component_supports(caps, "call_service"):
            return None
        # PRE-SEND: an establishment failure means the frame provably never reached
        # the component. Split into its own try so a POST-SEND failure below is never
        # misclassified as pre-send → a safe legacy first fire.
        try:
            ws = await get_websocket_client(
                url=self._client.base_url, token=self._client.token
            )
        except Exception as exc:
            logger.warning(
                "%s establishment failed; falling back to legacy: %r",
                WS_CALL_SERVICE,
                exc,
            )
            return None
        # The frame is now sent. Distinguish a command-ERROR response (pre-dispatch by
        # the component's guards / the documented mutate-then-raise residual → legacy)
        # from a response-wait TIMEOUT or any post-send transport drop (AMBIGUOUS →
        # partial, never retried).
        try:
            raw = await ws.send_command(
                WS_CALL_SERVICE,
                domain=domain,
                service=service,
                service_data=service_data,
                entity_ids=entity_ids,
                wait=wait,
                timeout=timeout,
                return_response=return_response,
            )
        except HomeAssistantCommandError as exc:
            # unknown_command means the command vanished: invalidate the cached caps so
            # the next call re-probes. Any other command error → legacy re-POST (the
            # documented at-most-once residual, see D9 above).
            if is_unknown_command(exc):
                invalidate_caps(self._client)
            else:
                logger.warning(
                    "%s command error; falling back to legacy: %r",
                    WS_CALL_SERVICE,
                    exc,
                )
            return None
        except Exception as exc:
            # HomeAssistantCommandTimeout (response-wait expired — the frame WAS sent)
            # or any post-send transport drop (e.g. a pooled-WS drop after send). The
            # component may still be lawfully mid-write, so this is ambiguous-
            # dispatched: report partial, NEVER re-POST (D9 at-most-once).
            logger.warning(
                "%s post-send timeout/drop; reporting partial (not retried): %r",
                WS_CALL_SERVICE,
                exc,
            )
            return _COMPONENT_DISPATCH_AMBIGUOUS
        result = raw.get("result")
        # ``dispatched`` is the frozen sentinel key every ``_do_call_service`` envelope
        # carries; its absence means no usable component response (treated as
        # never-reached → legacy), never a silently-dropped write.
        if not isinstance(result, dict) or "dispatched" not in result:
            return None
        return result

    @staticmethod
    def _component_verified_state(
        transitions: list[Any], entity_id: str | None
    ) -> str | None:
        """The confirmed post-state for ``entity_id`` from the component transitions.

        ``ha_call_service`` targets a single entity, so the component returns one
        transition for it. Returns the transition's ``new_state.state`` (the REAL
        post-dispatch state), or ``None`` when the entity vanished / has no state.
        """
        for transition in transitions:
            if not isinstance(transition, dict):
                continue
            if entity_id is None or transition.get("entity_id") == entity_id:
                new_state = transition.get("new_state")
                if isinstance(new_state, dict):
                    return new_state.get("state")
        return None

    def _build_component_call_response(
        self,
        component_result: dict[str, Any],
        *,
        domain: str,
        service: str,
        entity_id: str | None,
        data: str | dict[str, Any] | None,
        should_wait: bool,
        return_response: bool,
        verbose: bool,
        fields: list[str] | None,
        attribute_keys: list[str] | None,
    ) -> dict[str, Any]:
        """Map the component ``call_service`` result into ha_call_service's shape.

        The component's real pre->post transition replaces BOTH the hardcoded
        ``_SERVICE_TO_STATE`` guess and the WS-subscribe-and-sample verification: the
        transition ``new_state`` records are the same ``State.as_dict()`` shape the
        legacy REST POST returns, so they feed the SAME ``_project_service_result``
        projection; the confirmed target's ``new_state.state`` becomes
        ``verified_state``; and the component's ``partial`` flag (dispatched but the
        confirming event lapsed) drives the same partial-success shape the legacy
        timeout path produces (``_build_timeout_response``).
        """
        transitions = component_result.get("transitions") or []
        # The transition new_states are State.as_dict() records — the same shape the
        # legacy REST POST returns — so the existing projection helpers apply
        # unchanged (compact filters to the target, drops metadata / heavy lists).
        new_states = [
            transition["new_state"]
            for transition in transitions
            if isinstance(transition, dict)
            and isinstance(transition.get("new_state"), dict)
        ]
        projected_result, projection_warnings = self._project_service_result(
            new_states,
            entity_id=entity_id,
            verbose=verbose,
            fields=fields,
            attribute_keys=attribute_keys,
        )
        response: dict[str, Any] = {
            "success": True,
            "domain": domain,
            "service": service,
            "entity_id": entity_id,
            "parameters": data,
            "result": projected_result,
            "message": f"Successfully executed {domain}.{service}",
        }
        if projection_warnings:
            response.setdefault("warnings", []).extend(projection_warnings)
        if return_response and component_result.get("service_response") is not None:
            response["service_response"] = component_result["service_response"]
        if should_wait:
            if component_result.get("partial"):
                # Dispatched, but the confirming state_changed did not arrive within
                # the wait — the same partial-success contract the legacy timeout
                # path reports (success stays True; verification is never a failure).
                response["partial"] = True
                response.setdefault("warnings", []).append(
                    "Service executed but state change could not be verified "
                    "within timeout."
                )
            else:
                verified_state = self._component_verified_state(transitions, entity_id)
                if verified_state is not None:
                    response["verified_state"] = verified_state
        return response

    async def _maybe_component_call_service(
        self,
        *,
        domain: str,
        service: str,
        service_data: dict[str, Any],
        entity_id: str | None,
        data: str | dict[str, Any] | None,
        should_wait: bool,
        return_response: bool,
        verbose: bool,
        fields: list[str] | None,
        attribute_keys: list[str] | None,
    ) -> dict[str, Any] | None:
        """Route a confirmable single call through the component; ``None`` → do legacy.

        The component route is taken ONLY when confirming a single entity
        (``should_wait``). The capability's entire value is the real confirmed
        pre->post transition; for a non-confirmed call (``wait=False``, no / multi
        entity, or a non-state-changing domain) the component returns
        ``transitions=[]`` → ``result:[]``, silently dropping the changed-states body
        the legacy REST POST returns (e.g. a scene's member states). For those the
        legacy single POST costs the same one round-trip and is strictly richer, so
        this returns ``None`` and the caller stays on legacy.

        Returns a FINAL ``ha_call_service`` response when the component served the
        call: the mapped transition, or — on a post-send timeout / transport drop
        (the ambiguous sentinel) — the same dispatched-but-unconfirmed ``partial`` the
        legacy timeout path builds, NEVER re-POSTed (D9 at-most-once). Returns ``None``
        when the component was not used or provably never dispatched, so the caller
        runs the legacy REST path as a safe first fire.
        """
        if not should_wait:
            return None
        component_result = await self._call_service_via_component(
            domain=domain,
            service=service,
            service_data=service_data,
            entity_ids=[entity_id] if entity_id else [],
            wait=True,
            timeout=10.0,
            return_response=return_response,
        )
        if isinstance(component_result, _AmbiguousDispatch):
            return self._build_timeout_response(domain, service, entity_id, data)
        if component_result is None:
            return None
        return self._build_component_call_response(
            component_result,
            domain=domain,
            service=service,
            entity_id=entity_id,
            data=data,
            should_wait=should_wait,
            return_response=return_response,
            verbose=verbose,
            fields=fields,
            attribute_keys=attribute_keys,
        )

    @tool(
        name="ha_call_service",
        tags={"Service & Device Control"},
        annotations={
            "openWorldHint": False,
            "destructiveHint": True,
            "title": "Call Service",
        },
    )
    @log_tool_usage
    async def ha_call_service(
        self,
        domain: str | None = None,
        service: str | None = None,
        entity_id: str | None = None,
        data: Annotated[dict[str, Any] | None, JSON_STRING_COERCION] = None,
        return_response: bool = False,
        wait: bool = True,
        verbose: Annotated[
            bool,
            Field(
                description=(
                    "Return HA's raw service response unchanged (default: False). "
                    "Use as an escape hatch when you need the full propagation "
                    "chain or raw attribute payload (debug / inspection). "
                    "WARNING: brings back token-bloat for nested-group targets — "
                    "prefer result_fields / result_attribute_keys for targeted control."
                ),
            ),
        ] = False,
        result_fields: Annotated[
            str | list[str] | None,
            JSON_STRING_COERCION,
            Field(
                default=None,
                description=(
                    "Project each record in 'result' to only these top-level keys "
                    "(e.g. ['entity_id', 'state']). Mirrors ha_get_state's fields=. "
                    "Setting this DISABLES default compaction — no entity-id filter, "
                    "no metadata strip — and applies the explicit projection instead."
                ),
            ),
        ] = None,
        result_attribute_keys: Annotated[
            str | list[str] | None,
            JSON_STRING_COERCION,
            Field(
                default=None,
                description=(
                    "Project each record's 'attributes' dict to only these keys "
                    "(e.g. ['brightness', 'rgb_color']). Mirrors ha_get_state's "
                    "attribute_keys=. Setting this DISABLES default compaction. "
                    "Requires 'attributes' to be present in result_fields (or "
                    "result_fields=None)."
                ),
            ),
        ] = None,
        ws_command: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Advanced escape hatch: send a raw one-shot Home Assistant "
                    "WebSocket command that is NOT a registered service (e.g. "
                    "'repairs/ignore_issue' to dismiss a Repairs issue). When set, "
                    "omit domain/service and the other service params; put the "
                    "command's parameters in data. Streaming/two-phase and "
                    "service-invoking commands (call_service, execute_script) are "
                    "rejected."
                ),
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Execute Home Assistant services to control entities and trigger automations.

        This is the universal tool for controlling all Home Assistant entities. Services follow
        the pattern domain.service (e.g., light.turn_on, climate.set_temperature).

        **Basic Usage:**
        ```python
        # Turn on a light
        ha_call_service("light", "turn_on", entity_id="light.living_room")

        # Set temperature with parameters
        ha_call_service("climate", "set_temperature",
                      entity_id="climate.thermostat", data={"temperature": 22})

        # Trigger automation
        ha_call_service("automation", "trigger", entity_id="automation.morning_routine")

        # Universal controls work with any entity
        ha_call_service("homeassistant", "toggle", entity_id="switch.porch_light")
        ```

        **Key behavior:**
        - **wait** (default True): wait for the entity state to change before
          returning. Only applies to state-changing services on a single entity.
        - **Result compaction (default ON)**: ``result`` is trimmed
          to the targeted entity's record (drops parent-group propagation) and
          stripped of ``context`` / ``last_*`` metadata and heavy attribute
          lists (``effect_list``, ``hue_scenes``). Escape hatches: ``verbose=True``
          for the raw HA response, or ``result_fields`` / ``result_attribute_keys``
          for explicit per-record projection (mirrors ``ha_get_state``).

        **For detailed service documentation, use ha_get_skill_guide.**

        Common patterns: Use ha_get_state() to check current values before making changes.
        Use ha_search() to find correct entity IDs.

        **WebSocket command escape hatch (advanced):**
        A few Home Assistant operations are WebSocket-only commands, not
        registered services — most notably dismissing a Repairs issue. Pass
        ``ws_command`` (instead of domain/service) to send one, with its
        parameters in ``data``:
        ```python
        # Dismiss a repair (get domain/issue_id from ha_get_overview repairs
        # or ha_get_system_health include="repairs")
        ha_call_service(ws_command="repairs/ignore_issue",
                        data={"domain": "sun", "issue_id": "abc", "ignore": True})
        ```
        Only one-shot request/response commands are supported; streaming/two-phase
        and service-invoking commands are rejected, and the other service
        parameters (entity_id, return_response, etc.) don't apply.
        """
        # WebSocket-command escape hatch (issue #1839): reach one-shot WS
        # commands that aren't registered services (e.g. repairs/ignore_issue).
        if ws_command is not None:
            self._reject_incompatible_ws_params(
                entity_id,
                return_response,
                verbose,
                result_fields,
                result_attribute_keys,
            )
            return await self._call_ws_command(
                ws_command, data, domain=domain, service=service
            )

        # Service mode requires domain + service (optional at the signature level
        # only to make room for the ws_command escape hatch) and rejects the
        # reserved ha_mcp_tools domain.
        domain, service = self._validate_service_call_params(domain, service)
        try:
            service_data = self._parse_service_data(data, entity_id)

            return_response_bool = return_response
            wait_bool = wait
            verbose_bool = verbose
            parsed_result_fields, parsed_result_attribute_keys = (
                self._parse_result_projection_params(
                    result_fields, result_attribute_keys
                )
            )

            # Determine if we should wait for state change:
            # Only for state-changing services on a single entity, not for
            # trigger/reload/fire-and-forget services or services without entities.
            # This server-side decision (D6) also chooses whether to hand the
            # component wait+entity_ids: a non-state-changing call passes wait
            # implicitly false and no confirmation targets.
            should_wait = (
                wait_bool
                and entity_id is not None
                and service in _STATE_CHANGING_SERVICES
                and domain not in _NON_STATE_CHANGING_DOMAINS
            )

            # Route a confirmable single call through the component capability (D8);
            # a returned response means it served the call, None means fall through to
            # the legacy REST path below (a safe first fire, D9 at-most-once).
            component_response = await self._maybe_component_call_service(
                domain=domain,
                service=service,
                service_data=service_data,
                entity_id=entity_id,
                data=data,
                should_wait=should_wait,
                return_response=return_response_bool,
                verbose=verbose_bool,
                fields=parsed_result_fields,
                attribute_keys=parsed_result_attribute_keys,
            )
            if component_response is not None:
                return component_response

            # Legacy REST path (component absent, or it never dispatched): capture
            # initial state before the call for the WS-subscribe verification.
            initial_state = None
            if should_wait:
                initial_state = await self._capture_initial_state(entity_id)

            result = await self._client.call_service(
                domain, service, service_data, return_response=return_response_bool
            )

            projected_result, projection_warnings = self._project_service_result(
                result,
                entity_id=entity_id,
                verbose=verbose_bool,
                fields=parsed_result_fields,
                attribute_keys=parsed_result_attribute_keys,
            )

            response: dict[str, Any] = {
                "success": True,
                "domain": domain,
                "service": service,
                "entity_id": entity_id,
                "parameters": data,
                "result": projected_result,
                "message": f"Successfully executed {domain}.{service}",
            }
            if projection_warnings:
                response.setdefault("warnings", []).extend(projection_warnings)

            # If return_response was requested, include the service_response key prominently
            if return_response_bool and isinstance(result, dict):
                response["service_response"] = result.get("service_response", result)

            # Wait for entity state to change
            if should_wait and entity_id is not None:
                await self._verify_state_change(
                    entity_id,
                    service,
                    initial_state,
                    response,
                )

            return response
        except HomeAssistantConnectionError as error:
            return self._handle_connection_error(
                error,
                domain=domain,
                service=service,
                entity_id=entity_id,
                data=data,
            )
        except ToolError:
            raise
        except Exception as error:
            self._raise_unexpected_call_service_error(
                error, domain=domain, service=service, entity_id=entity_id
            )
            return (
                None  # unreachable: _raise_unexpected_call_service_error always raises
            )

    @tool(
        name="ha_get_operation_status",
        tags={"Service & Device Control"},
        annotations={
            "openWorldHint": False,
            "readOnlyHint": True,
            "title": "Get Operation Status",
        },
    )
    @log_tool_usage
    async def ha_get_operation_status(
        self,
        operation_id: Annotated[
            str | list[str],
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Single operation ID or list of operation IDs to check. "
                    "Use a single string for one operation, or a list for bulk status checks."
                ),
            ),
        ],
        timeout_seconds: int = 10,
    ) -> dict[str, Any]:
        """
        Check status of one or more device operations with real-time WebSocket verification.

        Pass a single operation_id string to check one operation, or a list of IDs
        to check multiple operations at once (bulk status).

        The timeout_seconds parameter applies to single-operation checks only.
        Bulk checks poll each operation individually with a short internal timeout.

        Use this to track operations initiated by ha_bulk_control or ha_call_service.
        For current entity states, use ha_get_state instead.
        """
        try:
            # JSON_STRING_COERCION turns a '["op1","op2"]' string into a list
            # before the body runs, so operation_id is already the final shape.
            if isinstance(operation_id, list):
                result = await self._device_tools.get_bulk_operation_status(
                    operation_ids=operation_id
                )
                return cast(dict[str, Any], result)
            result = await self._device_tools.get_device_operation_status(
                operation_id=operation_id, timeout_seconds=timeout_seconds
            )
            return cast(dict[str, Any], result)
        except ToolError:
            raise
        except Exception as e:
            op_context: dict[str, Any] = {"operation_id": operation_id}
            exception_to_structured_error(
                e,
                context=op_context,
                suggestions=[
                    "Verify the operation ID(s) are valid",
                    "Use ha_get_state() to check current entity states instead",
                ],
            )
            return None  # unreachable: exception_to_structured_error always raises

    @tool(
        name="ha_bulk_control",
        tags={"Service & Device Control"},
        annotations={
            "openWorldHint": False,
            "destructiveHint": True,
            "title": "Bulk Control",
        },
    )
    @log_tool_usage
    async def ha_bulk_control(
        self,
        operations: Annotated[list[dict[str, Any]], JSON_STRING_COERCION],
        parallel: bool = True,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Control multiple devices with bulk operation support and WebSocket tracking."""
        parallel_bool = parallel

        # FastMCP validates operations as list[dict] before this runs.
        # parse_json_param is kept as a defensive passthrough for the list case.
        try:
            parsed_operations = parse_json_param(operations, "operations")
        except ValueError as e:
            raise_tool_error(
                create_validation_error(
                    f"Invalid operations parameter: {e}",
                    parameter="operations",
                    invalid_json=True,
                )
            )

        if not isinstance(parsed_operations, list):
            raise_tool_error(
                create_validation_error(
                    "Operations parameter must be a list",
                    parameter="operations",
                    details=f"Received type: {type(parsed_operations).__name__}",
                )
            )

        operations_list = cast(list[dict[str, Any]], parsed_operations)
        result = await self._device_tools.bulk_device_control(
            operations=operations_list, parallel=parallel_bool, ctx=ctx
        )
        return cast(dict[str, Any], result)

    @tool(
        name="ha_call_event",
        tags={"Service & Device Control"},
        annotations={
            "openWorldHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "title": "Call Event",
        },
    )
    @log_tool_usage
    async def ha_call_event(
        self,
        event_type: str,
        data: Annotated[dict[str, Any] | None, JSON_STRING_COERCION] = None,
    ) -> dict[str, Any]:
        """Execute a custom event on the Home Assistant event bus.

        When NOT to use: for controlling entities (lights, switches, climate) — use
        ha_call_service instead. For triggering automations by name, use
        ha_call_service("automation", "trigger").

        Use this to publish custom event types consumed by event-triggered automations,
        Node-RED flows, or custom integrations that subscribe to specific event types.

        Caveats: Events are fire-and-forget; this tool confirms the event was accepted
        by the bus but does not verify whether any automation or subscriber acted on it.
        """
        # Validate event_type before hitting the wire — empty strings or path separators
        # produce malformed URLs at POST /api/events/{event_type}.
        if not event_type or not event_type.strip():
            raise_tool_error(
                create_validation_error(
                    "event_type cannot be empty or whitespace",
                    parameter="event_type",
                )
            )
        if "/" in event_type or "\\" in event_type:
            raise_tool_error(
                create_validation_error(
                    "event_type cannot contain path separators",
                    parameter="event_type",
                    details=f"Received: {event_type!r}",
                )
            )

        parsed_data = _parse_event_data(data)

        try:
            response = await self._client.fire_event(event_type, parsed_data)
        except HomeAssistantConnectionError as error:
            if isinstance(error.__cause__, httpx.TimeoutException):
                return {
                    "success": True,
                    "partial": True,
                    "event_type": event_type,
                    "message": (
                        f"Event {event_type} was dispatched but Home Assistant "
                        "did not respond within the timeout period."
                    ),
                    "warnings": [
                        "Response timed out. The event was dispatched and may still "
                        "have been delivered to subscribers."
                    ],
                }
            exception_to_structured_error(
                error,
                context={"event_type": event_type},
                suggestions=["Check Home Assistant connection"],
            )
        except ToolError:
            raise
        except Exception as e:
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
        }


def register_service_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register service call and operation monitoring tools with the MCP server."""
    device_tools = kwargs.get("device_tools")
    if not device_tools:
        raise ValueError("device_tools is required for service tools registration")
    register_tool_methods(mcp, ServiceTools(client, device_tools))
