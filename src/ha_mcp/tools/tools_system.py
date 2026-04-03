"""
System management tools for Home Assistant MCP Server.

This module provides tools for Home Assistant system administration including:
- Configuration validation
- Service restarts and reloads
- System health monitoring
"""

import logging
from typing import Any

from fastmcp.exceptions import ToolError

from ..errors import ErrorCode, create_error_response
from .helpers import (
    exception_to_structured_error,
    get_connected_ws_client,
    log_tool_usage,
    raise_tool_error,
)
from .util_helpers import coerce_bool_param

logger = logging.getLogger(__name__)

# Mapping of reload targets to their service domains and services
RELOAD_TARGETS = {
    "all": None,  # Special case - reload all
    "automations": ("automation", "reload"),
    "scripts": ("script", "reload"),
    "scenes": ("scene", "reload"),
    "groups": ("group", "reload"),
    "input_booleans": ("input_boolean", "reload"),
    "input_numbers": ("input_number", "reload"),
    "input_texts": ("input_text", "reload"),
    "input_selects": ("input_select", "reload"),
    "input_datetimes": ("input_datetime", "reload"),
    "input_buttons": ("input_button", "reload"),
    "timers": ("timer", "reload"),
    "counters": ("counter", "reload"),
    "templates": ("template", "reload"),
    "persons": ("person", "reload"),
    "zones": ("zone", "reload"),
    "core": ("homeassistant", "reload_core_config"),
    "themes": ("frontend", "reload_themes"),
}


