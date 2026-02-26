"""
Diagnostic and health monitoring tools for Home Assistant MCP Server.

This module provides tools for proactive system health monitoring, anomaly
detection, and troubleshooting. These tools complement the existing management
and control tools by enabling AI agents to detect and diagnose issues.

Tools included:
- ha_get_error_log: Structured system log entries with severity filtering
- ha_get_repair_items: HA repair/issue items grouped by severity
- ha_system_health_check: Composite diagnostic producing a full health report
- ha_get_zha_network: Zigbee devices with radio signal metrics (LQI/RSSI)
- ha_find_anomalous_entities: Detect entities with impossible or suspicious values
- ha_entity_diagnostics: Deep dive on a single entity combining state, history, device info
- ha_automation_report: Overview of automations with health indicators
- ha_fix_entity: Targeted diagnostic fixes (reload, enable, restart integration)
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from pydantic import Field

from .helpers import (
    exception_to_structured_error,
    get_connected_ws_client,
    log_tool_usage,
)
from .util_helpers import coerce_int_param

logger = logging.getLogger(__name__)


def register_diagnostics_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant diagnostic and health monitoring tools."""

    @mcp.tool(
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "tags": ["diagnostics"],
            "title": "Get Error Log",
        }
    )
    @log_tool_usage
    async def ha_get_error_log(
        severity: Annotated[
            str | None,
            Field(
                description="Filter by severity: 'error', 'warning', or 'info'. "
                "If omitted, returns all severities.",
                default=None,
            ),
        ] = None,
        limit: Annotated[
            int | str,
            Field(
                description="Maximum number of log entries to return (default: 50, max: 200)",
                default=50,
            ),
        ] = 50,
    ) -> dict[str, Any]:
        """
        Get structured system log entries from Home Assistant.

        Returns structured error/warning/info entries from the system log,
        unlike the plain-text error log. Each entry includes timestamp,
        severity level, message, source component, and occurrence count.

        This is useful for diagnosing integration issues, configuration
        errors, and runtime problems.

        EXAMPLES:
        - Get all recent errors: ha_get_error_log(severity="error")
        - Get all log entries: ha_get_error_log()
        - Get warnings only: ha_get_error_log(severity="warning", limit=20)

        RETURNS:
        - entries: List of log entries with timestamp, level, message, source, count
        - summary: Count of entries by severity level
        """
        ws_client = None
        try:
            effective_limit = (
                coerce_int_param(
                    limit, param_name="limit", default=50, min_value=1, max_value=200
                )
                or 50
            )

            if severity and severity.lower() not in ("error", "warning", "info"):
                return {
                    "success": False,
                    "error": f"Invalid severity: {severity}",
                    "valid_severities": ["error", "warning", "info"],
                    "suggestion": "Use 'error', 'warning', or 'info'",
                }

            ws_client, error = await get_connected_ws_client(
                client.base_url, client.token
            )
            if error or ws_client is None:
                return error or {
                    "success": False,
                    "error": "Failed to establish WebSocket connection",
                }

            response = await ws_client.send_command("system_log/list")

            if not response.get("success"):
                return {
                    "success": False,
                    "error": "Failed to retrieve system log",
                    "details": response.get("error"),
                }

            entries = response.get("result", [])

            # Filter by severity if specified
            if severity:
                severity_lower = severity.lower()
                # Map severity to HA log levels
                level_map = {
                    "error": ("ERROR", "CRITICAL", "FATAL"),
                    "warning": ("WARNING",),
                    "info": ("INFO", "DEBUG"),
                }
                allowed_levels = level_map.get(severity_lower, ())
                entries = [
                    e for e in entries if e.get("level", "").upper() in allowed_levels
                ]

            # Build summary before limiting
            summary: dict[str, int] = {}
            for entry in entries:
                level = entry.get("level", "UNKNOWN").upper()
                summary[level] = summary.get(level, 0) + 1

            # Apply limit
            entries = entries[:effective_limit]

            # Format entries
            formatted = [
                {
                    "timestamp": entry.get("timestamp"),
                    "level": entry.get("level"),
                    "message": entry.get("message"),
                    "source": entry.get("source"),
                    "count": entry.get("count", 1),
                    "first_occurred": entry.get("first_occurred"),
                    "name": entry.get("name"),
                }
                for entry in entries
            ]

            return {
                "success": True,
                "entries": formatted,
                "total_entries": len(response.get("result", [])),
                "returned_entries": len(formatted),
                "severity_filter": severity,
                "summary": summary,
                "message": f"Retrieved {len(formatted)} log entries",
            }

        except Exception as e:
            logger.error(f"Failed to get error log: {e}")
            exception_to_structured_error(
                e,
                context={"operation": "get_error_log"},
                suggestions=[
                    "Ensure Home Assistant is running",
                    "Check WebSocket connectivity",
                ],
            )
            # exception_to_structured_error raises when raise_error=True (default)
            return {"success": False}  # unreachable, satisfies type checker

        finally:
            if ws_client:
                try:
                    await ws_client.disconnect()
                except Exception:
                    pass

    @mcp.tool(
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "tags": ["diagnostics"],
            "title": "Get Repair Items",
        }
    )
    @log_tool_usage
    async def ha_get_repair_items(
        domain: Annotated[
            str | None,
            Field(
                description="Filter by integration domain (e.g., 'zha', 'hacs'). "
                "If omitted, returns all repair items.",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Get Home Assistant repair/issue items.

        Returns items from the HA repairs system, which flags configuration
        issues, deprecated features, and required user actions. Items are
        grouped by severity (error, warning, info).

        EXAMPLES:
        - Get all repairs: ha_get_repair_items()
        - Get ZHA repairs only: ha_get_repair_items(domain="zha")

        RETURNS:
        - issues: List of repair items with domain, severity, description
        - by_severity: Issues grouped by severity level
        """
        ws_client = None
        try:
            ws_client, error = await get_connected_ws_client(
                client.base_url, client.token
            )
            if error or ws_client is None:
                return error or {
                    "success": False,
                    "error": "Failed to establish WebSocket connection",
                }

            response = await ws_client.send_command("repairs/list_issues")

            if not response.get("success"):
                return {
                    "success": False,
                    "error": "Failed to retrieve repair items",
                    "details": response.get("error"),
                }

            issues = response.get("result", {}).get("issues", [])

            # Filter by domain if specified
            if domain:
                domain_lower = domain.lower()
                issues = [
                    i for i in issues if i.get("domain", "").lower() == domain_lower
                ]

            # Group by severity
            by_severity: dict[str, list[dict[str, Any]]] = {}
            formatted_issues = []
            for issue in issues:
                formatted_issue = {
                    "issue_id": issue.get("issue_id"),
                    "domain": issue.get("domain"),
                    "severity": issue.get("severity", "unknown"),
                    "translation_key": issue.get("translation_key"),
                    "translation_placeholders": issue.get("translation_placeholders"),
                    "is_fixable": issue.get("is_fixable", False),
                    "learn_more_url": issue.get("learn_more_url"),
                    "created": issue.get("created"),
                }
                formatted_issues.append(formatted_issue)

                sev = issue.get("severity", "unknown")
                if sev not in by_severity:
                    by_severity[sev] = []
                by_severity[sev].append(formatted_issue)

            return {
                "success": True,
                "issues": formatted_issues,
                "total_issues": len(formatted_issues),
                "domain_filter": domain,
                "by_severity": by_severity,
                "message": f"Found {len(formatted_issues)} repair item(s)",
            }

        except Exception as e:
            logger.error(f"Failed to get repair items: {e}")
            exception_to_structured_error(
                e,
                context={"operation": "get_repair_items"},
                suggestions=[
                    "Ensure Home Assistant is running",
                    "Check WebSocket connectivity",
                ],
            )
            return {"success": False}

        finally:
            if ws_client:
                try:
                    await ws_client.disconnect()
                except Exception:
                    pass

    @mcp.tool(
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "tags": ["diagnostics"],
            "title": "System Health Check",
        }
    )
    @log_tool_usage
    async def ha_system_health_check(
        stale_threshold_hours: Annotated[
            float | str,
            Field(
                description="Hours after which a sensor is considered stale (default: 2.0)",
                default=2.0,
            ),
        ] = 2.0,
        battery_warning_pct: Annotated[
            int | str,
            Field(
                description="Battery percentage threshold for warnings (default: 20)",
                default=20,
            ),
        ] = 20,
        battery_critical_pct: Annotated[
            int | str,
            Field(
                description="Battery percentage threshold for critical alerts (default: 10)",
                default=10,
            ),
        ] = 10,
    ) -> dict[str, Any]:
        """
        Run a composite health check across the entire Home Assistant instance.

        Produces a structured health report covering:
        - Unavailable/unknown entities grouped by domain
        - Battery levels with critical and low warnings
        - Stale sensors that haven't updated recently
        - System log error/warning counts
        - Open repair items
        - Pending updates

        This is the primary diagnostic tool for getting a quick overview of
        system health in a single call.

        EXAMPLES:
        - Full health check: ha_system_health_check()
        - Custom thresholds: ha_system_health_check(stale_threshold_hours=4, battery_warning_pct=30)

        RETURNS:
        - Structured health report with sections for each check
        - overall_status: "healthy", "warnings", or "critical"
        - issue_count: Total number of issues found
        """
        ws_client = None
        try:
            # Parse numeric parameters
            try:
                stale_hours = float(stale_threshold_hours)
            except (ValueError, TypeError):
                stale_hours = 2.0

            warn_pct = (
                coerce_int_param(
                    battery_warning_pct,
                    "battery_warning_pct",
                    default=20,
                    min_value=1,
                    max_value=100,
                )
                or 20
            )
            crit_pct = (
                coerce_int_param(
                    battery_critical_pct,
                    "battery_critical_pct",
                    default=10,
                    min_value=1,
                    max_value=100,
                )
                or 10
            )

            # Get all states
            states = await client.get_states()
            now = datetime.now(UTC)
            stale_threshold = now - timedelta(hours=stale_hours)

            # 1. Unavailable / unknown entities
            unavailable_entities: list[dict[str, str]] = []
            for state in states:
                entity_id = state.get("entity_id", "")
                current_state = state.get("state", "")
                if current_state in ("unavailable", "unknown"):
                    unavailable_entities.append(
                        {
                            "entity_id": entity_id,
                            "state": current_state,
                            "domain": entity_id.split(".")[0]
                            if "." in entity_id
                            else "",
                        }
                    )

            # Group unavailable by domain
            unavailable_by_domain: dict[str, int] = {}
            for ent in unavailable_entities:
                d = ent.get("domain", "other")
                unavailable_by_domain[d] = unavailable_by_domain.get(d, 0) + 1

            # 2. Battery levels
            battery_critical: list[dict[str, Any]] = []
            battery_low: list[dict[str, Any]] = []
            for state in states:
                entity_id = state.get("entity_id", "")
                attrs = state.get("attributes", {})
                device_class = attrs.get("device_class", "")
                current_state = state.get("state", "")

                is_battery = device_class == "battery" or (
                    "battery" in entity_id and entity_id.startswith("sensor.")
                )
                if not is_battery or current_state in ("unavailable", "unknown", ""):
                    continue

                try:
                    level = float(current_state)
                except (ValueError, TypeError):
                    continue

                if level < crit_pct:
                    battery_critical.append(
                        {
                            "entity_id": entity_id,
                            "level": level,
                            "friendly_name": attrs.get("friendly_name", entity_id),
                        }
                    )
                elif level < warn_pct:
                    battery_low.append(
                        {
                            "entity_id": entity_id,
                            "level": level,
                            "friendly_name": attrs.get("friendly_name", entity_id),
                        }
                    )

            # 3. Stale sensors
            stale_sensors: list[dict[str, Any]] = []
            for state in states:
                entity_id = state.get("entity_id", "")
                if not entity_id.startswith("sensor."):
                    continue

                current_state = state.get("state", "")
                if current_state in ("unavailable", "unknown"):
                    continue

                last_updated_str = state.get("last_updated")
                if not last_updated_str:
                    continue

                try:
                    if last_updated_str.endswith("Z"):
                        last_updated_str = last_updated_str[:-1] + "+00:00"
                    last_updated = datetime.fromisoformat(last_updated_str)
                    if last_updated.tzinfo is None:
                        last_updated = last_updated.replace(tzinfo=UTC)

                    if last_updated < stale_threshold:
                        hours_stale = (now - last_updated).total_seconds() / 3600
                        stale_sensors.append(
                            {
                                "entity_id": entity_id,
                                "last_updated": last_updated.isoformat(),
                                "hours_stale": round(hours_stale, 1),
                            }
                        )
                except (ValueError, TypeError):
                    continue

            # 4. System log errors/warnings (via WebSocket)
            error_count = 0
            warning_count = 0
            try:
                ws_client, ws_error = await get_connected_ws_client(
                    client.base_url, client.token
                )
                if ws_client and not ws_error:
                    log_response = await ws_client.send_command("system_log/list")
                    if log_response.get("success"):
                        log_entries = log_response.get("result", [])
                        for entry in log_entries:
                            level = entry.get("level", "").upper()
                            if level in ("ERROR", "CRITICAL", "FATAL"):
                                error_count += 1
                            elif level == "WARNING":
                                warning_count += 1

                    # 5. Repair items
                    repair_count = 0
                    try:
                        repair_response = await ws_client.send_command(
                            "repairs/list_issues"
                        )
                        if repair_response.get("success"):
                            repair_count = len(
                                repair_response.get("result", {}).get("issues", [])
                            )
                    except Exception:
                        pass

            except Exception as ws_exc:
                logger.debug(f"WebSocket checks failed: {ws_exc}")
                repair_count = 0

            # 6. Pending updates
            pending_updates: list[dict[str, str]] = []
            for state in states:
                entity_id = state.get("entity_id", "")
                if entity_id.startswith("update.") and state.get("state") == "on":
                    attrs = state.get("attributes", {})
                    pending_updates.append(
                        {
                            "entity_id": entity_id,
                            "title": attrs.get("title", entity_id),
                            "installed_version": attrs.get(
                                "installed_version", "unknown"
                            ),
                            "latest_version": attrs.get("latest_version", "unknown"),
                        }
                    )

            # Determine overall status
            issue_count = (
                len(unavailable_entities)
                + len(battery_critical)
                + len(battery_low)
                + len(stale_sensors)
                + error_count
                + repair_count
                + len(pending_updates)
            )

            if battery_critical or error_count > 10:
                overall_status = "critical"
            elif (
                unavailable_entities
                or battery_low
                or stale_sensors
                or warning_count > 5
                or repair_count > 0
                or pending_updates
            ):
                overall_status = "warnings"
            else:
                overall_status = "healthy"

            return {
                "success": True,
                "overall_status": overall_status,
                "issue_count": issue_count,
                "unavailable_entities": {
                    "count": len(unavailable_entities),
                    "by_domain": unavailable_by_domain,
                    "entities": unavailable_entities[:50],
                    "truncated": len(unavailable_entities) > 50,
                },
                "battery": {
                    "critical": battery_critical,
                    "low": battery_low,
                    "critical_count": len(battery_critical),
                    "low_count": len(battery_low),
                },
                "stale_sensors": {
                    "count": len(stale_sensors),
                    "threshold_hours": stale_hours,
                    "sensors": stale_sensors[:30],
                    "truncated": len(stale_sensors) > 30,
                },
                "system_log": {
                    "error_count": error_count,
                    "warning_count": warning_count,
                },
                "repairs": {
                    "open_count": repair_count,
                },
                "pending_updates": {
                    "count": len(pending_updates),
                    "updates": pending_updates,
                },
                "message": f"Health check complete: {overall_status} ({issue_count} issue(s))",
            }

        except Exception as e:
            logger.error(f"Failed to run health check: {e}")
            exception_to_structured_error(
                e,
                context={"operation": "system_health_check"},
                suggestions=[
                    "Ensure Home Assistant is running and accessible",
                    "Try ha_get_overview() for basic system information",
                ],
            )
            return {"success": False}

        finally:
            if ws_client:
                try:
                    await ws_client.disconnect()
                except Exception:
                    pass

    @mcp.tool(
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "tags": ["diagnostics", "zigbee"],
            "title": "Get ZHA Network",
        }
    )
    @log_tool_usage
    async def ha_get_zha_network(
        min_lqi: Annotated[
            int | str | None,
            Field(
                description="Minimum LQI (Link Quality Indicator, 0-255) to filter weak devices. "
                "Devices below this threshold are flagged.",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Get Zigbee (ZHA) network devices with radio signal metrics.

        Returns ZHA devices including LQI (Link Quality Indicator) and RSSI
        (Received Signal Strength Indicator) values that are not available
        through the standard device registry.

        LQI ranges: 0-255 (higher is better). Generally:
        - 200+ : Excellent
        - 100-200: Good
        - 50-100 : Fair
        - <50    : Poor (may cause reliability issues)

        EXAMPLES:
        - Get all ZHA devices: ha_get_zha_network()
        - Find weak devices: ha_get_zha_network(min_lqi=50)

        RETURNS:
        - devices: List of ZHA devices with ieee, name, manufacturer, model, LQI, RSSI
        - weak_devices: Devices below the LQI threshold
        - network_summary: Counts by device type (router, end_device, coordinator)
        """
        ws_client = None
        try:
            effective_min_lqi = coerce_int_param(
                min_lqi, param_name="min_lqi", default=None, min_value=0, max_value=255
            )

            ws_client, error = await get_connected_ws_client(
                client.base_url, client.token
            )
            if error or ws_client is None:
                return error or {
                    "success": False,
                    "error": "Failed to establish WebSocket connection",
                }

            response = await ws_client.send_command("zha/devices")

            if not response.get("success"):
                error_msg = response.get("error", {})
                if isinstance(error_msg, dict):
                    error_msg = error_msg.get("message", "Unknown error")
                return {
                    "success": False,
                    "error": f"Failed to retrieve ZHA devices: {error_msg}",
                    "suggestions": [
                        "ZHA integration may not be installed or configured",
                        "Use ha_get_integration(query='zha') to check ZHA status",
                    ],
                }

            raw_devices = response.get("result", [])

            devices = []
            weak_devices = []
            type_counts: dict[str, int] = {}

            for dev in raw_devices:
                device_type = dev.get("device_type", "unknown")
                type_counts[device_type] = type_counts.get(device_type, 0) + 1

                lqi = dev.get("lqi")
                rssi = dev.get("rssi")

                device_info: dict[str, Any] = {
                    "ieee": dev.get("ieee"),
                    "name": dev.get("user_given_name") or dev.get("name"),
                    "manufacturer": dev.get("manufacturer"),
                    "model": dev.get("model"),
                    "device_type": device_type,
                    "lqi": lqi,
                    "rssi": rssi,
                    "available": dev.get("available", False),
                    "last_seen": dev.get("last_seen"),
                    "power_source": dev.get("power_source"),
                    "quirk_applied": dev.get("quirk_applied", False),
                }
                devices.append(device_info)

                # Track weak devices
                if effective_min_lqi is not None and lqi is not None:
                    if lqi < effective_min_lqi:
                        weak_devices.append(device_info)

            return {
                "success": True,
                "devices": devices,
                "total_devices": len(devices),
                "weak_devices": weak_devices if effective_min_lqi is not None else None,
                "weak_count": len(weak_devices)
                if effective_min_lqi is not None
                else None,
                "min_lqi_filter": effective_min_lqi,
                "network_summary": type_counts,
                "message": f"Found {len(devices)} ZHA device(s)",
            }

        except Exception as e:
            logger.error(f"Failed to get ZHA network: {e}")
            exception_to_structured_error(
                e,
                context={"operation": "get_zha_network"},
                suggestions=[
                    "Ensure ZHA integration is configured",
                    "Check that the Zigbee coordinator is connected",
                ],
            )
            return {"success": False}

        finally:
            if ws_client:
                try:
                    await ws_client.disconnect()
                except Exception:
                    pass

    @mcp.tool(
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "tags": ["diagnostics"],
            "title": "Find Anomalous Entities",
        }
    )
    @log_tool_usage
    async def ha_find_anomalous_entities(
        temp_min: Annotated[
            float | str,
            Field(
                description="Minimum plausible temperature in Celsius (default: -50)",
                default=-50,
            ),
        ] = -50,
        temp_max: Annotated[
            float | str,
            Field(
                description="Maximum plausible temperature in Celsius (default: 60)",
                default=60,
            ),
        ] = 60,
    ) -> dict[str, Any]:
        """
        Detect entities with impossible or suspicious values.

        Scans all entity states for anomalies including:
        - Battery sensors >100% or <0%
        - Temperature sensors outside configurable range
        - Humidity sensors outside 0-100%
        - Frozen sensors (last_updated identical to last_changed for >24h)

        EXAMPLES:
        - Default check: ha_find_anomalous_entities()
        - Custom temperature range: ha_find_anomalous_entities(temp_min=-30, temp_max=50)

        RETURNS:
        - anomalies: List of anomalies grouped by type
        - total_anomalies: Count of all detected anomalies
        """
        try:
            try:
                t_min = float(temp_min)
                t_max = float(temp_max)
            except (ValueError, TypeError):
                t_min = -50.0
                t_max = 60.0

            states = await client.get_states()
            now = datetime.now(UTC)
            frozen_threshold = now - timedelta(hours=24)

            impossible_values: list[dict[str, Any]] = []
            out_of_range: list[dict[str, Any]] = []
            frozen_sensors: list[dict[str, Any]] = []

            for state in states:
                entity_id = state.get("entity_id", "")
                current_state = state.get("state", "")
                attrs = state.get("attributes", {})
                device_class = attrs.get("device_class", "")

                if current_state in ("unavailable", "unknown", ""):
                    continue

                # Check battery sensors
                is_battery = device_class == "battery" or (
                    "battery" in entity_id and entity_id.startswith("sensor.")
                )
                if is_battery:
                    try:
                        val = float(current_state)
                        if val > 100 or val < 0:
                            impossible_values.append(
                                {
                                    "entity_id": entity_id,
                                    "value": val,
                                    "reason": f"Battery {'above 100%' if val > 100 else 'below 0%'}",
                                    "friendly_name": attrs.get(
                                        "friendly_name", entity_id
                                    ),
                                }
                            )
                    except (ValueError, TypeError):
                        pass

                # Check temperature sensors
                if device_class == "temperature" and entity_id.startswith("sensor."):
                    try:
                        val = float(current_state)
                        unit = attrs.get("unit_of_measurement", "°C")
                        # Convert Fahrenheit to Celsius for comparison
                        val_c = val
                        if "°F" in str(unit) or str(unit).strip() == "F":
                            val_c = (val - 32) * 5 / 9

                        if val_c < t_min or val_c > t_max:
                            out_of_range.append(
                                {
                                    "entity_id": entity_id,
                                    "value": val,
                                    "unit": unit,
                                    "reason": f"Temperature {val}{unit} outside range [{t_min}, {t_max}]°C",
                                    "friendly_name": attrs.get(
                                        "friendly_name", entity_id
                                    ),
                                }
                            )
                    except (ValueError, TypeError):
                        pass

                # Check humidity sensors
                if device_class == "humidity" and entity_id.startswith("sensor."):
                    try:
                        val = float(current_state)
                        if val > 100 or val < 0:
                            impossible_values.append(
                                {
                                    "entity_id": entity_id,
                                    "value": val,
                                    "reason": f"Humidity {'above 100%' if val > 100 else 'below 0%'}",
                                    "friendly_name": attrs.get(
                                        "friendly_name", entity_id
                                    ),
                                }
                            )
                    except (ValueError, TypeError):
                        pass

                # Check for frozen sensors
                if entity_id.startswith("sensor."):
                    last_updated_str = state.get("last_updated")
                    last_changed_str = state.get("last_changed")
                    if last_updated_str and last_changed_str:
                        try:
                            if last_updated_str.endswith("Z"):
                                last_updated_str = last_updated_str[:-1] + "+00:00"
                            if last_changed_str.endswith("Z"):
                                last_changed_str = last_changed_str[:-1] + "+00:00"

                            last_updated = datetime.fromisoformat(last_updated_str)
                            last_changed = datetime.fromisoformat(last_changed_str)

                            if last_updated.tzinfo is None:
                                last_updated = last_updated.replace(tzinfo=UTC)
                            if last_changed.tzinfo is None:
                                last_changed = last_changed.replace(tzinfo=UTC)

                            # Frozen = last_updated equals last_changed AND
                            # both are older than 24h
                            if (
                                abs((last_updated - last_changed).total_seconds()) < 1
                                and last_updated < frozen_threshold
                            ):
                                hours_frozen = (
                                    now - last_updated
                                ).total_seconds() / 3600
                                frozen_sensors.append(
                                    {
                                        "entity_id": entity_id,
                                        "last_updated": last_updated.isoformat(),
                                        "hours_frozen": round(hours_frozen, 1),
                                        "state": current_state,
                                        "friendly_name": attrs.get(
                                            "friendly_name", entity_id
                                        ),
                                    }
                                )
                        except (ValueError, TypeError):
                            pass

            total = len(impossible_values) + len(out_of_range) + len(frozen_sensors)

            return {
                "success": True,
                "total_anomalies": total,
                "anomalies": {
                    "impossible_values": impossible_values,
                    "out_of_range": out_of_range,
                    "frozen_sensors": frozen_sensors,
                },
                "counts": {
                    "impossible_values": len(impossible_values),
                    "out_of_range": len(out_of_range),
                    "frozen_sensors": len(frozen_sensors),
                },
                "thresholds": {
                    "temperature_range_celsius": [t_min, t_max],
                    "frozen_threshold_hours": 24,
                },
                "message": f"Found {total} anomaly(ies) across all entities",
            }

        except Exception as e:
            logger.error(f"Failed to find anomalous entities: {e}")
            exception_to_structured_error(
                e,
                context={"operation": "find_anomalous_entities"},
                suggestions=[
                    "Ensure Home Assistant is running",
                    "Check API connectivity",
                ],
            )
            return {"success": False}

    @mcp.tool(
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "tags": ["diagnostics"],
            "title": "Entity Diagnostics",
        }
    )
    @log_tool_usage
    async def ha_entity_diagnostics(
        entity_id: Annotated[
            str,
            Field(
                description="Entity ID to diagnose (e.g., 'sensor.living_room_temperature')"
            ),
        ],
        history_hours: Annotated[
            int | str,
            Field(
                description="Hours of history to retrieve (default: 24, max: 168)",
                default=24,
            ),
        ] = 24,
    ) -> dict[str, Any]:
        """
        Deep diagnostic dive on a single entity.

        Combines multiple data sources into a single diagnostic report:
        - Current state and all attributes
        - State history for the specified period
        - Device registry information
        - Related system log entries mentioning this entity

        EXAMPLES:
        - Diagnose a sensor: ha_entity_diagnostics(entity_id="sensor.bedroom_temperature")
        - Extended history: ha_entity_diagnostics(entity_id="light.kitchen", history_hours=72)

        RETURNS:
        - Combined diagnostic dict with state, history, device_info, and related_errors
        """
        ws_client = None
        try:
            effective_hours = (
                coerce_int_param(
                    history_hours,
                    param_name="history_hours",
                    default=24,
                    min_value=1,
                    max_value=168,
                )
                or 24
            )

            # Get current state
            try:
                entity_state = await client.get_entity_state(entity_id)
            except Exception as state_err:
                return {
                    "success": False,
                    "entity_id": entity_id,
                    "error": f"Entity not found: {entity_id}",
                    "details": str(state_err),
                    "suggestions": [
                        "Verify the entity_id is correct",
                        "Use ha_search_entities() to find the entity",
                    ],
                }

            # Connect WebSocket for additional data
            ws_client, ws_error = await get_connected_ws_client(
                client.base_url, client.token
            )

            # Gather additional data concurrently
            history_data: list[dict[str, Any]] = []
            device_info: dict[str, Any] | None = None
            related_errors: list[dict[str, Any]] = []

            if ws_client and not ws_error:
                # Prepare concurrent tasks
                tasks: dict[str, Any] = {}

                # History
                now = datetime.now(UTC)
                start = now - timedelta(hours=effective_hours)
                tasks["history"] = ws_client.send_command(
                    "history/history_during_period",
                    start_time=start.isoformat(),
                    end_time=now.isoformat(),
                    entity_ids=[entity_id],
                    minimal_response=True,
                    significant_changes_only=True,
                    no_attributes=True,
                )

                # Entity registry (for device_id)
                tasks["entity_reg"] = ws_client.send_command(
                    "config/entity_registry/get",
                    entity_id=entity_id,
                )

                # System log
                tasks["system_log"] = ws_client.send_command("system_log/list")

                # Run concurrently
                results = await asyncio.gather(*tasks.values(), return_exceptions=True)
                result_keys = list(tasks.keys())
                result_map = dict(zip(result_keys, results, strict=True))

                # Process history
                hist_result = result_map.get("history")
                if isinstance(hist_result, dict) and hist_result.get("success"):
                    raw_history = hist_result.get("result", {}).get(entity_id, [])
                    history_data.extend(
                        {
                            "state": entry.get("s", entry.get("state")),
                            "last_changed": entry.get("lc", entry.get("last_changed")),
                        }
                        for entry in raw_history[-100:]
                    )

                # Process entity registry → device info
                reg_result = result_map.get("entity_reg")
                if isinstance(reg_result, dict) and reg_result.get("success"):
                    reg_data = reg_result.get("result", {})
                    device_id = reg_data.get("device_id")

                    device_info = {
                        "platform": reg_data.get("platform"),
                        "config_entry_id": reg_data.get("config_entry_id"),
                        "disabled_by": reg_data.get("disabled_by"),
                        "hidden_by": reg_data.get("hidden_by"),
                        "device_id": device_id,
                    }

                    # Get device details if we have a device_id
                    if device_id:
                        try:
                            dev_result = await ws_client.send_command(
                                "config/device_registry/get",
                                device_id=device_id,
                            )
                            if isinstance(dev_result, dict) and dev_result.get(
                                "success"
                            ):
                                dev_data = dev_result.get("result", {})
                                device_info.update(
                                    {
                                        "device_name": dev_data.get("name_by_user")
                                        or dev_data.get("name"),
                                        "manufacturer": dev_data.get("manufacturer"),
                                        "model": dev_data.get("model"),
                                        "sw_version": dev_data.get("sw_version"),
                                        "hw_version": dev_data.get("hw_version"),
                                        "via_device_id": dev_data.get("via_device_id"),
                                    }
                                )
                        except Exception:
                            pass

                # Process system log for related errors
                log_result = result_map.get("system_log")
                if isinstance(log_result, dict) and log_result.get("success"):
                    log_entries = log_result.get("result", [])
                    for entry in log_entries:
                        msg = entry.get("message", "")
                        source = str(entry.get("source", ""))
                        if entity_id in msg or entity_id in source:
                            related_errors.append(
                                {
                                    "timestamp": entry.get("timestamp"),
                                    "level": entry.get("level"),
                                    "message": msg[:500],
                                    "count": entry.get("count", 1),
                                }
                            )

            return {
                "success": True,
                "entity_id": entity_id,
                "current_state": {
                    "state": entity_state.get("state"),
                    "attributes": entity_state.get("attributes", {}),
                    "last_changed": entity_state.get("last_changed"),
                    "last_updated": entity_state.get("last_updated"),
                },
                "history": {
                    "period_hours": effective_hours,
                    "entries": history_data,
                    "entry_count": len(history_data),
                },
                "device_info": device_info,
                "related_errors": related_errors,
                "related_error_count": len(related_errors),
                "message": f"Diagnostic report for {entity_id}",
            }

        except Exception as e:
            logger.error(f"Failed to run entity diagnostics: {e}")
            exception_to_structured_error(
                e,
                context={"entity_id": entity_id, "operation": "entity_diagnostics"},
                suggestions=[
                    "Verify the entity_id exists",
                    "Check Home Assistant connectivity",
                ],
            )
            return {"success": False}

        finally:
            if ws_client:
                try:
                    await ws_client.disconnect()
                except Exception:
                    pass

    @mcp.tool(
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "tags": ["diagnostics", "automation"],
            "title": "Automation Report",
        }
    )
    @log_tool_usage
    async def ha_automation_report(
        stale_days: Annotated[
            int | str,
            Field(
                description="Days after which an automation is considered stale (default: 30)",
                default=30,
            ),
        ] = 30,
        include_traces: Annotated[
            bool | str,
            Field(
                description="Include latest trace summary for errored automations (default: false)",
                default=False,
            ),
        ] = False,
    ) -> dict[str, Any]:
        """
        Get an overview of all automations with health indicators.

        Analyzes all automations to flag:
        - Disabled automations
        - Automations that haven't triggered in a configurable period
        - Automations with recent errors (if include_traces is true)

        EXAMPLES:
        - Basic report: ha_automation_report()
        - Include error traces: ha_automation_report(include_traces=True, stale_days=14)

        RETURNS:
        - automations: List of automations with health status
        - summary: Counts of healthy, stale, disabled, errored automations
        """
        ws_client = None
        try:
            effective_stale_days = (
                coerce_int_param(
                    stale_days,
                    param_name="stale_days",
                    default=30,
                    min_value=1,
                    max_value=365,
                )
                or 30
            )

            # Handle include_traces as bool or string
            if isinstance(include_traces, str):
                traces_enabled = include_traces.lower() in ("true", "1", "yes", "on")
            else:
                traces_enabled = bool(include_traces)

            states = await client.get_states()
            now = datetime.now(UTC)
            stale_threshold = now - timedelta(days=effective_stale_days)

            automations = []
            disabled_count = 0
            stale_count = 0
            never_triggered_count = 0
            healthy_count = 0
            errored_count = 0

            for state in states:
                entity_id = state.get("entity_id", "")
                if not entity_id.startswith("automation."):
                    continue

                current_state = state.get("state", "")
                attrs = state.get("attributes", {})
                last_triggered = attrs.get("last_triggered")

                # Determine health status
                status = "healthy"
                if current_state == "off":
                    status = "disabled"
                    disabled_count += 1
                elif not last_triggered:
                    status = "never_triggered"
                    never_triggered_count += 1
                else:
                    try:
                        lt_str = str(last_triggered)
                        if lt_str.endswith("Z"):
                            lt_str = lt_str[:-1] + "+00:00"
                        lt = datetime.fromisoformat(lt_str)
                        if lt.tzinfo is None:
                            lt = lt.replace(tzinfo=UTC)
                        if lt < stale_threshold:
                            status = "stale"
                            stale_count += 1
                        else:
                            healthy_count += 1
                    except (ValueError, TypeError):
                        healthy_count += 1

                automation_info: dict[str, Any] = {
                    "entity_id": entity_id,
                    "friendly_name": attrs.get("friendly_name", entity_id),
                    "state": current_state,
                    "status": status,
                    "last_triggered": last_triggered,
                    "mode": attrs.get("mode", "single"),
                }
                automations.append(automation_info)

            # Optionally fetch traces for errored automations
            if traces_enabled:
                try:
                    ws_client, ws_error = await get_connected_ws_client(
                        client.base_url, client.token
                    )
                    if ws_client and not ws_error:
                        for auto in automations:
                            if auto["state"] == "off":
                                continue  # Skip disabled
                            auto_entity_id = auto["entity_id"]
                            object_id = auto_entity_id.split(".", 1)[1]
                            try:
                                # Resolve unique_id
                                reg_result = await ws_client.send_command(
                                    "config/entity_registry/get",
                                    entity_id=auto_entity_id,
                                )
                                item_id = object_id
                                if isinstance(reg_result, dict) and reg_result.get(
                                    "success"
                                ):
                                    uid = reg_result.get("result", {}).get("unique_id")
                                    if uid:
                                        item_id = uid

                                trace_result = await ws_client.send_command(
                                    "trace/list",
                                    domain="automation",
                                    item_id=item_id,
                                )
                                if isinstance(trace_result, dict) and trace_result.get(
                                    "success"
                                ):
                                    traces = trace_result.get("result", [])
                                    if traces:
                                        latest = traces[0]
                                        auto["latest_trace"] = {
                                            "run_id": latest.get("run_id"),
                                            "timestamp": latest.get("timestamp"),
                                            "state": latest.get("state"),
                                            "error": latest.get("error"),
                                        }
                                        if latest.get("error"):
                                            auto["status"] = "errored"
                                            errored_count += 1
                                            # Adjust healthy count
                                            if auto.get("status") == "healthy":
                                                healthy_count -= 1
                            except Exception:
                                pass
                except Exception:
                    pass

            return {
                "success": True,
                "automations": automations,
                "total_automations": len(automations),
                "summary": {
                    "healthy": healthy_count,
                    "disabled": disabled_count,
                    "stale": stale_count,
                    "never_triggered": never_triggered_count,
                    "errored": errored_count,
                },
                "stale_threshold_days": effective_stale_days,
                "include_traces": traces_enabled,
                "message": (
                    f"Found {len(automations)} automations: "
                    f"{healthy_count} healthy, {disabled_count} disabled, "
                    f"{stale_count} stale, {never_triggered_count} never triggered"
                ),
            }

        except Exception as e:
            logger.error(f"Failed to generate automation report: {e}")
            exception_to_structured_error(
                e,
                context={"operation": "automation_report"},
                suggestions=[
                    "Ensure Home Assistant is running",
                    "Check API connectivity",
                ],
            )
            return {"success": False}

        finally:
            if ws_client:
                try:
                    await ws_client.disconnect()
                except Exception:
                    pass

    @mcp.tool(
        annotations={
            "destructiveHint": True,
            "readOnlyHint": False,
            "idempotentHint": False,
            "tags": ["diagnostics", "fix"],
            "title": "Fix Entity",
        }
    )
    @log_tool_usage
    async def ha_fix_entity(
        entity_id: Annotated[
            str,
            Field(description="Entity ID to fix (e.g., 'sensor.bedroom_temperature')"),
        ],
        action: Annotated[
            str,
            Field(
                description="Fix action: 'reload_integration' (reload config entry), "
                "'enable_entity' (re-enable disabled entity), or "
                "'restart_integration' (disable + re-enable config entry)"
            ),
        ],
    ) -> dict[str, Any]:
        """
        Apply a targeted diagnostic fix to an entity.

        Available actions:
        - reload_integration: Reload the entity's config entry (integration)
        - enable_entity: Re-enable a disabled entity in the entity registry
        - restart_integration: Disable then re-enable the entity's config entry

        **WARNING**: restart_integration temporarily makes the integration unavailable.

        EXAMPLES:
        - Reload integration: ha_fix_entity(entity_id="sensor.temp", action="reload_integration")
        - Re-enable entity: ha_fix_entity(entity_id="sensor.temp", action="enable_entity")
        - Restart integration: ha_fix_entity(entity_id="sensor.temp", action="restart_integration")

        RETURNS:
        - Action result with before/after state
        """
        ws_client = None
        try:
            valid_actions = (
                "reload_integration",
                "enable_entity",
                "restart_integration",
            )
            if action not in valid_actions:
                return {
                    "success": False,
                    "error": f"Invalid action: {action}",
                    "valid_actions": list(valid_actions),
                    "suggestion": f"Use one of: {', '.join(valid_actions)}",
                }

            # Get before state
            try:
                before_state = await client.get_entity_state(entity_id)
            except Exception:
                before_state = None

            ws_client, ws_error = await get_connected_ws_client(
                client.base_url, client.token
            )
            if ws_error or ws_client is None:
                return ws_error or {
                    "success": False,
                    "error": "Failed to establish WebSocket connection",
                }

            # Get entity registry info to find config_entry_id
            reg_result = await ws_client.send_command(
                "config/entity_registry/get",
                entity_id=entity_id,
            )

            if not reg_result.get("success"):
                return {
                    "success": False,
                    "entity_id": entity_id,
                    "error": f"Entity not found in registry: {entity_id}",
                    "suggestions": [
                        "Verify the entity_id is correct",
                        "Use ha_search_entities() to find the entity",
                    ],
                }

            reg_data = reg_result.get("result", {})
            config_entry_id = reg_data.get("config_entry_id")

            if action == "enable_entity":
                # Re-enable a disabled entity
                update_result = await ws_client.send_command(
                    "config/entity_registry/update",
                    entity_id=entity_id,
                    disabled_by=None,
                )

                if not update_result.get("success"):
                    return {
                        "success": False,
                        "entity_id": entity_id,
                        "action": action,
                        "error": "Failed to enable entity",
                        "details": update_result.get("error"),
                    }

                return {
                    "success": True,
                    "entity_id": entity_id,
                    "action": action,
                    "before_disabled_by": reg_data.get("disabled_by"),
                    "message": f"Entity {entity_id} has been re-enabled",
                    "note": "Entity may take a moment to become available",
                }

            elif action == "reload_integration":
                if not config_entry_id:
                    return {
                        "success": False,
                        "entity_id": entity_id,
                        "action": action,
                        "error": "No config entry found for this entity",
                        "suggestion": "This entity may not be part of a reloadable integration",
                    }

                reload_result = await ws_client.send_command(
                    "config_entries/reload",
                    entry_id=config_entry_id,
                )

                success = reload_result.get("success", False)
                return {
                    "success": success,
                    "entity_id": entity_id,
                    "action": action,
                    "config_entry_id": config_entry_id,
                    "before_state": before_state.get("state") if before_state else None,
                    "message": (
                        f"Integration reloaded for {entity_id}"
                        if success
                        else "Failed to reload integration"
                    ),
                    "details": None if success else reload_result.get("error"),
                    "note": "Entity state may take a moment to update after reload",
                }

            elif action == "restart_integration":
                if not config_entry_id:
                    return {
                        "success": False,
                        "entity_id": entity_id,
                        "action": action,
                        "error": "No config entry found for this entity",
                        "suggestion": "This entity may not be part of a restartable integration",
                    }

                # Disable
                disable_result = await ws_client.send_command(
                    "config_entries/disable",
                    entry_id=config_entry_id,
                    disabled_by="user",
                )

                if not disable_result.get("success"):
                    return {
                        "success": False,
                        "entity_id": entity_id,
                        "action": action,
                        "error": "Failed to disable integration",
                        "details": disable_result.get("error"),
                    }

                # Brief pause to allow cleanup
                await asyncio.sleep(2)

                # Re-enable
                enable_result = await ws_client.send_command(
                    "config_entries/enable",
                    entry_id=config_entry_id,
                )

                success = enable_result.get("success", False)
                return {
                    "success": success,
                    "entity_id": entity_id,
                    "action": action,
                    "config_entry_id": config_entry_id,
                    "before_state": before_state.get("state") if before_state else None,
                    "message": (
                        f"Integration restarted for {entity_id}"
                        if success
                        else "Failed to re-enable integration"
                    ),
                    "details": None if success else enable_result.get("error"),
                    "note": "Integration may take a moment to fully restart",
                }

            # Should not reach here
            return {"success": False, "error": f"Unhandled action: {action}"}

        except Exception as e:
            logger.error(f"Failed to fix entity {entity_id}: {e}")
            exception_to_structured_error(
                e,
                context={"entity_id": entity_id, "action": action},
                suggestions=[
                    "Verify the entity_id exists",
                    "Check Home Assistant connectivity",
                    "Try a simpler action like reload_integration first",
                ],
            )
            return {"success": False}

        finally:
            if ws_client:
                try:
                    await ws_client.disconnect()
                except Exception:
                    pass
