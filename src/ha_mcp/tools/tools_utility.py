"""
Utility tools for Home Assistant MCP server.

This module provides general-purpose utility tools including log access,
template evaluation, and domain documentation retrieval.
"""

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastmcp.exceptions import ToolError

from ..client.rest_client import HomeAssistantAPIError, HomeAssistantConnectionError
from ..errors import ErrorCode, create_error_response
from .helpers import exception_to_structured_error, log_tool_usage, raise_tool_error
from .util_helpers import (
    add_timezone_metadata,
    coerce_bool_param,
    coerce_int_param,
    normalize_log_level,
)

logger = logging.getLogger(__name__)

# Fields to keep in compact logbook mode (strips attribute dictionaries
# and other bulky fields that can cause context exhaustion — see #683)
COMPACT_LOGBOOK_FIELDS = {"when", "entity_id", "state", "name", "message", "domain", "context_id", "source"}


def _compact_logbook_entries(entries: list[Any]) -> list[dict[str, Any]]:
    """Strip logbook entries to essential fields only.

    Returns entries with only the fields in COMPACT_LOGBOOK_FIELDS,
    filtering out any non-dict entries.
    """
    return [
        {k: v for k, v in entry.items() if k in COMPACT_LOGBOOK_FIELDS}
        for entry in entries
        if isinstance(entry, dict)
    ]


