"""
Smart device control tools with async verification.

This module provides intelligent device control with domain-specific handling
and async operation verification through WebSocket monitoring.
"""

import asyncio
import json
import logging
import time
from typing import Any, ClassVar

from fastmcp import Context
from fastmcp.exceptions import ToolError

from ..client.rest_client import (
    HomeAssistantClient,
    HomeAssistantCommandError,
    HomeAssistantCommandNotSent,
)
from ..client.websocket_client import get_websocket_client
from ..client.websocket_listener import start_websocket_listener
from ..config import get_global_settings
from ..errors import ErrorCode, create_error_response
from ..utils.domain_handlers import get_domain_handler
from ..utils.operation_manager import (
    fail_pending_operation,
    get_operation_from_memory,
    store_pending_operation,
)
from .component_api import (
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)
from .helpers import (
    exception_to_structured_error,
    raise_tool_error,
    safe_info,
    safe_progress,
)
from .util_helpers import _SERVICE_TO_STATE

logger = logging.getLogger(__name__)

# The ha_mcp_tools/bulk_call_service WS command (Phase 3, D5a): the BATCH write
# capability. When the component advertises ``bulk_call_service`` the consumer
# resolves every op's domain/service server-side (D6), sends one register-before-fire
# batch frame, and maps the inline-confirmed per-op transitions back into the legacy
# bulk response shape — no operation-id polling needed. Named once so the routing
# helper and its tests agree on the wire string.
WS_BULK_CALL_SERVICE = "ha_mcp_tools/bulk_call_service"