def register_system_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant system management tools."""

    @mcp.tool(tags={"System"}, annotations={"idempotentHint": True, "readOnlyHint": True, "title": "Check Configuration"})
    @log_tool_usage
    async def ha_check_config() -> dict[str, Any]:
        """
        Check Home Assistant configuration for errors.

        Validates configuration files without applying changes.
        Always run this before ha_restart() to ensure configuration is valid.
        """
        try:
            config_result = await client.check_config()

            # The API returns {"result": "valid"} or {"result": "invalid", "errors": [...]}
            is_valid = config_result.get("result") == "valid"
            errors = config_result.get("errors") or []  # Handle None case

            return {
                "success": True,
                "result": "valid" if is_valid else "invalid",
                "is_valid": is_valid,
                "errors": errors,
                "message": (
                    "Configuration is valid"
                    if is_valid
                    else f"Configuration has {len(errors)} error(s)"
                ),
            }

        except Exception as e:
            exception_to_structured_error(
                e,
                suggestions=[
                    "Ensure Home Assistant is running and accessible",
                    "Check your connection settings",
                ],
            )

    @mcp.tool(tags={"System"}, annotations={"destructiveHint": True, "title": "Restart Home Assistant"})
    @log_tool_usage
    async def ha_restart(
        confirm: bool | str = False,
    ) -> dict[str, Any]:
        """
        Restart Home Assistant.

        **WARNING: This will restart the entire Home Assistant instance!**
        All automations will be temporarily unavailable during restart.
        The restart typically takes 1-5 minutes depending on your setup.

        **Parameters:**
        - confirm: Must be set to True to confirm the restart. This is a safety
                   measure to prevent accidental restarts.

        **Best Practices:**
        1. Always run ha_check_config() first to ensure configuration is valid
        2. Notify users before restarting (if applicable)
        3. Schedule restarts during low-activity periods

        **Example Usage:**
        ```python
        # Always check config first
        config = ha_check_config()
        if config["result"] == "valid":
            # Restart with confirmation
            result = ha_restart(confirm=True)
        ```

        **Alternative:** For configuration changes, consider using ha_reload_core()
        instead, which reloads specific components without a full restart.
        """
        # Coerce boolean parameter that may come as string from XML-style calls
        confirm_bool = coerce_bool_param(confirm, "confirm", default=False) or False

        if not confirm_bool:
            raise_tool_error(create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "Restart not confirmed",
                details=(
                    "You must set confirm=True to restart Home Assistant. "
                    "This is a safety measure to prevent accidental restarts."
                ),
                suggestions=[
                    "Run ha_check_config() first to validate configuration",
                    "Call ha_restart(confirm=True) to proceed with restart",
                    "Consider using ha_reload_core() for config-only changes",
                ],
            ))

        restart_initiated = False
        try:
            # Check configuration first as a safety measure
            config_result = await client.check_config()
            if config_result.get("result") != "valid":
                errors = config_result.get("errors") or []
                raise_tool_error(create_error_response(
                    ErrorCode.CONFIG_INVALID,
                    "Configuration is invalid - restart aborted",
                    details=(
                        "Home Assistant configuration has errors. "
                        "Fix the errors before restarting."
                    ),
                    context={"config_errors": errors},
                ))

            # Call the restart service - mark as initiated before the call
            # as the connection may be closed before we get a response
            restart_initiated = True
            await client.call_service("homeassistant", "restart", {})

            return {
                "success": True,
                "message": (
                    "Home Assistant restart initiated. "
                    "The system will be unavailable for 1-5 minutes."
                ),
                "warning": (
                    "Connection will be lost during restart. "
                    "Wait for Home Assistant to become available again."
                ),
            }

        except ToolError:
            raise
        except Exception as e:
            error_msg = str(e)
            # Connection errors after restart initiated are expected
            # (HA closes connections during restart)
            if restart_initiated and any(
                pattern in error_msg.lower()
                for pattern in ("connect", "closed", "504")
            ):
                return {
                    "success": True,
                    "message": (
                        "Home Assistant restart initiated. "
                        "Connection was closed as expected during restart."
                    ),
                    "warning": "Wait 1-5 minutes for Home Assistant to restart.",
                }

            exception_to_structured_error(e)

    @mcp.tool(tags={"System"}, annotations={"destructiveHint": True, "title": "Reload Core Components"})
    @log_tool_usage
    async def ha_reload_core(
        target: str = "all",
    ) -> dict[str, Any]:
        """
        Reload Home Assistant configuration without full restart.

        This tool reloads specific configuration components, allowing changes
        to take effect without restarting the entire Home Assistant instance.
        This is much faster than a full restart.

        **Parameters:**
        - target: What to reload. Options:
          - "all": Reload all reloadable components
          - "automations": Reload automation configurations
          - "scripts": Reload script configurations
          - "scenes": Reload scene configurations
          - "groups": Reload group configurations
          - "input_booleans": Reload input_boolean helpers
          - "input_numbers": Reload input_number helpers
          - "input_texts": Reload input_text helpers
          - "input_selects": Reload input_select helpers
          - "input_datetimes": Reload input_datetime helpers
          - "input_buttons": Reload input_button helpers
          - "timers": Reload timer helpers
          - "counters": Reload counter helpers
          - "templates": Reload template sensors/entities
          - "persons": Reload person configurations
          - "zones": Reload zone configurations
          - "core": Reload core configuration (customize, packages)
          - "themes": Reload frontend themes

        **Example Usage:**
        ```python
        # Reload just automations after editing
        ha_reload_core(target="automations")

        # Reload all configurations
        ha_reload_core(target="all")

        # Reload input helpers after adding new ones
        ha_reload_core(target="input_booleans")
        ```

        **When to Use:**
        - After editing automation/script YAML files
        - After adding new input helpers via YAML
        - After modifying customize.yaml
        - After theme changes
        """
        target = target.lower().strip()

        if target not in RELOAD_TARGETS:
            raise_tool_error(create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Invalid reload target: {target}",
                context={"target": target, "valid_targets": list(RELOAD_TARGETS.keys())},
                suggestions=[f"Use one of: {', '.join(RELOAD_TARGETS.keys())}"],
            ))

        try:
            if target == "all":
                # Reload all reloadable components
                results = []
                errors = []

                for reload_target, service_info in RELOAD_TARGETS.items():
                    if service_info is None:  # Skip "all" itself
                        continue

                    domain, service = service_info
                    try:
                        await client.call_service(domain, service, {})
                        results.append(reload_target)
                    except Exception as e:
                        # Some services might not be available in all installations
                        error_msg = str(e)
                        if "not found" not in error_msg.lower():
                            errors.append(f"{reload_target}: {error_msg}")

                return {
                    "success": True,
                    "message": f"Reloaded {len(results)} components",
                    "reloaded": results,
                    "warnings": errors if errors else None,
                }

            else:
                # Reload specific component
                service_info = RELOAD_TARGETS[target]
                if service_info is None:
                    # This shouldn't happen as we check for "all" above
                    raise_tool_error(create_error_response(
                        ErrorCode.INTERNAL_ERROR,
                        f"Invalid target configuration for: {target}",
                        context={"target": target},
                    ))
                domain, service = service_info
                await client.call_service(domain, service, {})

                return {
                    "success": True,
                    "message": f"Successfully reloaded {target}",
                    "target": target,
                    "service": f"{domain}.{service}",
                }

        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={"target": target},
                suggestions=[
                    f"Ensure the {target} integration is loaded",
                    "Check Home Assistant logs for details",
                ],
            )

    @mcp.tool(tags={"System", "Zigbee", "Z-Wave"}, annotations={"idempotentHint": True, "readOnlyHint": True, "title": "Get System Health (incl. ZHA/Z-Wave diagnostics)"})
    @log_tool_usage
    async def ha_get_system_health(
        include: str | None = None,
    ) -> dict[str, Any]:
        """
        Get Home Assistant system health, including Zigbee (ZHA) and Z-Wave JS network diagnostics.

        Returns health check results from integrations, system resources, and connectivity.
        Available information varies by installation type and loaded integrations.

        **Parameters:**
        - include: Optional comma-separated list of additional data to include.
          - "repairs": Repair items from Settings > System > Repairs
          - "zha_network": ZHA Zigbee devices with radio signal summary (name, LQI, RSSI)
          - "zha_network_full": ZHA Zigbee devices with all device details
          - "zwave_network": Z-Wave JS network status and node summary (status, security, routing)
          - Example: include="repairs,zha_network,zwave_network"
        """
        # Parse include parameter into a set of requested sections
        includes: set[str] = set()
        if include:
            includes = {s.strip().lower() for s in include.split(",") if s.strip()}

        ws_client = None

        try:
            # Connect to WebSocket for system_health/info
            ws_client, error = await get_connected_ws_client(
                client.base_url, client.token
            )
            if error or ws_client is None:
                raise_tool_error(error or create_error_response(
                    ErrorCode.CONNECTION_FAILED,
                    "Failed to connect to Home Assistant WebSocket",
                ))

            # system_health/info returns a result + follow-up event
            try:
                _, event_response = await ws_client.send_command_with_event(
                    "system_health/info", wait_timeout=10.0
                )
            except TimeoutError:
                raise_tool_error(create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Timeout waiting for system health data",
                ))
            except Exception as e:
                raise_tool_error(create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    str(e),
                ))

            health_info = event_response.get("event", {})
            component_count = len(health_info) if isinstance(health_info, dict) else 0

            result: dict[str, Any] = {
                "success": True,
                "health_info": health_info,
                "component_count": component_count,
                "message": f"Retrieved health info for {component_count} components",
            }

            # Fetch repairs if requested
            if "repairs" in includes:
                result["repairs"] = {"issues": [], "count": 0}
                try:
                    repairs_result = await ws_client.send_command(
                        "repairs/list_issues"
                    )
                    if repairs_result.get("success"):
                        repairs_list = repairs_result.get("result", {}).get("issues", [])
                        result["repairs"] = {
                            "issues": repairs_list,
                            "count": len(repairs_list),
                        }
                except Exception as e:
                    logger.warning("Failed to fetch repairs: %s", e)
                    result["repairs"]["error"] = f"Repairs data not available: {e}"

            # Fetch ZHA network data if requested
            zha_full = "zha_network_full" in includes
            zha_summary = "zha_network" in includes
            if zha_full or zha_summary:
                result["zha_network"] = {"devices": [], "count": 0}
                try:
                    zha_result = await ws_client.send_command(
                        "zha/devices"
                    )
                    if zha_result.get("success"):
                        raw_devices = zha_result.get("result", [])
                        if zha_full:
                            # Return complete device data
                            zha_devices = raw_devices
                        else:
                            # Return only radio-relevant fields to reduce response size
                            zha_devices = [
                                {
                                    "ieee": d.get("ieee"),
                                    "name": d.get("user_given_name") or d.get("name"),
                                    "manufacturer": d.get("manufacturer"),
                                    "model": d.get("model"),
                                    "lqi": d.get("lqi"),
                                    "rssi": d.get("rssi"),
                                    "available": d.get("available"),
                                }
                                for d in raw_devices
                            ]
                        result["zha_network"] = {
                            "devices": zha_devices,
                            "count": len(zha_devices),
                        }
                except Exception as e:
                    logger.warning("Failed to fetch ZHA network data: %s", e)
                    result["zha_network"]["error"] = f"ZHA integration not available or error: {e}"

            # Fetch Z-Wave JS network data if requested
            if "zwave_network" in includes:
                result["zwave_network"] = {"controller": {}, "nodes": [], "count": 0}
                try:
                    # Get all zwave_js config entries to find entry_id
                    entries_result = await ws_client.send_command(
                        "config/entries/get",
                    )
                    zwave_entry_id = None
                    if entries_result.get("success"):
                        for entry in entries_result.get("result", []):
                            if entry.get("domain") == "zwave_js":
                                zwave_entry_id = entry.get("entry_id")
                                break

                    if zwave_entry_id:
                        # Get network status (controller info)
                        network_result = await ws_client.send_command(
                            "zwave_js/network_status",
                            entry_id=zwave_entry_id,
                        )
                        if network_result.get("success"):
                            net_data = network_result.get("result", {})
                            result["zwave_network"]["controller"] = net_data.get(
                                "controller", {}
                            )
                            # Extract node summaries from controller nodes
                            nodes = net_data.get("controller", {}).get("nodes", [])
                            node_summaries = [
                                {
                                    "node_id": n.get("node_id"),
                                    "status": n.get("status"),
                                    "is_routing": n.get("is_routing"),
                                    "is_secure": n.get("is_secure"),
                                    "zwave_plus_version": n.get("zwave_plus_version"),
                                    "is_controller_node": n.get("is_controller_node"),
                                }
                                for n in nodes
                            ]
                            if node_summaries:
                                result["zwave_network"]["nodes"] = node_summaries
                                result["zwave_network"]["count"] = len(node_summaries)
                    else:
                        result["zwave_network"]["error"] = (
                            "Z-Wave JS integration not found"
                        )
                except Exception as e:
                    logger.warning("Failed to fetch Z-Wave network data: %s", e)
                    result["zwave_network"]["error"] = (
                        f"Z-Wave JS integration not available or error: {e}"
                    )

            return result

        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                suggestions=[
                    "System health may not be available in all HA installations",
                    "Try ha_get_overview() for basic system information",
                ],
            )
        finally:
            if ws_client:
                try:
                    await ws_client.disconnect()
                except Exception:
                    pass