def register_utility_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant utility tools."""

    # Default and maximum limits for log entries
    DEFAULT_LIMIT = 50
    DEFAULT_LOG_LIMIT = 100
    MAX_LIMIT = 500

    def _coerce_limit(
        limit: int | str | None,
        default: int = DEFAULT_LIMIT,
        suggestion_example: str = "50",
    ) -> int:
        """Coerce and validate a limit parameter, raising a structured tool error on failure."""
        try:
            return coerce_int_param(limit, param_name="limit", default=default, min_value=1, max_value=MAX_LIMIT)
        except ValueError as e:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    str(e),
                    suggestions=[f"Provide limit as an integer (e.g., {suggestion_example})"],
                )
            )
    # Regex to match log level at the start of a log line
    _LOG_LEVEL_RE = re.compile(
        r"(?:^|\s)(DEBUG|INFO|WARNING|ERROR|CRITICAL)(?:\s|:|\])", re.IGNORECASE
    )

    # Valid log level values
    VALID_LOG_LEVELS = ("ERROR", "WARNING", "INFO", "DEBUG")

    @mcp.tool(
        tags={"History & Statistics"},
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "title": "Get Logs",
        }
    )
    @log_tool_usage
    async def ha_get_logs(
        source: Literal["logbook", "system", "error_log", "supervisor", "logger"] = "logbook",
        # Shared parameters
        limit: int | str | None = None,
        search: str | None = None,
        # Logbook-specific (ignored for other sources)
        hours_back: int | str = 1,
        entity_id: str | None = None,
        end_time: str | None = None,
        offset: int | str = 0,
        compact: bool | str = True,
        # System/error_log-specific
        level: str | None = None,
        # Supervisor-specific
        slug: str | None = None,
    ) -> dict[str, Any]:
        """
        Get Home Assistant logs from various sources.

        **Sources:**
        - "logbook" (default): Entity state change history with pagination
        - "system": Structured system log entries (errors, warnings) via system_log/list
        - "error_log": Raw home-assistant.log text
        - "supervisor": Add-on container logs (requires slug parameter)
        - "logger": Effective log level per integration via logger/log_info (confirms logger.set_level changes took effect)

        **Shared params:** limit, search (keyword filter on entries/lines; matches integration domain for source='logger')
        **Logbook params:** hours_back, entity_id, end_time, offset, compact (default True — strips attribute dicts to save context)
        **System/error_log params:** level (ERROR, WARNING, INFO, DEBUG)
        **Supervisor params:** slug (add-on slug, e.g. "core_mosquitto")
        """

        # Validate level if provided
        if level is not None:
            level_upper = level.strip().upper()
            if level_upper not in VALID_LOG_LEVELS:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"Invalid level '{level}'. Must be one of: {', '.join(VALID_LOG_LEVELS)}",
                        suggestions=["Use level='ERROR' to see only errors"],
                    )
                )
            level = level_upper

        # Collect warnings about source-incompatible parameters
        warnings: list[str] = []
        if source != "logbook" and any(p is not None for p in [entity_id, end_time]):
            ignored = [p for p, v in [("entity_id", entity_id), ("end_time", end_time)] if v is not None]
            warnings.append(
                f"Parameters {', '.join(ignored)} only apply to source='logbook'; "
                f"ignored for source='{source}'"
            )
        if source in ("logbook", "logger", "supervisor") and level is not None:
            warnings.append(
                "Parameter 'level' only applies to source='system' or 'error_log'; "
                f"ignored for source='{source}'"
            )

        # --- source="logbook" ---
        if source == "logbook":
            result = await _get_logbook(
                hours_back=hours_back,
                entity_id=entity_id,
                end_time=end_time,
                limit=limit,
                offset=offset,
                search=search,
                compact=compact,
            )
            if warnings:
                result["warnings"] = warnings
            return result

        # --- source="system" ---
        if source == "system":
            result = await _get_system_log(
                limit=limit,
                search=search,
                level=level,
            )
            if warnings:
                result["warnings"] = warnings
            return result

        # --- source="error_log" ---
        if source == "error_log":
            result = await _get_error_log(
                limit=limit,
                search=search,
                level=level,
            )
            if warnings:
                result["warnings"] = warnings
            return result

        # --- source="logger" ---
        if source == "logger":
            result = await _get_logger_info(limit=limit, search=search)
            if warnings:
                result["warnings"] = warnings
            return result

        # --- source="supervisor" ---
        # source == "supervisor" (Literal type guarantees this)
        if not slug:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "The 'slug' parameter is required for source='supervisor'",
                    suggestions=[
                        "Provide the add-on slug, e.g. slug='core_mosquitto'",
                        "Use ha_get_addon() to list installed add-on slugs",
                    ],
                )
            )
        result = await _get_supervisor_log(
            slug=slug,
            limit=limit,
            search=search,
        )
        if warnings:
            result["warnings"] = warnings
        return result

    # ---- Logbook source ----

    async def _get_logbook(
        hours_back: int | str = 1,
        entity_id: str | None = None,
        end_time: str | None = None,
        limit: int | str | None = None,
        offset: int | str = 0,
        search: str | None = None,
        compact: bool | str = True,
    ) -> dict[str, Any]:
        """Fetch logbook entries with search and pagination."""

        # Coerce parameters with string handling for AI tools
        compact_bool = coerce_bool_param(compact, "compact", default=True)
        try:
            hours_back_int = coerce_int_param(
                hours_back,
                param_name="hours_back",
                default=1,
                min_value=1,
            )
        except ValueError as e:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    str(e),
                    suggestions=["Provide hours_back as an integer (e.g., 24)"],
                )
            )

        effective_limit = _coerce_limit(limit)

        try:
            offset_int = coerce_int_param(
                offset,
                param_name="offset",
                default=0,
                min_value=0,
            )
        except ValueError as e:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    str(e),
                    suggestions=["Provide offset as an integer (e.g., 0)"],
                )
            )

        # Calculate start time
        if end_time:
            end_dt = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
        else:
            end_dt = datetime.now(UTC)

        start_dt = end_dt - timedelta(hours=hours_back_int)
        start_timestamp = start_dt.isoformat()

        try:
            response = await client.get_logbook(
                entity_id=entity_id, start_time=start_timestamp, end_time=end_time
            )

            # Apply search filter if provided
            filters_applied: dict[str, str] = {}
            if search and isinstance(response, list):
                search_lower = search.lower()
                response = [
                    e
                    for e in response
                    if search_lower in str(e.get("name", "")).lower()
                    or search_lower in str(e.get("message", "")).lower()
                    or search_lower in str(e.get("entity_id", "")).lower()
                ]
                filters_applied["search"] = search

            # Get total count before pagination
            total_entries = len(response) if isinstance(response, list) else 1

            # Apply pagination
            if isinstance(response, list):
                paginated_entries = response[offset_int : offset_int + effective_limit]
                has_more = (offset_int + effective_limit) < total_entries
            else:
                paginated_entries = response
                has_more = False

            # In compact mode, strip entries to essential fields only.
            # This prevents full attribute dictionaries from exhausting
            # the LLM context window during debugging workflows.
            if compact_bool and isinstance(paginated_entries, list):
                paginated_entries = _compact_logbook_entries(paginated_entries)

            logbook_data: dict[str, Any] = {
                "success": True,
                "source": "logbook",
                "entries": paginated_entries,
                "period": f"{hours_back_int} hours back from {end_dt.isoformat()}",
                "start_time": start_timestamp,
                "end_time": end_dt.isoformat(),
                "entity_filter": entity_id,
                "total_entries": total_entries,
                "returned_entries": len(paginated_entries)
                if isinstance(paginated_entries, list)
                else 1,
                "limit": effective_limit,
                "offset": offset_int,
                "has_more": has_more,
            }
            if filters_applied:
                logbook_data["filters_applied"] = filters_applied

            # Add helpful message when results are truncated
            if has_more:
                next_offset = offset_int + effective_limit
                # Build complete parameter string for reproducible pagination
                param_parts = [
                    f"hours_back={hours_back_int}",
                    f"limit={effective_limit}",
                    f"offset={next_offset}",
                ]
                if entity_id:
                    param_parts.append(f"entity_id={entity_id}")
                if end_time:
                    param_parts.append(f"end_time={end_time}")
                if search:
                    param_parts.append(f"search={search}")
                if not compact_bool:
                    param_parts.append("compact=False")

                param_str = ", ".join(param_parts)
                logbook_data["pagination_hint"] = (
                    f"Showing entries {offset_int + 1}-{offset_int + len(paginated_entries)} of {total_entries}. "
                    f"To get the next page, use: ha_get_logs({param_str})"
                )

            return await add_timezone_metadata(client, logbook_data)

        except ToolError:
            raise
        except Exception as e:
            error_str = str(e)
            suggestions = [
                "Try reducing 'hours_back' parameter (e.g., from 24 to 1 hour)",
                "Add a specific 'entity_id' filter to narrow down results",
            ]

            # Detect 500 errors (server crash from heavy query)
            if "500" in error_str:
                suggestions = [
                    "The query returned too many results causing a server error (500).",
                    "This often happens with very active entities or long time periods.",
                    "Try reducing 'hours_back' parameter (e.g., from 24 to 1 hour)",
                    "Add a specific 'entity_id' filter to narrow down results",
                    "If debugging an automation, filter by that automation's entity_id",
                    "Use ha_bug_report tool to check Home Assistant logs for crash details",
                ]

            exception_to_structured_error(
                e,
                context={
                    "period": f"{hours_back_int} hours back from {end_dt.isoformat()}",
                },
                suggestions=suggestions,
            )

    # ---- System log source ----

    async def _get_system_log(
        limit: int | str | None = None,
        search: str | None = None,
        level: str | None = None,
    ) -> dict[str, Any]:
        """Fetch structured system log entries via system_log/list."""
        effective_limit = _coerce_limit(limit)

        try:
            result = await client.send_websocket_message({"type": "system_log/list"})

            if not result.get("success"):
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        result.get("error", "Failed to retrieve system log"),
                        suggestions=["Check Home Assistant connection"],
                    )
                )

            entries = result.get("result", [])
            if not isinstance(entries, list):
                entries = []

            # Apply filters
            filters_applied: dict[str, str] = {}

            if level:
                entries = [
                    e for e in entries if str(e.get("level", "")).upper() == level
                ]
                filters_applied["level"] = level

            if search:
                search_lower = search.lower()
                entries = [
                    e
                    for e in entries
                    if search_lower in str(e.get("message", "")).lower()
                    or search_lower in str(e.get("name", "")).lower()
                ]
                filters_applied["search"] = search

            total_entries = len(entries)

            # Apply limit
            entries = entries[:effective_limit]

            data: dict[str, Any] = {
                "success": True,
                "source": "system",
                "entries": entries,
                "total_entries": total_entries,
                "returned_entries": len(entries),
                "limit": effective_limit,
            }
            if filters_applied:
                data["filters_applied"] = filters_applied

            return data

        except ToolError:
            raise
        except (
            HomeAssistantConnectionError,
            HomeAssistantAPIError,
            TimeoutError,
            OSError,
        ) as e:
            exception_to_structured_error(
                e,
                context={"source": "system"},
                suggestions=[
                    "Check Home Assistant WebSocket connection",
                    "Verify system_log integration is enabled",
                ],
            )

    # ---- Error log source ----

    async def _get_error_log(
        limit: int | str | None = None,
        search: str | None = None,
        level: str | None = None,
    ) -> dict[str, Any]:
        """Fetch raw error log text from home-assistant.log."""
        effective_limit = _coerce_limit(limit, default=DEFAULT_LOG_LIMIT, suggestion_example="100")

        try:
            raw_log = await client.get_error_log()
            lines = raw_log.splitlines() if raw_log else []

            # Apply filters
            filters_applied: dict[str, str] = {}

            if level:

                def _line_has_level(ln: str, target: str) -> bool:
                    m = _LOG_LEVEL_RE.search(ln)
                    return m is not None and m.group(1).upper() == target

                lines = [ln for ln in lines if _line_has_level(ln, level)]
                filters_applied["level"] = level

            if search:
                search_lower = search.lower()
                lines = [ln for ln in lines if search_lower in ln.lower()]
                filters_applied["search"] = search

            total_lines = len(lines)

            # Return the LAST N lines (most recent)
            lines = lines[-effective_limit:]

            data: dict[str, Any] = {
                "success": True,
                "source": "error_log",
                "log": "\n".join(lines),
                "total_lines": total_lines,
                "returned_lines": len(lines),
                "limit": effective_limit,
                "note": "Returned the most recent log lines matching filters",
            }
            if filters_applied:
                data["filters_applied"] = filters_applied

            return data

        except ToolError:
            raise
        except (
            HomeAssistantConnectionError,
            HomeAssistantAPIError,
            TimeoutError,
            OSError,
        ) as e:
            exception_to_structured_error(
                e,
                context={"source": "error_log"},
                suggestions=[
                    "Check Home Assistant connection",
                    "The error log may be empty if no errors have occurred",
                ],
            )

    # ---- Logger info source ----

    async def _get_logger_info(
        limit: int | str | None = None,
        search: str | None = None,
    ) -> dict[str, Any]:
        """Fetch per-integration log levels via the ``logger/log_info`` WS command."""
        effective_limit = _coerce_limit(limit)

        try:
            result = await client.send_websocket_message({"type": "logger/log_info"})

            if not result.get("success"):
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        result.get("error", "Failed to retrieve logger info"),
                        suggestions=[
                            "Verify the 'logger' integration is enabled in Home Assistant",
                            "Check Home Assistant WebSocket connection",
                        ],
                    )
                )

            raw_entries = result.get("result", [])
            if not isinstance(raw_entries, list):
                raw_entries = []

            loggers: list[dict[str, Any]] = []
            for entry in raw_entries:
                if not isinstance(entry, dict):
                    continue
                domain = entry.get("domain")
                if not isinstance(domain, str) or not domain:
                    continue
                raw_level = entry.get("level")
                level_name = normalize_log_level(raw_level)
                if level_name is None:
                    continue
                loggers.append(
                    {
                        "domain": domain,
                        "level": level_name,
                        "level_raw": raw_level if isinstance(raw_level, int) else None,
                    }
                )

            filters_applied: dict[str, str] = {}
            if search:
                search_lower = search.lower()
                loggers = [
                    entry for entry in loggers
                    if search_lower in entry["domain"].lower()
                ]
                filters_applied["search"] = search

            loggers.sort(key=lambda entry: entry["domain"])

            total_entries = len(loggers)
            loggers = loggers[:effective_limit]

            data: dict[str, Any] = {
                "success": True,
                "source": "logger",
                "loggers": loggers,
                "total_entries": total_entries,
                "returned_entries": len(loggers),
                "limit": effective_limit,
            }
            if filters_applied:
                data["filters_applied"] = filters_applied

            return data

        except ToolError:
            raise
        except (
            HomeAssistantConnectionError,
            HomeAssistantAPIError,
            TimeoutError,
            OSError,
        ) as e:
            exception_to_structured_error(
                e,
                context={"source": "logger"},
                suggestions=[
                    "Check Home Assistant WebSocket connection",
                    "Verify the 'logger' integration is enabled",
                ],
            )

    # ---- Supervisor log source ----

    async def _get_supervisor_log(
        slug: str,
        limit: int | str | None = None,
        search: str | None = None,
    ) -> dict[str, Any]:
        """Fetch add-on container logs via HA Core's Supervisor REST proxy.

        Routes through `/api/hassio/addons/{slug}/logs` (returned as
        text/plain) instead of the `supervisor/api` websocket path, which
        always fails because HA Core's proxy tries to JSON-decode the
        text body. See #950.
        """
        effective_limit = _coerce_limit(limit, default=DEFAULT_LOG_LIMIT, suggestion_example="100")

        try:
            log_text = await client.get_addon_logs(slug)

            lines = log_text.splitlines() if log_text else []

            # Apply filters
            filters_applied: dict[str, str] = {}

            if search:
                search_lower = search.lower()
                lines = [ln for ln in lines if search_lower in ln.lower()]
                filters_applied["search"] = search

            total_lines = len(lines)

            # Return the LAST N lines (most recent)
            lines = lines[-effective_limit:]

            data: dict[str, Any] = {
                "success": True,
                "source": "supervisor",
                "slug": slug,
                "log": "\n".join(lines),
                "total_lines": total_lines,
                "returned_lines": len(lines),
                "limit": effective_limit,
            }
            if filters_applied:
                data["filters_applied"] = filters_applied

            return data

        except ToolError:
            raise
        except HomeAssistantAPIError as e:
            status = getattr(e, "status_code", None)
            if status == 400:
                # Supervisor-side rejection — not caller validation. The default
                # `exception_to_structured_error` path would map 400 →
                # VALIDATION_INVALID_PARAMETER, which reads as "caller passed
                # bad input"; a downstream proxy rejection is better modelled
                # as SERVICE_CALL_FAILED.
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        str(e),
                        context={"source": "supervisor", "slug": slug},
                        suggestions=[
                            f"Supervisor rejected the request for '{slug}' — "
                            "verify slug format or that the add-on is installed "
                            "and running",
                            "Use ha_get_addon() to list installed add-on slugs",
                            "Ensure Supervisor is available (HA OS or Supervised install)",
                        ],
                    )
                )
            if status == 404:
                first_suggestion = f"Add-on '{slug}' not found or not installed"
            else:
                first_suggestion = f"Verify add-on slug '{slug}' is correct"
            exception_to_structured_error(
                e,
                context={"source": "supervisor", "slug": slug},
                suggestions=[
                    first_suggestion,
                    "Use ha_get_addon() to list installed add-on slugs",
                    "Ensure Supervisor is available (HA OS or Supervised install)",
                ],
            )
        except (
            HomeAssistantConnectionError,
            TimeoutError,
            OSError,
        ) as e:
            exception_to_structured_error(
                e,
                context={"source": "supervisor", "slug": slug},
                suggestions=[
                    "Check Home Assistant connection",
                    f"Verify add-on slug '{slug}' is correct",
                    "Use ha_get_addon() to list installed add-on slugs",
                    "Ensure Supervisor is available (HA OS or Supervised install)",
                ],
            )

    @mcp.tool(
        tags={"Utilities"},
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "title": "Evaluate Template"
        }
    )
    @log_tool_usage
    async def ha_eval_template(
        template: str, timeout: int = 3, report_errors: bool | str = True
    ) -> dict[str, Any]:
        """
        Evaluate Jinja2 templates using Home Assistant's template engine.

        This tool allows testing and debugging of Jinja2 template expressions that are commonly used in
        Home Assistant automations, scripts, and configurations. It provides real-time evaluation with
        access to all Home Assistant states, functions, and template variables.

        **Parameters:**
        - template: The Jinja2 template string to evaluate
        - timeout: Maximum evaluation time in seconds (default: 3)
        - report_errors: Whether to return detailed error information (default: True)

        **Common Template Functions:**

        **State Access:**
        ```jinja2
        {{ states('sensor.temperature') }}              # Get entity state value
        {{ states.sensor.temperature.state }}           # Alternative syntax
        {{ state_attr('light.bedroom', 'brightness') }} # Get entity attribute
        {{ is_state('light.living_room', 'on') }}       # Check if entity has specific state
        ```

        **Numeric Operations:**
        ```jinja2
        {{ states('sensor.temperature') | float(0) }}   # Convert to float with default
        {{ states('sensor.humidity') | int }}           # Convert to integer
        {{ (states('sensor.temp') | float + 5) | round(1) }} # Math operations
        ```

        **Time and Date:**
        ```jinja2
        {{ now() }}                                     # Current datetime
        {{ now().strftime('%H:%M:%S') }}               # Format current time
        {{ as_timestamp(now()) }}                      # Convert to Unix timestamp
        {{ now().hour }}                               # Current hour (0-23)
        {{ now().weekday() }}                          # Day of week (0=Monday)
        ```

        **Conditional Logic:**
        ```jinja2
        {{ 'Day' if now().hour < 18 else 'Night' }}    # Ternary operator
        {% if is_state('sun.sun', 'above_horizon') %}
          It's daytime
        {% else %}
          It's nighttime
        {% endif %}
        ```

        **Lists and Loops:**
        ```jinja2
        {% for entity in states.light %}
          {{ entity.entity_id }}: {{ entity.state }}
        {% endfor %}

        {{ states.light | selectattr('state', 'eq', 'on') | list | count }} # Count on lights
        ```

        **String Operations:**
        ```jinja2
        {{ states('sensor.weather') | title }}         # Title case
        {{ 'Hello ' + states('input_text.name') }}     # String concatenation
        {{ states('sensor.data') | regex_replace('pattern', 'replacement') }}
        ```

        **Device and Area Functions:**
        ```jinja2
        {{ device_entities('device_id_here') }}        # Get entities for device
        {{ area_entities('living_room') }}             # Get entities in area
        {{ device_id('light.bedroom') }}               # Get device ID for entity
        ```

        **Common Use Cases:**

        **Automation Conditions:**
        ```jinja2
        # Check if it's a workday and after 7 AM
        {{ is_state('binary_sensor.workday', 'on') and now().hour >= 7 }}

        # Temperature-based condition
        {{ states('sensor.outdoor_temp') | float < 0 }}
        ```

        **Dynamic Service Data:**
        ```jinja2
        # Dynamic brightness based on time
        {{ 255 if now().hour < 22 else 50 }}

        # Message with current values
        "Temperature is {{ states('sensor.temp') }}°C, humidity {{ states('sensor.humidity') }}%"
        ```

        **Examples:**

        **Test basic state access:**
        ```python
        ha_eval_template("{{ states('light.living_room') }}")
        ```

        **Test conditional logic:**
        ```python
        ha_eval_template("{{ 'Day' if now().hour < 18 else 'Night' }}")
        ```

        **Test mathematical operations:**
        ```python
        ha_eval_template("{{ (states('sensor.temperature') | float + 5) | round(1) }}")
        ```

        **Test complex automation condition:**
        ```python
        ha_eval_template("{{ is_state('binary_sensor.workday', 'on') and now().hour >= 7 and states('sensor.temperature') | float > 20 }}")
        ```

        **Test entity counting:**
        ```python
        ha_eval_template("{{ states.light | selectattr('state', 'eq', 'on') | list | count }}")
        ```

        **IMPORTANT NOTES:**
        - Templates have access to all current Home Assistant states and attributes
        - Use this tool to test templates before using them in automations or scripts
        - Template evaluation respects Home Assistant's security model and timeouts
        - Complex templates may affect Home Assistant performance - keep them efficient
        - Use default values (e.g., `| float(0)`) to handle missing or invalid states

        **For template documentation:** https://www.home-assistant.io/docs/configuration/templating/
        """
        # Coerce boolean parameter that may come as string from XML-style calls
        report_errors_bool = coerce_bool_param(
            report_errors, "report_errors", default=True
        )
        assert report_errors_bool is not None  # default=True guarantees non-None

        try:
            # Generate unique ID for the template evaluation request
            import time

            request_id = int(time.time() * 1000) % 1000000  # Simple unique ID

            # Construct WebSocket message following the protocol
            message: dict[str, Any] = {
                "type": "render_template",
                "template": template,
                "timeout": timeout,
                "report_errors": report_errors_bool,
                "id": request_id,
            }

            # Send WebSocket message and get response
            result = await client.send_websocket_message(message)

            if result.get("success"):
                # Check if we have an event-type response with the actual result
                if "event" in result and "result" in result["event"]:
                    template_result = result["event"]["result"]
                    listeners = result["event"].get("listeners", {})

                    return {
                        "success": True,
                        "template": template,
                        "result": template_result,
                        "listeners": listeners,
                        "request_id": request_id,
                        "evaluation_time": timeout,
                    }
                else:
                    # Handle direct result response
                    return {
                        "success": True,
                        "template": template,
                        "result": result.get("result"),
                        "request_id": request_id,
                        "evaluation_time": timeout,
                    }
            else:
                error_info = result.get("error", "Unknown error occurred")
                raise_tool_error(create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    str(error_info) if not isinstance(error_info, str) else error_info,
                    context={"template": template, "request_id": request_id},
                    suggestions=[
                        "Check template syntax - ensure proper Jinja2 formatting",
                        "Verify entity_ids exist using ha_get_state()",
                        "Use default values: {{ states('sensor.temp') | float(0) }}",
                        "Check for typos in function names and entity references",
                        "Test simpler templates first to isolate issues",
                    ],
                ))

        except ToolError:
            raise
        except Exception as e:
            error_str = str(e)
            suggestions = [
                "Check Home Assistant WebSocket connection",
                "Verify template syntax is valid Jinja2",
                "Try a simpler template to test basic functionality",
                "Check if referenced entities exist",
                "Ensure template doesn't exceed timeout limit",
            ]

            # Add specific suggestions for 403 errors
            if "403" in error_str and "Forbidden" in error_str:
                suggestions = [
                    "The request was blocked (403 Forbidden) - this may be caused by:",
                    "  • Reverse proxy security rules (Apache, Nginx, Traefik)",
                    "  • Rate limiting from multiple simultaneous requests",
                    "  • Complex template triggering security filters",
                    "Try simplifying the template (remove newlines, reduce complexity)",
                    "Break complex templates into multiple simpler calls",
                    "Use ha_bug_report tool to check Home Assistant logs for details",
                ] + suggestions

            exception_to_structured_error(
                e,
                context={"template": template},
                suggestions=suggestions,
            )
