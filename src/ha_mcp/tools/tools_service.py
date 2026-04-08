"""
Service call and device operation tools for Home Assistant MCP server.

This module provides service execution and WebSocket-enabled operation monitoring tools.
"""

import logging
from typing import Annotated, Any, cast

import httpx
from fastmcp.exceptions import ToolError
from pydantic import Field

from ..client.rest_client import HomeAssistantConnectionError
from ..errors import (
    create_validation_error,
)
from .helpers import exception_to_structured_error, log_tool_usage, raise_tool_error
from .util_helpers import coerce_bool_param, parse_json_param, wait_for_state_change

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
    "set_speed",
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

# Domains where service calls don't produce entity state changes
_NON_STATE_CHANGING_DOMAINS = {
    "automation",
    "script",
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


def _build_service_suggestions(
    domain: str, service: str, entity_id: str | None
) -> list[str]:
    """Build common error suggestions for service call failures."""
    return [
        f"Verify {entity_id} exists using ha_get_state()"
        if entity_id
        else "Specify an entity_id for targeted service calls",
        f"Check available services for {domain} domain using ha_get_skill_home_assistant_best_practices",
        "Use ha_search_entities() to find correct entity IDs",
    ]


def register_service_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register service call and operation monitoring tools with the MCP server."""
    device_tools = kwargs.get("device_tools")
    if not device_tools:
        raise ValueError("device_tools is required for service tools registration")

    @mcp.tool(tags={"Service & Device Control"}, annotations={"destructiveHint": True, "title": "Call Service"})
    @log_tool_usage
    async def ha_call_service(
        domain: str | None = None,
        service: str | None = None,
        entity_id: str | None = None,
        data: str | dict[str, Any] | None = None,
        return_response: bool | str = False,
        wait: bool | str = True,
        intent: str | None = None,
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

        **Parameters:**
        - **domain**: Service domain (light, climate, automation, etc.). Not needed when using intent.
        - **service**: Service name (turn_on, set_temperature, trigger, etc.). Not needed when using intent.
        - **entity_id**: Optional target entity. For some services (e.g., light.turn_off), omitting this targets all entities in the domain
        - **data**: Optional dict of service-specific parameters or intent data
        - **return_response**: Set to True for services that return data
        - **wait**: Wait for the entity state to change after the service call (default: True).
          Only applies to state-changing services on a single entity. Set to False for
          fire-and-forget calls, bulk operations, or services without observable state changes.
        - **intent**: HA intent name (e.g., 'HassMediaSearch', 'HassMediaPause'). When set,
          domain and service are ignored and the call is routed to POST /api/intent/handle.
          Use for media intents that lack direct service equivalents. Requires the
          ``conversation`` integration (enabled by default in HA 2023.2+).

        **Intent examples (use when no direct service equivalent exists):**
        ```python
        # Search for media — no ha_call_service equivalent
        ha_call_service(intent="HassMediaSearch",
                        data={"media_type": "music", "search_term": "jazz"})

        # Pause/resume via intent (resolves the active media player automatically)
        ha_call_service(intent="HassMediaPause")
        ha_call_service(intent="HassMediaUnpause")
        ```

        **For detailed service documentation, use ha_get_skill_home_assistant_best_practices.**

        Common patterns: Use ha_get_state() to check current values before making changes.
        Use ha_search_entities() to find correct entity IDs.
        """
        try:
            # --- Intent routing ---
            if intent is not None:
                parsed_intent_data = parse_json_param(data, "data") if data is not None else None
                intent_data = parsed_intent_data if isinstance(parsed_intent_data, dict) else {}
                result = await client.call_intent(intent, intent_data or None)
                response_block = result.get("response", {})
                speech = response_block.get("speech", {}).get("plain", {}).get("speech", "")
                return {
                    "success": True,
                    "intent": intent,
                    "response_type": response_block.get("response_type"),
                    "speech": speech,
                    "raw": result,
                }

            # --- Normal service routing ---
            # Validate required parameters for service calls
            if domain is None or service is None:
                raise_tool_error(
                    create_validation_error(
                        "domain and service are required when intent is not set",
                        parameter="domain" if domain is None else "service",
                    )
                )
            # Mypy narrowing: after raise_tool_error (NoReturn), domain and service are str
            assert domain is not None
            assert service is not None

            # Parse JSON data if provided as string
            try:
                parsed_data = parse_json_param(data, "data")
            except ValueError as e:
                raise_tool_error(
                    create_validation_error(
                        f"Invalid data parameter: {e}",
                        parameter="data",
                        invalid_json=True,
                    )
                )

            # Ensure service_data is a dict
            service_data: dict[str, Any] = {}
            if parsed_data is not None:
                if isinstance(parsed_data, dict):
                    service_data = parsed_data
                else:
                    raise_tool_error(
                        create_validation_error(
                            "Data parameter must be a JSON object",
                            parameter="data",
                            details=f"Received type: {type(parsed_data).__name__}",
                        )
                    )

            if entity_id:
                service_data["entity_id"] = entity_id

            # Coerce return_response boolean parameter
            return_response_bool = (
                coerce_bool_param(return_response, "return_response", default=False)
                or False
            )
            wait_bool = coerce_bool_param(wait, "wait", default=True)

            # Determine if we should wait for state change:
            # Only for state-changing services on a single entity, not for
            # trigger/reload/fire-and-forget services or services without entities.
            should_wait = (
                wait_bool
                and entity_id is not None
                and service in _STATE_CHANGING_SERVICES
                and domain not in _NON_STATE_CHANGING_DOMAINS
            )

            # Capture initial state before the call
            initial_state = None
            if should_wait:
                try:
                    state_data = await client.get_entity_state(entity_id)
                    initial_state = state_data.get("state") if state_data else None
                except Exception as e:
                    logger.debug(
                        f"Could not fetch initial state for {entity_id}: {e} — state verification may be degraded"
                    )

            result = await client.call_service(
                domain, service, service_data, return_response=return_response_bool
            )

            response: dict[str, Any] = {
                "success": True,
                "domain": domain,
                "service": service,
                "entity_id": entity_id,
                "parameters": data,
                "result": result,
                "message": f"Successfully executed {domain}.{service}",
            }

            # If return_response was requested, include the service_response key prominently
            if return_response_bool and isinstance(result, dict):
                response["service_response"] = result.get("service_response", result)

            # Wait for entity state to change
            if should_wait and entity_id is not None:
                try:
                    expected = _SERVICE_TO_STATE.get(service)
                    new_state = await wait_for_state_change(
                        client,
                        entity_id,
                        expected_state=expected,
                        initial_state=initial_state,
                        timeout=10.0,
                    )
                    if new_state:
                        response["verified_state"] = new_state.get("state")
                    else:
                        response["warning"] = (
                            "Service executed but state change could not be verified within timeout."
                        )
                except Exception as e:
                    response["warning"] = (
                        f"Service executed but state verification failed: {e}"
                    )

            return response
        except HomeAssistantConnectionError as error:
            # Check if this is a timeout - for service calls, timeouts typically
            # mean the service was dispatched but HA didn't respond in time.
            # The operation is likely still running (e.g., update.install, long automations).
            if isinstance(error.__cause__, httpx.TimeoutException):
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
                    "warning": (
                        "Response timed out. This is normal for long-running services "
                        f"like updates or firmware installs. Use ha_get_state('{entity_id}') "
                        "to check the current status."
                        if entity_id
                        else "Response timed out. This is normal for long-running services. "
                        "The service was dispatched and may still be executing."
                    ),
                }
            # Non-timeout connection errors are real failures
            exception_to_structured_error(
                error,
                context={
                    "domain": domain,
                    "service": service,
                    "entity_id": entity_id,
                },
                suggestions=_build_service_suggestions(domain or "", service or "", entity_id),
            )
        except ToolError:
            raise
        except Exception as error:
            # Use structured error response
            suggestions = _build_service_suggestions(domain or "", service or "", entity_id)
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

    @mcp.tool(tags={"Service & Device Control"}, annotations={"readOnlyHint": True, "title": "Get Operation Status"})
    @log_tool_usage
    async def ha_get_operation_status(
        operation_id: Annotated[
            str | list[str],
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
            # Handle JSON string coercion (MCP clients may send '["op1","op2"]')
            resolved_id: str | list[str] = operation_id
            if isinstance(operation_id, str):
                try:
                    parsed = parse_json_param(operation_id, "operation_id")
                    if isinstance(parsed, list):
                        resolved_id = [str(item) for item in parsed]
                except ValueError:
                    pass  # Plain string — treat as single operation ID

            if isinstance(resolved_id, list):
                result = await device_tools.get_bulk_operation_status(
                    operation_ids=resolved_id
                )
                return cast(dict[str, Any], result)
            result = await device_tools.get_device_operation_status(
                operation_id=resolved_id, timeout_seconds=timeout_seconds
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

    @mcp.tool(tags={"Service & Device Control"}, annotations={"destructiveHint": True, "title": "Bulk Control"})
    @log_tool_usage
    async def ha_bulk_control(
        operations: str | list[dict[str, Any]], parallel: bool | str = True
    ) -> dict[str, Any]:
        """Control multiple devices with bulk operation support and WebSocket tracking."""
        # Coerce boolean parameter that may come as string from XML-style calls
        parallel_bool = coerce_bool_param(parallel, "parallel", default=True)
        assert parallel_bool is not None  # default=True guarantees non-None

        # Parse JSON operations if provided as string
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

        # Ensure operations is a list of dicts
        if parsed_operations is None or not isinstance(parsed_operations, list):
            raise_tool_error(
                create_validation_error(
                    "Operations parameter must be a list",
                    parameter="operations",
                    details=f"Received type: {type(parsed_operations).__name__}",
                )
            )

        operations_list = cast(list[dict[str, Any]], parsed_operations)
        result = await device_tools.bulk_device_control(
            operations=operations_list, parallel=parallel_bool
        )
        return cast(dict[str, Any], result)