class DeviceControlTools:
    """Smart device control tools with async verification."""

    def __init__(self, client: HomeAssistantClient | None = None):
        """Initialize device control tools."""
        # Only load settings if client not provided
        if client is None:
            self.settings = get_global_settings()
            self.client = HomeAssistantClient()
        else:
            self.settings = None  # type: ignore[assignment]
            self.client = client
        self._listener_started = False

    async def _ensure_websocket_listener(self) -> None:
        """Ensure WebSocket listener is running for async verification."""
        if not self._listener_started:
            try:
                success = await start_websocket_listener()
                if success:
                    self._listener_started = True
                    logger.info("WebSocket listener started for async verification")
                else:
                    logger.warning(
                        "Failed to start WebSocket listener - async verification disabled"
                    )
            except Exception as e:
                logger.error(f"Error starting WebSocket listener: {e}")

    async def control_device_smart(
        self,
        entity_id: str,
        action: str,
        parameters: dict[str, Any] | None = None,
        timeout_seconds: int = 10,
        validate_first: bool = True,
    ) -> dict[str, Any]:
        """
        Universal smart device control with async verification.

        This tool provides intelligent device control with domain-specific
        parameter handling and async operation verification via WebSocket.

        Args:
            entity_id: Target entity ID (e.g., 'light.living_room')
            action: Action to perform (on, off, toggle, set, etc.)
            parameters: Action-specific parameters (brightness, temperature, etc.)
            timeout_seconds: How long to wait for operation completion
            validate_first: Whether to validate entity exists before action

        Returns:
            Operation result with follow-up instructions for async checking
        """
        await self._ensure_websocket_listener()

        try:
            parameters = self._parse_parameters(parameters, entity_id, action)

            # Parse domain from entity ID
            if "." not in entity_id:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.ENTITY_INVALID_ID,
                        f"Invalid entity ID format: {entity_id}",
                        suggestions=[
                            "Entity ID must be in format 'domain.entity_name'",
                            "Use smart_entity_search to find correct entity ID",
                        ],
                        context={"entity_id": entity_id, "action": action},
                    )
                )

            domain = entity_id.split(".")[0]
            handler = get_domain_handler(domain)

            # Validate entity exists if requested
            current_state = None
            if validate_first:
                current_state = await self._validate_entity_exists(entity_id, action)

            # Validate action for domain
            valid_actions = handler.get("valid_actions", ["on", "off", "toggle"])
            if action not in valid_actions:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_INVALID_ACTION,
                        f"Invalid action '{action}' for domain '{domain}'",
                        suggestions=[
                            f"Valid actions for {domain}: {', '.join(valid_actions)}",
                            "Use 'toggle' for simple on/off control",
                        ],
                        context={
                            "entity_id": entity_id,
                            "action": action,
                            "valid_actions": valid_actions,
                        },
                    )
                )

            # Build service call
            service_call = self._build_service_call(
                entity_id, domain, action, parameters
            )

            # Predict expected state after operation
            expected_state = self._predict_expected_state(
                current_state if validate_first else None, action, parameters, domain
            )

            # Register the pending operation BEFORE dispatching the service so a
            # fast entity's state_changed event can't arrive (and be dropped for
            # having no matching op) in the window between the call returning and
            # the operation being stored. On a dispatch failure the operation is
            # flipped to FAILED so it can't be spuriously completed by a later,
            # unrelated event for the same entity.
            operation_id = store_pending_operation(
                entity_id=entity_id,
                action=action,
                service_domain=service_call["domain"],
                service_name=service_call["service"],
                service_data=service_call["data"],
                expected_state=expected_state,
                timeout_ms=timeout_seconds * 1000,
            )

            # Execute service call
            try:
                await self.client.call_service(
                    service_call["domain"],
                    service_call["service"],
                    service_call["data"],
                )

                return {
                    "entity_id": entity_id,
                    "action": action,
                    "parameters": parameters or {},
                    "command_sent": True,
                    "operation_id": operation_id,
                    "status": "pending_verification",
                    "message": f"Command sent to {entity_id}. Use get_device_operation_status() to verify completion.",
                    "service_call": service_call,
                    "expected_state": expected_state,
                    "timeout_seconds": timeout_seconds,
                    "follow_up": {
                        "tool": "get_device_operation_status",
                        "parameters": {
                            "operation_id": operation_id,
                            "timeout_seconds": timeout_seconds,
                        },
                    },
                }

            except ToolError:
                fail_pending_operation(operation_id, "Service dispatch failed")
                raise
            except Exception as e:
                fail_pending_operation(operation_id, f"Service dispatch failed: {e}")
                exception_to_structured_error(
                    e,
                    context={"entity_id": entity_id, "action": action},
                    suggestions=[
                        "Check if entity supports this action",
                        "Verify Home Assistant connection",
                        "Check Home Assistant logs for details",
                    ],
                )

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error in control_device_smart: {e}")
            exception_to_structured_error(
                e,
                context={"entity_id": entity_id, "action": action},
                suggestions=[
                    "Check entity ID format",
                    "Verify Home Assistant connection",
                    "Try simpler action like 'toggle'",
                ],
            )
            raise  # unreachable: exception_to_structured_error always raises
        return None  # py/mixed-returns: explicit terminal; error handlers above always raise (NoReturn), unreachable

    def _parse_parameters(
        self,
        parameters: dict[str, Any] | None,
        entity_id: str,
        action: str,
    ) -> dict[str, Any] | None:
        if parameters and isinstance(parameters, str):
            try:
                return json.loads(parameters)
            except json.JSONDecodeError:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_JSON,
                        f"Invalid JSON in parameters: {parameters}",
                        suggestions=[
                            "Parameters should be a valid JSON object",
                            "Example: {'brightness': 102, 'color_temp_kelvin': 4000}",
                        ],
                        context={"entity_id": entity_id, "action": action},
                    )
                )
        return parameters

    async def _validate_entity_exists(
        self,
        entity_id: str,
        action: str,
    ) -> dict[str, Any]:
        """Fetch entity state, raising ToolError if the entity does not exist."""
        try:
            current_state = await self.client.get_entity_state(entity_id)
            if not current_state:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.ENTITY_NOT_FOUND,
                        f"Entity not found: {entity_id}",
                        suggestions=[
                            "Use smart_entity_search to find the correct entity",
                            "Check entity is not disabled in Home Assistant",
                        ],
                        context={"entity_id": entity_id, "action": action},
                    )
                )
            return current_state
        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={"entity_id": entity_id, "action": action},
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify entity ID spelling",
                ],
            )
            raise  # unreachable; keeps type checker satisfied

    def _resolve_service_name(
        self,
        domain: str,
        action: str,
        parameters: dict[str, Any] | None,
    ) -> tuple[str, dict[str, Any] | None]:
        service_mapping = {
            "on": "turn_on",
            "off": "turn_off",
            "toggle": "toggle",
            "open": "open_cover" if domain == "cover" else "turn_on",
            "close": "close_cover" if domain == "cover" else "turn_off",
            "set": "turn_on" if domain == "light" else "set_temperature",
        }

        service_name = service_mapping.get(action, action)

        if domain == "climate":
            if action in ["heat", "cool", "auto"]:
                service_name = "set_hvac_mode"
                if not parameters:
                    parameters = {}
                parameters["hvac_mode"] = action
            elif action == "set":
                service_name = "set_temperature"

        elif domain == "media_player":
            if action in ["play", "pause", "stop"]:
                service_name = f"media_{action}"
            elif action == "set":
                service_name = "volume_set"

        return service_name, parameters

    _DOMAIN_PARAMS: ClassVar[dict[str, list[str]]] = {
        "light": ["brightness", "color_temp_kelvin", "rgb_color", "effect"],
        "climate": ["temperature", "target_temp_high", "target_temp_low", "hvac_mode"],
        "cover": ["position", "tilt_position"],
        "media_player": ["volume_level", "media_content_id", "media_content_type"],
    }

    @staticmethod
    def _normalize_light_color_temp(parameters: dict[str, Any]) -> None:
        """Convert deprecated color temp parameters to color_temp_kelvin."""
        if "color_temp_kelvin" in parameters:
            return
        if "kelvin" in parameters:
            parameters["color_temp_kelvin"] = parameters.pop("kelvin")
        elif "color_temp" in parameters:
            mired_val = parameters.pop("color_temp")
            if isinstance(mired_val, (int, float)) and mired_val > 0:
                parameters["color_temp_kelvin"] = round(1_000_000 / mired_val)

    def _add_domain_params(
        self,
        domain: str,
        parameters: dict[str, Any],
        service_data: dict[str, Any],
    ) -> None:
        if domain == "light":
            self._normalize_light_color_temp(parameters)

        allowed = self._DOMAIN_PARAMS.get(domain, [])
        for param in allowed:
            if param in parameters:
                service_data[param] = parameters[param]

    def _build_service_call(
        self,
        entity_id: str,
        domain: str,
        action: str,
        parameters: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Build Home Assistant service call from action and parameters."""
        service_name, parameters = self._resolve_service_name(
            domain, action, parameters
        )

        service_data: dict[str, Any] = {"entity_id": entity_id}

        if parameters:
            self._add_domain_params(domain, parameters, service_data)

        # Remove None values
        service_data = {k: v for k, v in service_data.items() if v is not None}

        return {"domain": domain, "service": service_name, "data": service_data}

    def _predict_state_from_action(
        self,
        action: str,
        current_state: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        expected: dict[str, Any] = {}
        if action == "on":
            expected["state"] = "on"
        elif action == "off":
            expected["state"] = "off"
        elif action == "toggle":
            if current_state:
                current = current_state.get("state", "off")
                expected["state"] = "off" if current == "on" else "on"
            else:
                return None
        elif action == "open":
            expected["state"] = "open"
        elif action == "close":
            expected["state"] = "closed"
        return expected

    def _predict_attributes_from_params(
        self,
        domain: str,
        action: str,
        parameters: dict[str, Any],
        expected: dict[str, Any],
    ) -> None:
        if domain == "light" and action in ["on", "set"]:
            if "brightness" in parameters:
                expected["brightness"] = parameters["brightness"]
            if "color_temp_kelvin" in parameters:
                expected["color_temp_kelvin"] = parameters["color_temp_kelvin"]

        elif domain == "climate" and action in ["set", "heat", "cool", "auto"]:
            if "temperature" in parameters:
                expected["temperature"] = parameters["temperature"]
            if "hvac_mode" in parameters:
                expected["hvac_mode"] = parameters["hvac_mode"]
            elif action in ["heat", "cool", "auto"]:
                expected["hvac_mode"] = action

    def _predict_expected_state(
        self,
        current_state: dict[str, Any] | None,
        action: str,
        parameters: dict[str, Any] | None,
        domain: str,
    ) -> dict[str, Any] | None:
        """Predict expected entity state after operation."""
        expected = self._predict_state_from_action(action, current_state)
        if expected is None:
            return None

        if parameters:
            self._predict_attributes_from_params(domain, action, parameters, expected)

        return expected if expected else None

    async def get_device_operation_status(
        self, operation_id: str, timeout_seconds: int = 10
    ) -> dict[str, Any]:
        """Check status of a device operation, waiting up to ``timeout_seconds`` for completion.

        Polls the in-memory operation registry (mutated by the WebSocket
        listener as state changes arrive) every 0.2s while the operation is
        pending, up to ``timeout_seconds``. Returns the final structured status
        — completed/failed/timeout/pending — produced by
        ``control_device_smart``.
        """
        operation = get_operation_from_memory(operation_id)

        if not operation:
            raise_tool_error(
                create_error_response(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    "Operation not found or expired",
                    suggestions=[
                        "Operation may have been cleaned up after completion",
                        "Check operation ID spelling",
                        "Use control_device_smart to start new operation",
                    ],
                    context={"operation_id": operation_id},
                )
            )

        # Wait up to timeout_seconds for the operation to leave the pending state.
        # The WebSocket listener mutates operation.status as state changes arrive,
        # so polling memory is sufficient — no need to subscribe again. Uses
        # time.monotonic() so the deadline can be cleanly patched in tests.
        if operation.status.value == "pending" and timeout_seconds > 0:
            deadline = time.monotonic() + timeout_seconds
            while operation.status.value == "pending":
                if time.monotonic() >= deadline:
                    break
                await asyncio.sleep(0.2)
                refreshed = get_operation_from_memory(operation_id)
                if refreshed is None:
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.RESOURCE_NOT_FOUND,
                            "Operation cleaned up during status poll",
                            suggestions=[
                                "Operation may have completed and been purged before "
                                + "verification finished",
                                "Use control_device_smart to start new operation",
                            ],
                            context={"operation_id": operation_id},
                        )
                    )
                operation = refreshed

        # Check operation status
        if operation.status.value == "completed":
            return {
                "operation_id": operation_id,
                "status": "completed",
                "success": True,
                "entity_id": operation.entity_id,
                "action": operation.action,
                "final_state": operation.result_state,
                "duration_ms": operation.duration_ms,
                "message": f"Device {operation.entity_id} successfully {operation.action}",
                "verification_method": "websocket_state_change",
                "details": {
                    "service_call": {
                        "domain": operation.service_domain,
                        "service": operation.service_name,
                        "data": operation.service_data,
                    },
                    "expected_state": operation.expected_state,
                    "actual_state": operation.result_state,
                },
            }

        elif operation.status.value == "failed":
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    operation.error_message or "Device operation failed",
                    context={
                        "operation_id": operation_id,
                        "entity_id": operation.entity_id,
                        "action": operation.action,
                        "duration_ms": operation.duration_ms,
                    },
                    suggestions=[
                        "Check if device is available and responding",
                        "Verify device supports the requested action",
                        "Check Home Assistant logs for error details",
                        "Try a simpler action like toggle",
                    ],
                )
            )

        elif operation.status.value == "timeout":
            raise_tool_error(
                create_error_response(
                    ErrorCode.TIMEOUT_OPERATION,
                    f"Operation timed out after {operation.timeout_ms}ms",
                    context={
                        "operation_id": operation_id,
                        "entity_id": operation.entity_id,
                        "action": operation.action,
                        "elapsed_ms": operation.elapsed_ms,
                    },
                    suggestions=[
                        "Device may be slow to respond or offline",
                        "Check device connectivity",
                        "Try increasing timeout for slow devices",
                        "Verify device is powered on",
                    ],
                )
            )

        else:  # pending
            return {
                "operation_id": operation_id,
                "status": "pending",
                "entity_id": operation.entity_id,
                "action": operation.action,
                "elapsed_ms": operation.elapsed_ms,
                "timeout_in_ms": operation.timeout_ms,
                "time_remaining_ms": operation.timeout_ms - operation.elapsed_ms,
                "message": f"Waiting for {operation.entity_id} to respond to {operation.action}...",
                "expected_state": operation.expected_state,
                "monitoring": "websocket_state_changes",
                "tips": [
                    "Operation will auto-complete when device state changes",
                    "Physical devices may take 1-3 seconds to respond",
                    "Call this function again to check for updates",
                ],
            }

    @staticmethod
    def _validate_bulk_operations(
        operations: list[dict[str, Any]],
        skipped_operations: list[dict[str, Any]],
    ) -> list[tuple[int, dict[str, Any], str, str]]:
        valid: list[tuple[int, dict[str, Any], str, str]] = []
        for i, op in enumerate(operations):
            if not isinstance(op, dict):
                error = f"Operation at index {i} is not a dict: {type(op).__name__}"
                logger.warning(f"Bulk control: {error}")
                err_response = create_error_response(
                    ErrorCode.VALIDATION_MISSING_PARAMETER, error, context={"index": i}
                )
                err_response["index"] = i
                err_response["operation"] = op
                skipped_operations.append(err_response)
                continue

            entity_id = op.get("entity_id")
            action = op.get("action")
            missing = [f for f in ("entity_id", "action") if not op.get(f)]

            if missing:
                error = f"Operation at index {i} missing required fields: {', '.join(missing)}"
                logger.warning(f"Bulk control: {error}")
                err_response = create_error_response(
                    ErrorCode.VALIDATION_MISSING_PARAMETER, error, context={"index": i}
                )
                err_response["index"] = i
                err_response["operation"] = op
                skipped_operations.append(err_response)
            else:
                valid.append((i, op, str(entity_id), str(action)))
        return valid

    async def bulk_device_control(
        self,
        operations: list[dict[str, Any]],
        parallel: bool = True,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """
        Control multiple devices with bulk operation support.

        Args:
            operations: List of device control operations
            parallel: Whether to execute operations in parallel

        Returns:
            Bulk operation results
        """
        if not operations:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_MISSING_PARAMETER,
                    "No operations provided",
                    suggestions=["Provide a list of device control operations"],
                    context={"results": []},
                )
            )

        results: list[dict[str, Any]] = []
        operation_ids: list[str] = []
        skipped_operations: list[dict[str, Any]] = []

        try:
            valid_operations = self._validate_bulk_operations(
                operations, skipped_operations
            )

            await safe_info(
                ctx,
                f"bulk_device_control: {len(valid_operations)} valid op(s), "
                f"{len(skipped_operations)} skipped, "
                f"mode={'parallel' if parallel else 'sequential'}",
            )

            # Route through the component's bulk_call_service capability when
            # advertised (D5a): one register-before-fire batch frame confirms every
            # op inline, so ops return already-verified (no operation-id polling). A
            # None return means nothing dispatched (capability miss / unresolvable op
            # / transport failure), so the legacy path below is a safe first fire
            # (D9 at-most-once).
            component_response = await self._bulk_via_component(
                operations, valid_operations, skipped_operations, parallel
            )
            if component_response is not None:
                # M-progress-msg: the ambiguous batch response carries a top-level
                # ``partial`` (dispatched but the batch confirmation never arrived — not
                # retried); only a fully-served batch is "confirmed inline". The legacy
                # ``_build_bulk_response`` never sets a top-level ``partial``, so this
                # flag reliably marks the ambiguous path.
                component_message = (
                    "dispatched via component (unconfirmed — not retried)"
                    if component_response.get("partial")
                    else "dispatched via component (confirmed inline)"
                )
                await safe_progress(
                    ctx,
                    progress=len(valid_operations),
                    total=len(valid_operations),
                    message=component_message,
                )
                return component_response

            await safe_progress(
                ctx,
                progress=0,
                total=len(valid_operations),
                message="dispatching operations",
            )

            # Execute only valid operations
            if parallel:
                await self._execute_parallel(valid_operations, results, operation_ids)
            else:
                await self._execute_sequential(
                    valid_operations, results, operation_ids, ctx=ctx
                )

            await safe_progress(
                ctx,
                progress=len(valid_operations),
                total=len(valid_operations),
                message=(
                    f"dispatched {len(operation_ids)} op(s); "
                    "use get_bulk_operation_status to verify completion"
                ),
            )

            return self._build_bulk_response(
                operations, results, operation_ids, skipped_operations, parallel
            )

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error in bulk_device_control: {e}")
            exception_to_structured_error(
                e,
                context={"results": results},
                suggestions=["Check operation parameters and try again"],
            )
            raise  # unreachable: exception_to_structured_error always raises

    def _resolve_component_op(
        self,
        entity_id: str,
        action: str,
        parameters: Any,
    ) -> dict[str, Any] | None:
        """Resolve one bulk op into a fully-formed component operation row.

        Verb resolution stays server-side (D6): reuses the SAME
        ``get_domain_handler`` / ``_resolve_service_name`` / ``_build_service_call``
        the legacy path uses, so the component receives only a fully-resolved
        ``{domain, service, service_data, entity_ids, expected_state}`` row and never
        guesses a service name. ``entity_ids`` is the confirmation target;
        ``service_data`` already folds in the entity_id so the dispatch targets it;
        ``expected_state`` is the per-op confirmation hint (``_SERVICE_TO_STATE``).

        Returns ``None`` for any op the legacy path would reject with a structured
        per-op error (a malformed entity_id, an action outside the domain handler, or
        unparseable parameters). A ``None`` aborts the WHOLE batch back to the legacy
        path (nothing has dispatched yet), which then produces the proper per-op error
        — cleaner than half-resolving a batch.
        """
        try:
            if "." not in entity_id:
                return None
            domain = entity_id.split(".")[0]
            handler = get_domain_handler(domain)
            valid_actions = handler.get("valid_actions", ["on", "off", "toggle"])
            if action not in valid_actions:
                return None
            parsed = self._parse_parameters(parameters, entity_id, action)
            service_call = self._build_service_call(entity_id, domain, action, parsed)
        except Exception:
            # Any resolution failure (incl. a ToolError from _parse_parameters on
            # invalid JSON) → abort the batch to legacy, which surfaces the error.
            return None
        return {
            "domain": service_call["domain"],
            "service": service_call["service"],
            "service_data": service_call["data"],
            "entity_ids": [entity_id],
            # Confirmation HINT (see util_helpers._SERVICE_TO_STATE): the expected
            # primary state after this op's service, or None. The component confirms
            # only on REACHING it (skipping intermediate/noise events) and immediate-
            # matches an idempotent no-op; a None hint keeps any-first-event behavior.
            "expected_state": _SERVICE_TO_STATE.get(service_call["service"]),
        }

    def _map_component_op_result(
        self,
        op: dict[str, Any],
        entity_id: str,
        action: str,
        row: dict[str, Any],
        component_op: dict[str, Any],
    ) -> dict[str, Any]:
        """Map one component op result into the legacy per-op result shape.

        The component confirms inline, so the op returns already-verified with a
        synthesized terminal status instead of the legacy ``pending_verification`` +
        operation-id handle. A ``command_sent: True`` result is counted ``successful``
        by ``_build_bulk_response`` exactly as the legacy dispatch result is; an op
        whose ``async_call`` raised under the batch (``error`` set / not dispatched)
        maps to a structured ``SERVICE_CALL_FAILED`` error and is NOT re-dispatched
        (D9). When ``validate_first`` (the default) is set and the target's captured
        pre-state is null (the entity does not exist), the op maps to a structured
        ``ENTITY_NOT_FOUND`` failure — parity with the legacy per-op validation.
        """
        service_call = {
            "domain": row["domain"],
            "service": row["service"],
            "data": row["service_data"],
        }
        if component_op.get("error") is not None or not component_op.get("dispatched"):
            err = create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                str(component_op.get("error") or "Operation failed to dispatch"),
                context={"entity_id": entity_id, "action": action},
            )
            err["service_call"] = service_call
            return err
        transitions = component_op.get("transitions") or []
        # I3: honor validate_first (default). The component captured each target's
        # pre-state; a null old_state on a confirmable op means the entity does not
        # exist (HA no-ops the dispatch, then the wait stalls to partial). The legacy
        # path returns a structured ENTITY_NOT_FOUND per-op failure — match it from
        # the pre-state already in the transition (no extra hops), rather than
        # counting a phantom entity as a successful command. Reachability is
        # drift-bounded: a missing transition row from a length-drifted batch now routes
        # to the ambiguous path (I2) before reaching here, so a null old_state here
        # means a genuinely absent entity, not a dropped row.
        if op.get("validate_first", True) and row.get("entity_ids"):
            old_state = next(
                (
                    t.get("old_state")
                    for t in transitions
                    if isinstance(t, dict) and t.get("entity_id") == entity_id
                ),
                None,
            )
            if old_state is None:
                err = create_error_response(
                    ErrorCode.ENTITY_NOT_FOUND,
                    f"Entity not found: {entity_id}",
                    suggestions=[
                        "Use ha_search to find the correct entity",
                        "Check the entity is not disabled in Home Assistant",
                    ],
                    context={"entity_id": entity_id, "action": action},
                )
                err["service_call"] = service_call
                return err
        final_state = next(
            (
                t["new_state"].get("state")
                for t in transitions
                if isinstance(t, dict) and isinstance(t.get("new_state"), dict)
            ),
            None,
        )
        confirmed = bool(component_op.get("confirmed"))
        partial = bool(component_op.get("partial"))
        return {
            "entity_id": entity_id,
            "action": action,
            "parameters": op.get("parameters") or {},
            "command_sent": True,
            # M-status: an inline-confirmed op has no operation_id, so a
            # "pending_verification" status is misleading (nothing will poll it). Use
            # "dispatched_unconfirmed" — the same label the ambiguous batch path uses.
            "status": "completed" if confirmed else "dispatched_unconfirmed",
            "confirmed": confirmed,
            "partial": partial,
            "final_state": final_state,
            "transitions": transitions,
            "service_call": service_call,
            "verification_method": "component_state_change",
            "message": (
                f"{entity_id} {action} " + ("confirmed" if confirmed else "dispatched")
            ),
        }

    def _resolve_component_rows(
        self,
        valid_operations: list[tuple[int, dict[str, Any], str, str]],
    ) -> list[dict[str, Any]] | None:
        """Resolve every valid op into a component row (server-side, D6).

        Returns ``None`` — abort the WHOLE batch to legacy BEFORE any dispatch — when
        any op cannot be resolved (the legacy path then surfaces the proper per-op
        error, and no partial batch lands) or when nothing resolvable remains (an
        empty batch; the component's schema rejects it anyway).
        """
        rows: list[dict[str, Any]] = []
        for _idx, op, entity_id, action in valid_operations:
            row = self._resolve_component_op(entity_id, action, op.get("parameters"))
            if row is None:
                return None
            rows.append(row)
        return rows or None

    async def _bulk_via_component(
        self,
        operations: list[dict[str, Any]],
        valid_operations: list[tuple[int, dict[str, Any], str, str]],
        skipped_operations: list[dict[str, Any]],
        parallel: bool,
    ) -> dict[str, Any] | None:
        """Route the batch through the component ``bulk_call_service`` capability.

        Returns a legacy-shaped bulk response (built by the SAME
        ``_build_bulk_response`` the legacy path uses, with ``operation_ids`` empty
        and ``follow_up`` None since ops are confirmed inline) when the component
        advertises ``bulk_call_service`` and the frame lands, else ``None`` so the
        caller runs the unchanged legacy operation-registry path.

        **D9 (at-most-once, per batch).** The boundary is PRE-SEND vs POST-SEND:

        * ``None`` (nothing dispatched → safe legacy first fire): a capability miss,
          an op that could not be resolved server-side, an empty batch, a batch whose
          entity_ids are not all distinct (the component's per-entity waiter cannot
          confirm a repeated entity), an ``unknown_command`` (caps invalidated), a
          connection-ESTABLISHMENT failure, a ``HomeAssistantCommandNotSent`` (the
          frame provably never left the process — the send_command readiness guard),
          or a command-ERROR response. A command error is pre-dispatch because the
          batch's all-guards-first pass (D1 / ServiceNotFound) raises before ANY
          ``async_call`` AND the component's post-dispatch assembly is TOTAL (never
          raises — I1), so no landed write can surface as a command error.
        * A returned result means the batch dispatched: each op's own ``dispatched``
          flag is authoritative and NOTHING is re-dispatched, even the ops that failed.
        * A response-wait TIMEOUT, a post-send transport drop, OR a malformed/unusable
          SUCCESS envelope (non-dict result or op-list length drift) is AMBIGUOUS for
          the WHOLE batch frame (some/all ops may have landed; a success frame is built
          only AFTER every ``async_call`` fired, and ``async_call`` under the batch is
          unbounded). It returns a partial batch response — every op reported
          dispatched-but-unconfirmed — and is NEVER re-dispatched via legacy (a
          re-dispatch would double-fire every landed op, inverting ``toggle`` ops).
        """
        caps = await get_component_caps(self.client)
        if not component_supports(caps, "bulk_call_service"):
            return None

        rows = self._resolve_component_rows(valid_operations)
        # PRE-SEND, route the WHOLE batch to legacy (nothing dispatched yet → safe first
        # fire, D9) when either: an op could not be resolved server-side (``rows`` None),
        # or the batch targets the same entity_id twice. The component keys ONE
        # entity-keyed transition waiter per entity, so the first ``state_changed``
        # satisfies every op for that entity — both would report the same
        # transition/final_state (e.g. `light.a on` then `light.a off` both read as the
        # first). The legacy sequential path confirms each op independently.
        if rows is None or self._has_duplicate_entity_ids(valid_operations):
            return None

        # PRE-SEND: an establishment failure means the batch frame provably never
        # reached the component → legacy is a safe first fire. Split from the send
        # below so a POST-SEND failure is never misclassified as pre-send.
        try:
            ws = await get_websocket_client(
                url=self.client.base_url, token=self.client.token
            )
        except Exception as exc:
            logger.warning(
                "%s establishment failed; falling back to legacy: %r",
                WS_BULK_CALL_SERVICE,
                exc,
            )
            return None
        # ``send_command`` transmits the batch frame INSIDE itself, AFTER its readiness
        # guard and the socket write — so exception TYPE marks the send boundary:
        # ``HomeAssistantCommandNotSent`` is raised ONLY at the readiness guard (the one
        # provably-never-sent site → safe legacy first fire). A command-ERROR response is
        # pre-dispatch: the all-guards-first D1 / ServiceNotFound pass raises before any
        # async_call AND the component's post-dispatch assembly is total (never raises),
        # so a command error cannot carry a landed write → legacy is safe. A response-
        # wait TIMEOUT, a send() that raised (bytes may already be on the socket), OR a
        # post-send transport drop (a mid-await socket close raises plain
        # ``HomeAssistantConnectionError``) is AMBIGUOUS for the WHOLE batch → report
        # every op dispatched-but-unconfirmed, NEVER re-dispatch. ``frame_timeout``
        # honors the per-op ``timeout_seconds`` legacy respects (M-timeout).
        frame_timeout = self._bulk_frame_timeout(valid_operations)
        try:
            raw = await ws.send_command(
                WS_BULK_CALL_SERVICE,
                operations=rows,
                parallel=parallel,
                wait=True,
                timeout=frame_timeout,
                # Keep the response-wait comfortably above the batch confirmation wait
                # so the component's own bounded wait (not the client) decides the
                # partial cutoff, preserving the legacy 10s/30s margin at any timeout.
                _wait_timeout=frame_timeout + 20.0,
            )
        except HomeAssistantCommandNotSent as exc:
            # PRE-SEND: the batch frame provably never left the process. Nothing
            # dispatched, so legacy is a safe first fire.
            logger.warning(
                "%s not sent; falling back to legacy: %r",
                WS_BULK_CALL_SERVICE,
                exc,
            )
            return None
        except HomeAssistantCommandError as exc:
            # unknown_command → invalidate the cached caps so the next call re-probes;
            # any other command error → legacy (provably pre-dispatch per D9 above).
            if is_unknown_command(exc):
                invalidate_caps(self.client)
            else:
                logger.warning(
                    "%s command error; falling back to legacy: %r",
                    WS_BULK_CALL_SERVICE,
                    exc,
                )
            return None
        except Exception as exc:
            # HomeAssistantCommandTimeout (the response-wait expired — the batch frame
            # WAS sent) or any post-send transport drop: the batch is ambiguous-
            # dispatched. Report partial, NEVER re-dispatch (D9 at-most-once).
            logger.warning(
                "%s post-send timeout/drop; reporting batch partial (not retried): %r",
                WS_BULK_CALL_SERVICE,
                exc,
            )
            return self._build_ambiguous_bulk_response(
                operations, valid_operations, rows, skipped_operations, parallel
            )

        # A SUCCESS result frame is produced ONLY after the batch prep ran to completion
        # (every op's async_call fired), so a malformed/unusable success envelope (a
        # non-dict result, or an op-list whose length drifts from the rows we sent)
        # means the writes already HAPPENED. Report the batch AMBIGUOUS (every op
        # partial, never re-dispatched) — a ``None`` here would route to the legacy
        # operation-registry path and double-fire every landed op.
        op_results = self._component_bulk_op_results(raw, len(rows))
        if op_results is None:
            return self._build_ambiguous_bulk_response(
                operations, valid_operations, rows, skipped_operations, parallel
            )

        results = [
            self._map_component_op_result(op, entity_id, action, row, component_op)
            for (_idx, op, entity_id, action), row, component_op in zip(
                valid_operations, rows, op_results, strict=True
            )
        ]
        # Reuse the legacy response builder: operation_ids is empty (ops confirmed
        # inline, no polling handle) → follow_up is None, and the successful/failed
        # tallies key off command_sent exactly as the legacy path.
        return self._build_bulk_response(
            operations, results, [], skipped_operations, parallel
        )

    @staticmethod
    def _has_duplicate_entity_ids(
        valid_operations: list[tuple[int, dict[str, Any], str, str]],
    ) -> bool:
        """True when two+ ops in the batch target the same entity_id.

        The component keys ONE transition waiter per entity_id, so the first
        ``state_changed`` satisfies every op sharing that entity — both would report the
        same transition/final_state. The legacy sequential path confirms each op
        independently, so ``_bulk_via_component`` routes the WHOLE batch to legacy when
        this returns True.
        """
        entity_ids = [entity_id for _idx, _op, entity_id, _action in valid_operations]
        return len(entity_ids) != len(set(entity_ids))

    @staticmethod
    def _bulk_frame_timeout(
        valid_operations: list[tuple[int, dict[str, Any], str, str]],
    ) -> float:
        """The batch confirmation-wait bound, honoring per-op ``timeout_seconds``.

        Legacy runs each op with its own ``timeout_seconds`` (default 10); the single
        batch frame shares ONE bounded wait, so use the max over the valid ops (capped
        at 60s) rather than a hardcoded 10s (M-timeout). ``valid_operations`` is
        non-empty here (an empty batch returned to legacy before the send). An explicit
        ``timeout_seconds`` of 0 is honored (parity with legacy and the component
        schema); only an ABSENT key defaults to 10.
        """
        return min(
            60.0,
            max(
                float(v) if (v := op.get("timeout_seconds")) is not None else 10.0
                for _idx, op, _eid, _action in valid_operations
            ),
        )

    @staticmethod
    def _component_bulk_op_results(
        raw: dict[str, Any], expected_len: int
    ) -> list[Any] | None:
        """The per-op results list from a batch frame, or ``None`` if malformed.

        The component preserves op order and returns exactly one result per row it was
        sent. A non-dict result or an op-list whose length drifts from ``expected_len``
        is an unusable/incompatible SUCCESS envelope — the caller treats ``None`` as
        AMBIGUOUS (the writes already landed; never reconcile a mismatched batch).
        """
        result = raw.get("result")
        if not isinstance(result, dict):
            return None
        op_results = result.get("operations")
        if not isinstance(op_results, list) or len(op_results) != expected_len:
            return None
        return op_results

    def _build_ambiguous_bulk_response(
        self,
        operations: list[dict[str, Any]],
        valid_operations: list[tuple[int, dict[str, Any], str, str]],
        rows: list[dict[str, Any]],
        skipped_operations: list[dict[str, Any]],
        parallel: bool,
    ) -> dict[str, Any]:
        """Partial-success bulk response for a post-send batch-frame timeout/drop.

        The batch frame WAS sent, so some/all ops may have dispatched; the batch
        response never arrived. Per D9 at-most-once every valid op is reported
        dispatched-but-unconfirmed (``partial``) and the batch is NEVER re-dispatched
        via legacy. ``rows`` is aligned 1:1 with ``valid_operations`` (both built in
        order; an unresolvable op returned to legacy earlier, so no ``None`` rows
        reach here).
        """
        results: list[dict[str, Any]] = [
            {
                "entity_id": entity_id,
                "action": action,
                "parameters": op.get("parameters") or {},
                "command_sent": True,
                "status": "dispatched_unconfirmed",
                "confirmed": False,
                "partial": True,
                "final_state": None,
                "transitions": [],
                "service_call": {
                    "domain": row["domain"],
                    "service": row["service"],
                    "data": row["service_data"],
                },
                "verification_method": "component_state_change",
                "message": (
                    f"{entity_id} {action} dispatched but the batch confirmation did "
                    "not arrive within the timeout; not retried"
                ),
            }
            for (_idx, op, entity_id, action), row in zip(
                valid_operations, rows, strict=True
            )
        ]
        response = self._build_bulk_response(
            operations, results, [], skipped_operations, parallel
        )
        response["partial"] = True
        response.setdefault("warnings", []).append(
            "The bulk operation was dispatched but Home Assistant did not confirm "
            "within the timeout. Operations were NOT retried (at-most-once). Use "
            "ha_get_state to verify the affected entities."
        )
        return response

    @staticmethod
    def _tool_error_to_dict(e: ToolError) -> dict[str, Any]:
        """Extract structured error dict from ToolError without double-encoding."""
        try:
            result: dict[str, Any] = json.loads(str(e))
        except (json.JSONDecodeError, TypeError):
            logger.warning(f"Could not decode ToolError as structured response: {e!r}")
            result = create_error_response(ErrorCode.SERVICE_CALL_FAILED, str(e))
        return result

    async def _execute_parallel(
        self,
        valid_operations: list[tuple[int, dict[str, Any], str, str]],
        results: list[dict[str, Any]],
        operation_ids: list[str],
    ) -> None:
        tasks = []
        for _i, op, entity_id, action in valid_operations:
            task = self.control_device_smart(
                entity_id=entity_id,
                action=action,
                parameters=op.get("parameters"),
                timeout_seconds=op.get("timeout_seconds", 10),
                validate_first=op.get("validate_first", True),
            )
            tasks.append(task)

        if tasks:
            task_results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in task_results:
                if isinstance(result, ToolError):
                    results.append(self._tool_error_to_dict(result))
                elif isinstance(result, Exception):
                    results.append(
                        create_error_response(
                            ErrorCode.SERVICE_CALL_FAILED,
                            f"Exception during execution: {result!s}",
                        )
                    )
                elif isinstance(result, dict):
                    results.append(result)
                    if "operation_id" in result:
                        operation_ids.append(result["operation_id"])

    async def _execute_sequential(
        self,
        valid_operations: list[tuple[int, dict[str, Any], str, str]],
        results: list[dict[str, Any]],
        operation_ids: list[str],
        ctx: Context | None = None,
    ) -> None:
        total = len(valid_operations)
        for i, (_orig_index, op, entity_id, action) in enumerate(valid_operations):
            try:
                result = await self.control_device_smart(
                    entity_id=entity_id,
                    action=action,
                    parameters=op.get("parameters"),
                    timeout_seconds=op.get("timeout_seconds", 10),
                    validate_first=op.get("validate_first", True),
                )
                results.append(result)
                if "operation_id" in result:
                    operation_ids.append(result["operation_id"])
            except ToolError as e:
                results.append(self._tool_error_to_dict(e))
            except Exception as e:
                results.append(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        f"Exception during execution: {e!s}",
                    )
                )
            await safe_progress(
                ctx,
                progress=i + 1,
                total=total,
                message=f"{entity_id} {action} dispatched",
            )

    def _build_bulk_response(
        self,
        operations: list[dict[str, Any]],
        results: list[dict[str, Any]],
        operation_ids: list[str],
        skipped_operations: list[dict[str, Any]],
        parallel: bool,
    ) -> dict[str, Any]:
        successful = len(
            [r for r in results if isinstance(r, dict) and r.get("command_sent")]
        )
        executed_failed = len(results) - successful
        # Total failed includes both execution failures and skipped operations
        total_failed = executed_failed + len(skipped_operations)

        response: dict[str, Any] = {
            "total_operations": len(operations),
            "successful_commands": successful,
            "failed_commands": total_failed,
            "skipped_operations": len(skipped_operations),
            "execution_mode": "parallel" if parallel else "sequential",
            "operation_ids": operation_ids,
            "results": results,
            "follow_up": (
                {
                    "message": (
                        f"Use get_bulk_operation_status() to check all "
                        f"{len(operation_ids)} operations"
                    ),
                    "operation_ids": operation_ids,
                }
                if operation_ids
                else None
            ),
        }

        # Include skipped operation details if any were skipped
        if skipped_operations:
            response["skipped_details"] = skipped_operations
            response["suggestions"] = [
                "Some operations were skipped due to validation errors",
                "Each operation requires 'entity_id' and 'action' fields",
                "Check skipped_details for specific errors",
                "Example format: {'entity_id': 'light.living_room', 'action': 'on'}",
            ]

        return response

    async def get_bulk_operation_status(
        self, operation_ids: list[str]
    ) -> dict[str, Any]:
        """
        Check status of multiple operations.

        Args:
            operation_ids: List of operation IDs to check

        Returns:
            Status summary for all operations
        """
        if not operation_ids:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_MISSING_PARAMETER,
                    "No operation IDs provided",
                    suggestions=[
                        "Provide a list of operation IDs from control_device_smart"
                    ],
                )
            )

        # Check all operations
        statuses = []
        for op_id in operation_ids:
            status = await self.get_device_operation_status(op_id)
            statuses.append(status)

        # Summarize results
        completed = len([s for s in statuses if s.get("status") == "completed"])
        failed = len([s for s in statuses if s.get("status") in ["failed", "timeout"]])
        pending = len([s for s in statuses if s.get("status") == "pending"])

        return {
            "total_operations": len(operation_ids),
            "completed": completed,
            "failed": failed,
            "pending": pending,
            "all_complete": pending == 0,
            "summary": {
                "success_rate": f"{completed}/{len(operation_ids)}",
                "completion_percentage": (completed / len(operation_ids)) * 100,
            },
            "detailed_results": statuses,
            "recommendations": (
                [
                    "Wait a few seconds and check again if operations are pending",
                    "Check failed operations for specific error messages",
                    "Retry failed operations with different parameters if needed",
                ]
                if pending > 0 or failed > 0
                else ["All operations completed successfully!"]
            ),
        }


def create_device_control_tools(
    client: HomeAssistantClient | None = None,
) -> DeviceControlTools:
    """Create device control tools instance."""
    return DeviceControlTools(client)
