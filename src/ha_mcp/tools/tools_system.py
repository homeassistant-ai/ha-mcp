"""
System management tools for Home Assistant MCP Server.

This module provides tools for Home Assistant system administration including:
- Configuration validation
- Service restarts and reloads
- System health monitoring
"""

import logging
from typing import Any

from .helpers import get_connected_ws_client, log_tool_usage
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

    @mcp.tool(annotations={"idempotentHint": True, "readOnlyHint": True, "tags": ["system"], "title": "Check Configuration"})
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
            logger.error(f"Failed to check configuration: {e}")
            return {
                "success": False,
                "error": f"Failed to check configuration: {str(e)}",
                "suggestions": [
                    "Ensure Home Assistant is running and accessible",
                    "Check your connection settings",
                ],
            }

    @mcp.tool(annotations={"tags": ["system"], "title": "Restart Home Assistant"})
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
            return {
                "success": False,
                "error": "Restart not confirmed",
                "message": (
                    "You must set confirm=True to restart Home Assistant. "
                    "This is a safety measure to prevent accidental restarts."
                ),
                "suggestions": [
                    "Run ha_check_config() first to validate configuration",
                    "Call ha_restart(confirm=True) to proceed with restart",
                    "Consider using ha_reload_core() for config-only changes",
                ],
            }

        restart_initiated = False
        try:
            # Check configuration first as a safety measure
            config_result = await client.check_config()
            if config_result.get("result") != "valid":
                errors = config_result.get("errors") or []
                return {
                    "success": False,
                    "error": "Configuration is invalid - restart aborted",
                    "config_errors": errors,
                    "message": (
                        "Home Assistant configuration has errors. "
                        "Fix the errors before restarting."
                    ),
                }

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

        except Exception as e:
            error_msg = str(e)
            # Connection errors after restart initiated are expected
            # (HA closes connections during restart)
            if restart_initiated and (
                "connect" in error_msg.lower() or "closed" in error_msg.lower()
            ):
                return {
                    "success": True,
                    "message": (
                        "Home Assistant restart initiated. "
                        "Connection was closed as expected during restart."
                    ),
                    "warning": "Wait 1-5 minutes for Home Assistant to restart.",
                }

            logger.error(f"Failed to restart Home Assistant: {e}")
            return {
                "success": False,
                "error": f"Failed to restart Home Assistant: {str(e)}",
            }

    @mcp.tool(annotations={"tags": ["system"], "title": "Reload Core Components"})
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
            return {
                "success": False,
                "error": f"Invalid reload target: {target}",
                "valid_targets": list(RELOAD_TARGETS.keys()),
                "suggestion": f"Use one of: {', '.join(RELOAD_TARGETS.keys())}",
            }

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
                    return {
                        "success": False,
                        "error": f"Invalid target configuration for: {target}",
                    }
                domain, service = service_info
                await client.call_service(domain, service, {})

                return {
                    "success": True,
                    "message": f"Successfully reloaded {target}",
                    "target": target,
                    "service": f"{domain}.{service}",
                }

        except Exception as e:
            logger.error(f"Failed to reload {target}: {e}")
            return {
                "success": False,
                "error": f"Failed to reload {target}: {str(e)}",
                "suggestions": [
                    f"Ensure the {target} integration is loaded",
                    "Check Home Assistant logs for details",
                ],
            }

    @mcp.tool(annotations={"idempotentHint": True, "readOnlyHint": True, "tags": ["system"], "title": "Get System Info"})
    @log_tool_usage
    async def ha_get_system_info() -> dict[str, Any]:
        """
        Get Home Assistant system information.

        Returns version, location settings, timezone, loaded components, and configuration paths.
        """
        try:
            config = await client.get_config()

            # Extract relevant system information
            system_info = {
                "success": True,
                "version": config.get("version"),
                "location_name": config.get("location_name"),
                "time_zone": config.get("time_zone"),
                "unit_system": config.get("unit_system", {}),
                "latitude": config.get("latitude"),
                "longitude": config.get("longitude"),
                "elevation": config.get("elevation"),
                "currency": config.get("currency"),
                "country": config.get("country"),
                "language": config.get("language"),
                "config_dir": config.get("config_dir"),
                "allowlist_external_dirs": config.get("allowlist_external_dirs", []),
                "allowlist_external_urls": config.get("allowlist_external_urls", []),
                "components": config.get("components", []),
                "component_count": len(config.get("components", [])),
                "state": config.get("state"),
                "safe_mode": config.get("safe_mode", False),
                "internal_url": config.get("internal_url"),
                "external_url": config.get("external_url"),
            }

            return system_info

        except Exception as e:
            logger.error(f"Failed to get system info: {e}")
            return {
                "success": False,
                "error": f"Failed to get system info: {str(e)}",
            }

    @mcp.tool(annotations={"idempotentHint": True, "readOnlyHint": True, "tags": ["system"], "title": "Get System Health"})
    @log_tool_usage
    async def ha_get_system_health() -> dict[str, Any]:
        """
        Get Home Assistant system health information.

        Returns health check results from integrations, system resources, and connectivity.
        Available information varies by installation type and loaded integrations.
        """
        ws_client = None

        try:
            # Connect to WebSocket for system_health/info
            ws_client, error = await get_connected_ws_client(
                client.base_url, client.token
            )
            if error or ws_client is None:
                return error or {
                    "success": False,
                    "error": "Failed to establish WebSocket connection",
                }

            # Request system health info via WebSocket
            health_response = await ws_client.send_command("system_health/info")

            if not health_response.get("success"):
                return {
                    "success": False,
                    "error": "Failed to retrieve system health",
                    "details": health_response,
                }

            health_info = health_response.get("result") or {}

            return {
                "success": True,
                "health_info": health_info,
                "component_count": len(health_info) if isinstance(health_info, dict) else 0,
                "message": f"Retrieved health info for {len(health_info) if isinstance(health_info, dict) else 0} components",
            }

        except Exception as e:
            logger.error(f"Failed to get system health: {e}")
            return {
                "success": False,
                "error": f"Failed to get system health: {str(e)}",
                "suggestions": [
                    "System health may not be available in all HA installations",
                    "Try ha_get_system_info() for basic system information",
                ],
            }
        finally:
            if ws_client:
                try:
                    await ws_client.disconnect()
                except Exception:
                    pass
