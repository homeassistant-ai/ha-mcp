"""
Smart device control tools with async verification.

This module provides intelligent device control with domain-specific handling
and async operation verification through WebSocket monitoring.
"""

import asyncio
import logging
from typing import Any

from ..client.rest_client import HomeAssistantClient
from ..client.websocket_listener import start_websocket_listener
from ..config import get_global_settings
from ..utils.domain_handlers import get_domain_handler
from ..utils.operation_manager import get_operation_from_memory, store_pending_operation

logger = logging.getLogger(__name__)


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
            # Handle parameters that might be passed as JSON string
            if parameters and isinstance(parameters, str):
                try:
                    import json

                    parameters = json.loads(parameters)
                except json.JSONDecodeError:
                    return {
                        "entity_id": entity_id,
                        "action": action,
                        "success": False,
                        "error": f"Invalid JSON in parameters: {parameters}",
                        "suggestions": [
                            "Parameters should be a valid JSON object",
                            "Example: {'brightness': 102, 'color_temp': 4000}",
                        ],
                    }
            # Parse domain from entity ID
            if "." not in entity_id:
                return {
                    "entity_id": entity_id,
                    "action": action,
                    "success": False,
                    "error": f"Invalid entity ID format: {entity_id}",
                    "suggestions": [
                        "Entity ID must be in format 'domain.entity_name'",
                        "Use smart_entity_search to find correct entity ID",
                    ],
                }

            domain = entity_id.split(".")[0]
            handler = get_domain_handler(domain)

            # Validate entity exists if requested
            if validate_first:
                try:
                    current_state = await self.client.get_entity_state(entity_id)
                    if not current_state:
                        return {
                            "entity_id": entity_id,
                            "action": action,
                            "success": False,
                            "error": f"Entity not found: {entity_id}",
                            "suggestions": [
                                "Use smart_entity_search to find the correct entity",
                                "Check entity is not disabled in Home Assistant",
                            ],
                        }
                except Exception as e:
                    return {
                        "entity_id": entity_id,
                        "action": action,
                        "success": False,
                        "error": f"Cannot verify entity exists: {str(e)}",
                        "suggestions": [
                            "Check Home Assistant connection",
                            "Verify entity ID spelling",
                        ],
                    }

            # Validate action for domain
            valid_actions = handler.get("valid_actions", ["on", "off", "toggle"])
            if action not in valid_actions:
                return {
                    "entity_id": entity_id,
                    "action": action,
                    "success": False,
                    "error": f"Invalid action '{action}' for domain '{domain}'",
                    "valid_actions": valid_actions,
                    "suggestions": [
                        f"Valid actions for {domain}: {', '.join(valid_actions)}",
                        "Use 'toggle' for simple on/off control",
                    ],
                }

            # Build service call
            service_call = self._build_service_call(
                entity_id, domain, action, parameters, handler
            )

            # Predict expected state after operation
            expected_state = self._predict_expected_state(
                current_state if validate_first else None, action, parameters, domain
            )

            # Execute service call
            try:
                await self.client.call_service(
                    service_call["domain"],
                    service_call["service"],
                    service_call["data"],
                )

                # Store operation for async verification
                operation_id = store_pending_operation(
                    entity_id=entity_id,
                    action=action,
                    service_domain=service_call["domain"],
                    service_name=service_call["service"],
                    service_data=service_call["data"],
                    expected_state=expected_state,
                    timeout_ms=timeout_seconds * 1000,
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

            except Exception as e:
                return {
                    "entity_id": entity_id,
                    "action": action,
                    "success": False,
                    "error": f"Service call failed: {str(e)}",
                    "service_call": service_call,
                    "suggestions": [
                        "Check if entity supports this action",
                        "Verify Home Assistant connection",
                        "Check Home Assistant logs for details",
                    ],
                }

        except Exception as e:
            logger.error(f"Error in control_device_smart: {e}")
            return {
                "entity_id": entity_id,
                "action": action,
                "success": False,
                "error": f"Unexpected error: {str(e)}",
                "suggestions": [
                    "Check entity ID format",
                    "Verify Home Assistant connection",
                    "Try simpler action like 'toggle'",
                ],
            }

    def _build_service_call(
        self,
        entity_id: str,
        domain: str,
        action: str,
        parameters: dict[str, Any] | None,
        handler: dict[str, Any],
    ) -> dict[str, Any]:
        """Build Home Assistant service call from action and parameters."""

        # Basic service mapping
        service_mapping = {
            "on": "turn_on",
            "off": "turn_off",
            "toggle": "toggle",
            "open": "open_cover" if domain == "cover" else "turn_on",
            "close": "close_cover" if domain == "cover" else "turn_off",
            "set": "turn_on" if domain == "light" else "set_temperature",
        }

        service_name = service_mapping.get(action, action)

        # Domain-specific service adjustments
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

        # Build service data
        service_data = {"entity_id": entity_id}

        # Add parameters based on domain
        if parameters:
            if domain == "light":
                light_params = [
                    "brightness",
                    "color_temp",
                    "rgb_color",
                    "effect",
                    "kelvin",
                ]
                for param in light_params:
                    if param in parameters:
                        service_data[param] = parameters[param]

            elif domain == "climate":
                climate_params = [
                    "temperature",
                    "target_temp_high",
                    "target_temp_low",
                    "hvac_mode",
                ]
                for param in climate_params:
                    if param in parameters:
                        service_data[param] = parameters[param]

            elif domain == "cover":
                cover_params = ["position", "tilt_position"]
                for param in cover_params:
                    if param in parameters:
                        service_data[param] = parameters[param]

            elif domain == "media_player":
                media_params = [
                    "volume_level",
                    "media_content_id",
                    "media_content_type",
                ]
                for param in media_params:
                    if param in parameters:
                        service_data[param] = parameters[param]

        # Remove None values
        service_data = {k: v for k, v in service_data.items() if v is not None}

        return {"domain": domain, "service": service_name, "data": service_data}

    def _predict_expected_state(
        self,
        current_state: dict[str, Any] | None,
        action: str,
        parameters: dict[str, Any] | None,
        domain: str,
    ) -> dict[str, Any] | None:
        """Predict expected entity state after operation."""

        expected = {}

        # State predictions by action
        if action == "on":
            expected["state"] = "on"
        elif action == "off":
            expected["state"] = "off"
        elif action == "toggle":
            if current_state:
                current = current_state.get("state", "off")
                expected["state"] = "off" if current == "on" else "on"
            else:
                # Can't predict toggle without current state
                return None
        elif action == "open":
            expected["state"] = "open"
        elif action == "close":
            expected["state"] = "closed"

        # Domain-specific attribute predictions
        if parameters:
            if domain == "light" and action in ["on", "set"]:
                if "brightness" in parameters:
                    expected["brightness"] = parameters["brightness"]
                if "color_temp" in parameters:
                    expected["color_temp"] = parameters["color_temp"]

            elif domain == "climate" and action in ["set", "heat", "cool", "auto"]:
                if "temperature" in parameters:
                    expected["temperature"] = parameters["temperature"]
                if "hvac_mode" in parameters:
                    expected["hvac_mode"] = parameters["hvac_mode"]
                elif action in ["heat", "cool", "auto"]:
                    expected["hvac_mode"] = action

        return expected if expected else None

    async def get_device_operation_status(
        self, operation_id: str, timeout_seconds: int = 10
    ) -> dict[str, Any]:
        """
        Check status of device operation with async verification.

        This tool checks the status of operations initiated by control_device_smart.
        Results come from real-time WebSocket monitoring of Home Assistant state changes.

        Args:
            operation_id: Operation ID returned by control_device_smart
            timeout_seconds: Maximum time to wait for completion

        Returns:
            Operation status with completion details or timeout info
        """
        operation = get_operation_from_memory(operation_id)

        if not operation:
            return {
                "operation_id": operation_id,
                "success": False,
                "error": "Operation not found or expired",
                "suggestions": [
                    "Operation may have been cleaned up after completion",
                    "Check operation ID spelling",
                    "Use control_device_smart to start new operation",
                ],
            }

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
            return {
                "operation_id": operation_id,
                "status": "failed",
                "success": False,
                "entity_id": operation.entity_id,
                "action": operation.action,
                "error": operation.error_message,
                "duration_ms": operation.duration_ms,
                "suggestions": [
                    "Check if device is available and responding",
                    "Verify device supports the requested action",
                    "Check Home Assistant logs for error details",
                    "Try a simpler action like toggle",
                ],
            }

        elif operation.status.value == "timeout":
            return {
                "operation_id": operation_id,
                "status": "timeout",
                "success": False,
                "entity_id": operation.entity_id,
                "action": operation.action,
                "error": f"Operation timed out after {operation.timeout_ms}ms",
                "elapsed_ms": operation.elapsed_ms,
                "suggestions": [
                    "Device may be slow to respond or offline",
                    "Check device connectivity",
                    "Try increasing timeout for slow devices",
                    "Verify device is powered on",
                ],
            }

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

    async def bulk_device_control(
        self, operations: list[dict[str, Any]], parallel: bool = True
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
            return {"success": False, "error": "No operations provided", "results": []}

        results: list[dict[str, Any]] = []
        operation_ids: list[str] = []
        skipped_operations: list[dict[str, Any]] = []

        def validate_operation(
            op: Any, index: int
        ) -> tuple[str | None, str | None, str | None]:
            """Validate operation and return (entity_id, action, error) tuple."""
            if not isinstance(op, dict):
                error = f"Operation at index {index} is not a dict: {type(op).__name__}"
                logger.warning(f"Bulk control: {error}")
                return None, None, error

            entity_id = op.get("entity_id")
            action = op.get("action")

            missing_fields = []
            if not entity_id:
                missing_fields.append("entity_id")
            if not action:
                missing_fields.append("action")

            if missing_fields:
                error = (
                    f"Operation at index {index} missing required fields: "
                    f"{', '.join(missing_fields)}"
                )
                logger.warning(f"Bulk control: {error}")
                return None, None, error

            return str(entity_id), str(action), None

        try:
            # Validate all operations first (centralized validation)
            valid_operations: list[tuple[int, dict[str, Any], str, str]] = []
            for i, op in enumerate(operations):
                entity_id, action, error = validate_operation(op, i)
                if error:
                    skipped_operations.append(
                        {
                            "index": i,
                            "operation": op,
                            "error": error,
                            "success": False,
                        }
                    )
                else:
                    # Store (index, original_op, entity_id, action) for execution
                    valid_operations.append((i, op, entity_id, action))  # type: ignore[arg-type]

            # Execute only valid operations
            if parallel:
                # Build tasks for parallel execution
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

                    # Process results and extract operation IDs
                    for result in task_results:
                        if isinstance(result, Exception):
                            results.append(
                                {
                                    "success": False,
                                    "error": f"Exception during execution: {result!s}",
                                }
                            )
                        elif isinstance(result, dict):
                            results.append(result)
                            if "operation_id" in result:
                                operation_ids.append(result["operation_id"])

            else:
                # Execute valid operations sequentially
                for _i, op, entity_id, action in valid_operations:
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

            # Count successes and failures from executed operations
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

        except Exception as e:
            logger.error(f"Error in bulk_device_control: {e}")
            return {
                "success": False,
                "error": f"Bulk operation failed: {e!s}",
                "results": results,
            }

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
            return {"success": False, "error": "No operation IDs provided"}

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
