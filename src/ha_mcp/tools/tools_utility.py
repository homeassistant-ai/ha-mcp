"""
Utility tools for Home Assistant MCP server.

This module provides general-purpose utility tools including logbook access,
template evaluation, and domain documentation retrieval.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from .helpers import log_tool_usage
from .util_helpers import add_timezone_metadata, coerce_bool_param, coerce_int_param

logger = logging.getLogger(__name__)


def register_utility_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant utility tools."""

    # Default and maximum limits for logbook entries
    DEFAULT_LOGBOOK_LIMIT = 50
    MAX_LOGBOOK_LIMIT = 500

    @mcp.tool(annotations={"idempotentHint": True, "readOnlyHint": True, "tags": ["history"], "title": "Get Logbook Entries"})
    @log_tool_usage
    async def ha_get_logbook(
        hours_back: int | str = 1,
        entity_id: str | None = None,
        end_time: str | None = None,
        limit: int | str | None = None,
        offset: int | str = 0,
    ) -> dict[str, Any]:
        """
        Get Home Assistant logbook entries for the specified time period.

        Returns paginated logbook entries to prevent excessively large responses.

        **Parameters:**
        - hours_back: Number of hours to look back (default: 1)
        - entity_id: Optional entity ID to filter entries
        - end_time: Optional end time in ISO format (defaults to now)
        - limit: Maximum number of entries to return (default: 50, max: 500)
        - offset: Number of entries to skip for pagination (default: 0)

        **Pagination:**
        When the logbook has more entries than the limit, use offset to get
        additional pages. The response includes `has_more` to indicate if
        more entries are available.

        **IMPORTANT - Pagination Stability:**
        Pagination is performed client-side on the full result set returned
        by Home Assistant. If new logbook entries are created between page
        requests, results may shift and items could be missed or duplicated
        across pages. For best results, use consistent time ranges (start/end)
        and retrieve pages in quick succession.

        **Example:**
        - First page: ha_get_logbook(hours_back=24, limit=50, offset=0)
        - Second page: ha_get_logbook(hours_back=24, limit=50, offset=50)
        """

        # Coerce parameters with string handling for AI tools
        try:
            hours_back_int = coerce_int_param(
                hours_back,
                param_name="hours_back",
                default=1,
                min_value=1,
            )
            if hours_back_int is None:
                hours_back_int = 1
        except ValueError as e:
            return {
                "success": False,
                "error": str(e),
                "suggestions": ["Provide hours_back as an integer (e.g., 24)"],
            }

        try:
            effective_limit = coerce_int_param(
                limit,
                param_name="limit",
                default=DEFAULT_LOGBOOK_LIMIT,
                min_value=1,
                max_value=MAX_LOGBOOK_LIMIT,
            )
            if effective_limit is None:
                effective_limit = DEFAULT_LOGBOOK_LIMIT
        except ValueError as e:
            return {
                "success": False,
                "error": str(e),
                "suggestions": ["Provide limit as an integer (e.g., 50)"],
            }

        try:
            offset_int = coerce_int_param(
                offset,
                param_name="offset",
                default=0,
                min_value=0,
            )
            if offset_int is None:
                offset_int = 0
        except ValueError as e:
            return {
                "success": False,
                "error": str(e),
                "suggestions": ["Provide offset as an integer (e.g., 0)"],
            }

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

            if not response:
                no_entries_data = {
                    "success": False,
                    "error": "No logbook entries found",
                    "period": f"{hours_back_int} hours back from {end_dt.isoformat()}",
                    "entity_filter": entity_id,
                    "total_entries": 0,
                    "returned_entries": 0,
                    "limit": effective_limit,
                    "offset": offset_int,
                    "has_more": False,
                }
                return await add_timezone_metadata(client, no_entries_data)

            # Get total count before pagination
            total_entries = len(response) if isinstance(response, list) else 1

            # Apply pagination
            if isinstance(response, list):
                paginated_entries = response[offset_int : offset_int + effective_limit]
                has_more = (offset_int + effective_limit) < total_entries
            else:
                paginated_entries = response
                has_more = False

            logbook_data = {
                "success": True,
                "entries": paginated_entries,
                "period": f"{hours_back_int} hours back from {end_dt.isoformat()}",
                "start_time": start_timestamp,
                "end_time": end_dt.isoformat(),
                "entity_filter": entity_id,
                "total_entries": total_entries,
                "returned_entries": len(paginated_entries) if isinstance(paginated_entries, list) else 1,
                "limit": effective_limit,
                "offset": offset_int,
                "has_more": has_more,
            }

            # Add helpful message when results are truncated
            if has_more:
                next_offset = offset_int + effective_limit
                # Build complete parameter string for reproducible pagination
                param_parts = [
                    f"hours_back={hours_back_int}",
                    f"limit={effective_limit}",
                    f"offset={next_offset}"
                ]
                if entity_id:
                    param_parts.append(f"entity_id={entity_id}")
                if end_time:
                    param_parts.append(f"end_time={end_time}")

                param_str = ", ".join(param_parts)
                logbook_data["pagination_hint"] = (
                    f"Showing entries {offset_int + 1}-{offset_int + len(paginated_entries)} of {total_entries}. "
                    f"To get the next page, use: ha_get_logbook({param_str})"
                )

            return await add_timezone_metadata(client, logbook_data)

        except Exception as e:
            error_data = {
                "success": False,
                "error": f"Failed to retrieve logbook: {str(e)}",
                "period": f"{hours_back_int} hours back from {end_dt.isoformat()}",
            }
            return await add_timezone_metadata(client, error_data)

    @mcp.tool(annotations={"idempotentHint": True, "readOnlyHint": True, "tags": ["docs"], "title": "Evaluate Template"})
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
        report_errors_bool = coerce_bool_param(report_errors, "report_errors", default=True) or True

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
                return {
                    "success": False,
                    "template": template,
                    "error": error_info,
                    "request_id": request_id,
                    "suggestions": [
                        "Check template syntax - ensure proper Jinja2 formatting",
                        "Verify entity_ids exist using ha_get_state()",
                        "Use default values: {{ states('sensor.temp') | float(0) }}",
                        "Check for typos in function names and entity references",
                        "Test simpler templates first to isolate issues",
                    ],
                }

        except Exception as e:
            return {
                "success": False,
                "template": template,
                "error": f"Template evaluation failed: {str(e)}",
                "suggestions": [
                    "Check Home Assistant WebSocket connection",
                    "Verify template syntax is valid Jinja2",
                    "Try a simpler template to test basic functionality",
                    "Check if referenced entities exist",
                    "Ensure template doesn't exceed timeout limit",
                ],
            }

    @mcp.tool(annotations={"readOnlyHint": True})
    async def ha_get_domain_docs(domain: str) -> dict[str, Any]:
        """Get comprehensive documentation for Home Assistant entity domains."""
        domain = domain.lower().strip()

        # GitHub URL for Home Assistant integration documentation
        github_url = f"https://raw.githubusercontent.com/home-assistant/home-assistant.io/refs/heads/current/source/_integrations/{domain}.markdown"

        try:
            # Fetch documentation from GitHub
            async with httpx.AsyncClient(timeout=30.0) as client_http:
                response = await client_http.get(github_url)

                if response.status_code == 200:
                    # Successfully fetched documentation
                    doc_content = response.text

                    # Extract title from the first line if available
                    lines = doc_content.split("\n")
                    title = lines[0] if lines else f"{domain.title()} Integration"

                    return {
                        "domain": domain,
                        "source": "Home Assistant Official Documentation",
                        "url": github_url,
                        "documentation": doc_content,
                        "title": title.strip("# "),
                        "fetched_at": asyncio.get_event_loop().time(),
                        "status": "success",
                    }

                elif response.status_code == 404:
                    # Domain documentation not found
                    return {
                        "error": f"No official documentation found for domain '{domain}'",
                        "domain": domain,
                        "status": "not_found",
                        "suggestion": "Check if the domain name is correct. Common domains include: light, climate, switch, lock, sensor, automation, media_player, cover, fan, binary_sensor, camera, alarm_control_panel, etc.",
                        "github_url": github_url,
                    }

                else:
                    # Other HTTP errors
                    return {
                        "error": f"Failed to fetch documentation for '{domain}' (HTTP {response.status_code})",
                        "domain": domain,
                        "status": "fetch_error",
                        "github_url": github_url,
                        "suggestion": "Try again later or check the domain name",
                    }

        except httpx.TimeoutException:
            return {
                "error": f"Timeout while fetching documentation for '{domain}'",
                "domain": domain,
                "status": "timeout",
                "suggestion": "Try again later - GitHub may be temporarily unavailable",
            }

        except Exception as e:
            return {
                "error": f"Unexpected error fetching documentation for '{domain}': {str(e)}",
                "domain": domain,
                "status": "error",
                "suggestion": "Check your internet connection and try again",
            }
