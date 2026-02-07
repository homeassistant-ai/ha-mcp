"""
Service call and device operation tools for Home Assistant MCP server.

This module provides service execution and WebSocket-enabled operation monitoring tools.
"""

import logging
from typing import Any, cast

import httpx

from ..errors import (
    create_validation_error,
)
from ..client.rest_client import HomeAssistantConnectionError
from .helpers import exception_to_structured_error, log_tool_usage
from .util_helpers import coerce_bool_param, parse_json_param


def _build_service_suggestions(domain: str, service: str, entity_id: str | None) -> list[str]:
    """Build common error suggestions for service call failures."""
    return [
        f"Verify {entity_id} exists using ha_get_state()" if entity_id else "Specify an entity_id for targeted service calls",
        f"Check available services for {domain} domain using ha_get_domain_docs()",
        "Use ha_search_entities() to find correct entity IDs",
    ]


def register_service_tools(mcp, client, **kwargs):
    """Register service call and operation monitoring tools with the MCP server."""
    device_tools = kwargs.get("device_tools")
    if not device_tools:
        raise ValueError("device_tools is required for service tools registration")

    @mcp.tool(annotations={"destructiveHint": True, "title": "Call Service"})
    @log_tool_usage
    async def ha_call_service(
        domain: str,
        service: str,
        entity_id: str | None = None,
        data: str | dict[str, Any] | None = None,
        return_response: bool | str = False,
        wait: bool | str = True,
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
        - **domain**: Service domain (light, climate, automation, etc.)
        - **service**: Service name (turn_on, set_temperature, trigger, etc.)
        - **entity_id**: Optional target entity. For some services (e.g., light.turn_off), omitting this targets all entities in the domain
        - **data**: Optional dict of service-specific parameters
        - **return_response**: Set to True for services that return data
        - **wait**: Wait for the entity state to change after the service call (default: True).
          Only applies to state-changing services on a single entity. Set to False for
          fire-and-forget calls, bulk operations, or services without observable state changes.

        **For detailed service documentation and parameters, use ha_get_domain_docs(domain).**

        Common patterns: Use ha_get_state() to check current values before making changes.
        Use ha_search_entities() to find correct entity IDs.
        """
        try:
            # Parse JSON data if provided as string
            try:
                parsed_data = parse_json_param(data, "data")
            except ValueError as e:
                return create_validation_error(
                    f"Invalid data parameter: {e}",
                    parameter="data",
                    invalid_json=True,
                )

            # Ensure service_data is a dict
            service_data: dict[str, Any] = {}
            if parsed_data is not None:
                if isinstance(parsed_data, dict):
                    service_data = parsed_data
                else:
                    return create_validation_error(
                        "Data parameter must be a JSON object",
                        parameter="data",
                        details=f"Received type: {type(parsed_data).__name__}",
                    )

            if entity_id:
                service_data["entity_id"] = entity_id

            # Coerce return_response boolean parameter
            return_response_bool = coerce_bool_param(return_response, "return_response", default=False) or False
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
                    logger.debug(f"Could not fetch initial state for {entity_id}: {e} — state verification may be degraded")

            result = await client.call_service(domain, service, service_data, return_response=return_response_bool)

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
                        client, entity_id, expected_state=expected,
                        initial_state=initial_state, timeout=10.0,
                    )
                    if new_state:
                        response["verified_state"] = new_state.get("state")
                    else:
                        response["warning"] = "Service executed but state change could not be verified within timeout."
                except Exception as e:
                    response["warning"] = f"Service executed but state verification failed: {e}"

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
            error_response = exception_to_structured_error(
                error,
                context={
                    "domain": domain,
                    "service": service,
                    "entity_id": entity_id,
                },
            )
            if "error" in error_response and isinstance(error_response["error"], dict):
                error_response["error"]["suggestions"] = _build_service_suggestions(domain, service, entity_id)
            return error_response
        except Exception as error:
            # Use structured error response
            error_response = exception_to_structured_error(
                error,
                context={
                    "domain": domain,
                    "service": service,
                    "entity_id": entity_id,
                },
            )
            suggestions = _build_service_suggestions(domain, service, entity_id)
            if entity_id:
                suggestions.extend([
                    f"For automation: ha_call_service('automation', 'trigger', entity_id='{entity_id}')",
                    f"For universal control: ha_call_service('homeassistant', 'toggle', entity_id='{entity_id}')",
                ])
            # Merge suggestions into error response
            if "error" in error_response and isinstance(error_response["error"], dict):
                error_response["error"]["suggestions"] = suggestions
            return error_response

    @mcp.tool(annotations={"readOnlyHint": True, "title": "Get Operation Status"})
    @log_tool_usage
    async def ha_get_operation_status(
        operation_id: str, timeout_seconds: int = 10
    ) -> dict[str, Any]:
        """Check status of device operation with real-time WebSocket verification."""
        result = await device_tools.get_device_operation_status(
            operation_id=operation_id, timeout_seconds=timeout_seconds
        )
        return cast(dict[str, Any], result)

    @mcp.tool(annotations={"destructiveHint": True, "title": "Bulk Control"})
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
            return create_validation_error(
                f"Invalid operations parameter: {e}",
                parameter="operations",
                invalid_json=True,
            )

        # Ensure operations is a list of dicts
        if parsed_operations is None or not isinstance(parsed_operations, list):
            return create_validation_error(
                "Operations parameter must be a list",
                parameter="operations",
                details=f"Received type: {type(parsed_operations).__name__}",
            )

        operations_list = cast(list[dict[str, Any]], parsed_operations)
        result = await device_tools.bulk_device_control(
            operations=operations_list, parallel=parallel_bool
        )
        return cast(dict[str, Any], result)

    @mcp.tool(annotations={"readOnlyHint": True, "title": "Get Bulk Operation Status"})
    @log_tool_usage
    async def ha_get_bulk_status(operation_ids: list[str]) -> dict[str, Any]:
        """
        Check status of multiple device control operations.

        Use this tool to check the status of operations initiated by ha_bulk_control
        or control_device_smart. Each of these tools returns unique operation_ids
        that can be tracked here.

        **IMPORTANT:** This tool is for tracking async device operations, NOT for
        checking current entity states. To get current states of entities, use
        ha_get_state instead.

        **Args:**
            operation_ids: List of operation IDs returned by ha_bulk_control or
                          control_device_smart (e.g., ["op_1234", "op_5678"])

        **Returns:**
            Status summary with completion/pending/failed counts and detailed
            results for each operation.

        **Example:**
            # After calling control_device_smart
            result = control_device_smart("light.kitchen", "on")
            op_id = result["operation_id"]  # e.g., "op_1234"

            # Check operation status
            status = ha_get_bulk_status([op_id])
        """
        result = await device_tools.get_bulk_operation_status(
            operation_ids=operation_ids
        )
        return cast(dict[str, Any], result)
