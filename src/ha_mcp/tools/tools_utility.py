"""
Utility tools for Home Assistant MCP server.

Template evaluation. The log tools that used to share this module are in
``tools_logs`` and its helpers, split out under `.gemini/styleguide.md` §
Tool Consolidation and Module Size.
"""

import logging
import time
from typing import Any

from fastmcp.exceptions import ToolError

from ..errors import ErrorCode, create_error_response
from .helpers import exception_to_structured_error, log_tool_usage, raise_tool_error

logger = logging.getLogger(__name__)


class UtilityTools:
    def __init__(self, client: Any) -> None:
        self._client = client

    async def eval_template(
        self, template: str, timeout: int, report_errors: bool
    ) -> dict[str, Any]:

        try:
            request_id = int(time.time() * 1000) % 1000000  # Simple unique ID

            message: dict[str, Any] = {
                "type": "render_template",
                "template": template,
                "timeout": timeout,
                "report_errors": report_errors,
                "id": request_id,
            }

            result = await self._client.send_websocket_message(message)

            if result.get("success"):
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
                    return {
                        "success": True,
                        "template": template,
                        "result": result.get("result"),
                        "request_id": request_id,
                        "evaluation_time": timeout,
                    }
            else:
                error_info = result.get("error", "Unknown error occurred")
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        str(error_info)
                        if not isinstance(error_info, str)
                        else error_info,
                        context={"template": template, "request_id": request_id},
                        suggestions=[
                            "Check template syntax - ensure proper Jinja2 formatting",
                            "Verify entity_ids exist using ha_get_state()",
                            "Use default values: {{ states('sensor.temp') | float(0) }}",
                            "Check for typos in function names and entity references",
                            "Test simpler templates first to isolate issues",
                        ],
                    )
                )

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
                    "Use ha_report_issue to check Home Assistant logs for details",
                ] + suggestions

            exception_to_structured_error(
                e,
                context={"template": template},
                suggestions=suggestions,
            )
            raise  # unreachable: exception_to_structured_error always raises
        return None  # py/mixed-returns: explicit terminal; error handlers above always raise (NoReturn), unreachable


def register_utility_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant utility tools."""
    tools = UtilityTools(client)

    @mcp.tool(
        tags={"Utilities"},
        annotations={
            "openWorldHint": False,
            "idempotentHint": True,
            "readOnlyHint": True,
            "title": "Evaluate Template",
        },
    )
    @log_tool_usage
    async def ha_eval_template(
        template: str, timeout: int = 3, report_errors: bool = True
    ) -> dict[str, Any]:
        """
        Evaluate Jinja2 templates using Home Assistant's template engine.

        This tool allows testing and debugging of Jinja2 template expressions that are commonly used in
        Home Assistant automations, scripts, and configurations. It provides real-time evaluation with
        access to all Home Assistant states, functions, and template variables.

        **When NOT to use this for automation/script logic:**
        Templates have legitimate uses (notification bodies, dynamic `data.*` values,
        debugging existing templates), but `condition:` / `trigger:` positions and
        action service names are better expressed as native HA constructs:
        native constructs are schema-validated at config load and surface
        structural errors loudly, whereas equivalent template logic only errors
        at runtime — and a template that renders a non-truthy value is silently
        treated as false.
        Prefer:
        - `condition: numeric_state` over `{{ states('x') | float > N }}`
        - `condition: state` over `{{ is_state(...) }}`
        - `condition: time` / `condition: sun` over `now().hour` / `is_state('sun.sun', ...)`
        - Native `for:` field on state/numeric_state triggers and state conditions over
          `{{ now() - X.last_changed > timedelta(...) }}` duration math
        - `choose` action over templated `service:` / `action:` strings
        See `ha_get_skill_guide` (best-practices skill) for the full anti-pattern list.

        **When to use (reach for this tool, don't compute it yourself):**
        Any one-shot question whose answer is DERIVED from current HA state — an
        average/sum/min/max across sensors, a count of entities matching a
        condition, a boolean comparison, or a rendered message with live values.
        One render call beats fetching N states and doing the math yourself, and
        it is the canonical way to *test* a template before embedding it. This is
        for one-shot answers and template testing only — NOT for putting templates
        into automation logic; for `condition:` / `trigger:` positions native
        constructs win.
        - "average temperature across the bedroom sensors"
          -> `{{ ([states('sensor.a'), states('sensor.b')] | map('float', 0) | sum) / 2 }}`
        - "how many lights are on"
          -> `{{ states.light | selectattr('state', 'eq', 'on') | list | count }}`
        NOT for a plain single-entity value ("what's the state of X") — that is
        `ha_get_state` / `ha_search`; rendering `{{ states('X') }}` there is over-use.

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
        {{ states('sensor.humidity') | int(0) }}        # Convert to integer with default
        {{ (states('sensor.temp') | float(0) + 5) | round(1) }} # Math operations
        ```

        **Time and Date:**
        ```jinja2
        {{ now() }}                                     # Current datetime
        {{ now().strftime('%H:%M:%S') }}               # Format current time
        {{ as_timestamp(now()) }}                      # Convert to Unix timestamp
        {{ now().hour }}                               # Current hour (0-23)
        {{ now().weekday() }}                          # Day of week (0=Monday)
        ```

        **Conditional Logic (for display strings — not for `condition:` positions):**
        ```jinja2
        {{ 'Day' if now().hour < 18 else 'Night' }}    # Ternary operator
        {% if is_state('alarm_control_panel.home', 'armed_away') %}
          Alarm is armed
        {% else %}
          Alarm is disarmed
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

        **Common Use Cases (legitimate template positions):**

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

        **Test a string expression (e.g. for a notification body):**
        ```python
        ha_eval_template("{{ 'Day' if now().hour < 18 else 'Night' }}")
        ```

        **Test mathematical operations:**
        ```python
        ha_eval_template("{{ (states('sensor.temperature') | float(0) + 5) | round(1) }}")
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
        return await tools.eval_template(template, timeout, report_errors)
