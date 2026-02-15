"""
Utility tools for Home Assistant MCP server.

This module provides general-purpose utility tools including logbook access,
template evaluation, and domain documentation retrieval.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import httpx
from pydantic import Field

from ..errors import ErrorCode, create_error_response
from .helpers import log_tool_usage
from .util_helpers import add_timezone_metadata, coerce_bool_param, coerce_int_param, validate_guide_response

logger = logging.getLogger(__name__)


def register_utility_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant utility tools."""

    # Default and maximum limits for logbook entries
    DEFAULT_LOGBOOK_LIMIT = 50
    MAX_LOGBOOK_LIMIT = 500

    @mcp.tool(
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "tags": ["history"],
            "title": "Get Logbook Entries",
        }
    )
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
                "returned_entries": len(paginated_entries)
                if isinstance(paginated_entries, list)
                else 1,
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
                    f"offset={next_offset}",
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
            error_str = str(e)
            suggestions = []

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

            error_data = {
                "success": False,
                "error": f"Failed to retrieve logbook: {error_str}",
                "period": f"{hours_back_int} hours back from {end_dt.isoformat()}",
                "suggestions": suggestions if suggestions else None,
            }
            return await add_timezone_metadata(client, error_data)

    @mcp.tool(
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "tags": ["docs"],
            "title": "Evaluate Template",
        }
    )
    @log_tool_usage
    async def ha_eval_template(
        template: str,
        guide_response: Annotated[
            str | dict[str, Any],
            Field(
                description="REQUIRED: Output from ha_get_tool_guide('template')"
            ),
        ],
        timeout: int = 3,
        report_errors: bool | str = True
    ) -> dict[str, Any]:
        """Evaluate a Jinja2 template using Home Assistant's template engine.

        REQUIRED: You MUST call ha_get_tool_guide("template") before using this tool.
        The guide contains the full function reference (state access, numeric, time/date,
        conditional, string, device/area, loops) and examples essential for correct usage.
        Common patterns: states('entity_id'), state_attr('entity_id', 'attr'), is_state('entity_id', 'value').
        Also: https://www.home-assistant.io/docs/configuration/templating/ for full reference."""
        # Validate guide_response - enforces ha_get_tool_guide() was called first
        try:
            validate_guide_response(guide_response, "template")
        except ValueError as e:
            return {"success": False, "error": str(e)}

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

            return {
                "success": False,
                "template": template,
                "error": f"Template evaluation failed: {error_str}",
                "suggestions": suggestions,
            }

    @mcp.tool(annotations={"readOnlyHint": True, "title": "Get Domain Docs"})
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

    # ---- Tool usage guides (on-demand reference) ----
    # These contain critical guidance, examples, and warnings that were previously
    # embedded in tool descriptions. Loaded on-demand to reduce idle context usage.

    _TOOL_GUIDES: dict[str, dict[str, Any]] = {
        "automation": {
            "topic": "Creating and updating automations",
            "automation_types": [
                "1. Regular Automations - Define triggers and actions directly",
                "2. Blueprint Automations - Use pre-built templates with customizable inputs",
            ],
            "required_fields": {
                "regular": ["alias", "trigger", "action"],
                "blueprint": ["alias", "use_blueprint (with path + input)"],
            },
            "optional_fields": [
                "description (RECOMMENDED: helps safely modify implementation later)",
                "condition - Additional conditions that must be met",
                "mode - 'single' (default), 'restart', 'queued', 'parallel'",
                "max - Maximum concurrent executions (for queued/parallel modes)",
                "initial_state - Whether automation starts enabled (true/false)",
                "variables - Variables for use in automation",
            ],
            "critical_guidance": [
                "PREFER NATIVE SOLUTIONS OVER TEMPLATES:",
                "- Use `condition: state` with `state: [list]` instead of template for multiple states",
                "- Use `condition: state` with `attribute:` instead of template for attribute checks",
                "- Use `condition: numeric_state` instead of template for number comparisons",
                "- Use `wait_for_trigger` instead of `wait_template` when waiting for state changes",
                "- Use `choose` action instead of template-based service names",
            ],
            "trigger_types": "time, time_pattern, sun, state, numeric_state, event, device, zone, template, and more",
            "condition_types": "state, numeric_state, time, sun, template, device, zone, and more",
            "action_types": "service calls, delays, wait_for_trigger, wait_template, if/then/else, choose, repeat, parallel",
            "examples": {
                "time_trigger": {
                    "alias": "Morning Lights",
                    "description": "Turn on bedroom lights at 7 AM to help wake up",
                    "trigger": [{"platform": "time", "at": "07:00:00"}],
                    "action": [{"service": "light.turn_on", "target": {"area_id": "bedroom"}}],
                },
                "motion_with_condition": {
                    "alias": "Motion Light",
                    "trigger": [{"platform": "state", "entity_id": "binary_sensor.motion", "to": "on"}],
                    "condition": [{"condition": "sun", "after": "sunset"}],
                    "action": [
                        {"service": "light.turn_on", "target": {"entity_id": "light.hallway"}},
                        {"delay": {"minutes": 5}},
                        {"service": "light.turn_off", "target": {"entity_id": "light.hallway"}},
                    ],
                    "mode": "restart",
                },
                "update_existing": {
                    "_note": "Pass identifier to update: ha_config_set_automation(identifier='automation.morning_routine', config={...})",
                    "alias": "Updated Morning Routine",
                    "trigger": [{"platform": "time", "at": "06:30:00"}],
                    "action": [
                        {"service": "light.turn_on", "target": {"area_id": "bedroom"}},
                        {"service": "climate.set_temperature", "target": {"entity_id": "climate.bedroom"}, "data": {"temperature": 22}},
                    ],
                },
                "blueprint": {
                    "alias": "Motion Light Kitchen",
                    "use_blueprint": {
                        "path": "homeassistant/motion_light.yaml",
                        "input": {
                            "motion_entity": "binary_sensor.kitchen_motion",
                            "light_target": {"entity_id": "light.kitchen"},
                            "no_motion_wait": 120,
                        },
                    },
                },
                "update_blueprint_inputs": {
                    "_note": "Pass identifier to update: ha_config_set_automation(identifier='automation.motion_light_kitchen', config={...})",
                    "alias": "Motion Light Kitchen",
                    "use_blueprint": {
                        "path": "homeassistant/motion_light.yaml",
                        "input": {
                            "motion_entity": "binary_sensor.kitchen_motion",
                            "light_target": {"entity_id": "light.kitchen"},
                            "no_motion_wait": 300,
                        },
                    },
                },
            },
            "documentation": [
                "ha_get_domain_docs('automation') for comprehensive HA documentation",
                "https://www.home-assistant.io/docs/automation/ for full reference",
            ],
            "troubleshooting": [
                "Use ha_get_state() to verify entity_ids exist",
                "Use ha_search_entities() to find correct entity_ids",
                "Use ha_search_entities(domain_filter='automation') to find existing automations",
                "Use ha_eval_template() to test Jinja2 templates before using in automations",
                "Use ha_get_domain_docs('automation') for full HA documentation",
            ],
        },
        "script": {
            "topic": "Creating and updating scripts",
            "important": "The 'config' parameter must be passed as a proper dictionary/object.",
            "required_fields": {
                "regular": ["sequence (list of actions)"],
                "blueprint": ["use_blueprint (with path + input)"],
            },
            "optional_fields": [
                "alias - Display name (defaults to script_id)",
                "description - Script description",
                "icon - Icon to display",
                "mode - Execution mode: 'single', 'restart', 'queued', 'parallel'",
                "max - Maximum concurrent executions (for queued/parallel modes)",
                "fields - Input parameters for the script",
            ],
            "critical_guidance": [
                "PREFER NATIVE ACTIONS OVER TEMPLATES:",
                "- Use `choose` action instead of template-based service names",
                "- Use `if/then/else` action instead of template conditions",
                "- Use `repeat` action with `for_each` instead of template loops",
                "- Use `wait_for_trigger` instead of `wait_template` when waiting for state changes",
                "- Use native action variables instead of complex template calculations",
            ],
            "note": "Scripts use Home Assistant's action syntax. Check documentation for advanced features like conditions, variables, parallel execution, and service call options.",
            "examples": {
                "basic_sequence": {
                    "_note": "ha_config_set_script('blink_light', config={...})",
                    "sequence": [
                        {"service": "light.turn_on", "target": {"entity_id": "light.living_room"}},
                        {"delay": {"seconds": 2}},
                        {"service": "light.turn_off", "target": {"entity_id": "light.living_room"}},
                    ],
                    "alias": "Light Blink",
                    "mode": "single",
                },
                "with_parameters": {
                    "alias": "Backup with Reference",
                    "description": "Create backup with optional reference parameter",
                    "fields": {
                        "reference": {
                            "name": "Reference",
                            "description": "Optional reference for backup identification",
                            "selector": {"text": None},
                        },
                    },
                    "sequence": [
                        {
                            "action": "hassio.backup_partial",
                            "data": {
                                "compressed": False,
                                "homeassistant": True,
                                "homeassistant_exclude_database": True,
                                "name": "Backup_{{ reference | default('auto') }}_{{ now().strftime('%Y%m%d_%H%M%S') }}",
                            },
                        },
                    ],
                },
                "update_script": {
                    "_note": "ha_config_set_script('morning_routine', config={...})",
                    "sequence": [
                        {"service": "light.turn_on", "target": {"area_id": "bedroom"}},
                        {"service": "climate.set_temperature", "target": {"entity_id": "climate.bedroom"}, "data": {"temperature": 22}},
                    ],
                    "alias": "Updated Morning Routine",
                },
                "blueprint": {
                    "_note": "ha_config_set_script('notification_script', config={...})",
                    "alias": "My Notification Script",
                    "use_blueprint": {
                        "path": "notification_script.yaml",
                        "input": {"message": "Hello World", "title": "Test Notification"},
                    },
                },
                "update_blueprint_inputs": {
                    "_note": "ha_config_set_script('notification_script', config={...})",
                    "alias": "My Notification Script",
                    "use_blueprint": {
                        "path": "notification_script.yaml",
                        "input": {"message": "Updated message", "title": "Updated Title"},
                    },
                },
            },
            "documentation": "ha_get_domain_docs('script') for detailed script configuration help",
        },
        "dashboard": {
            "topic": "Creating and updating dashboards",
            "critical_guidance": [
                "url_path must contain a hyphen (-) to be valid",
                "Use 'default' or 'lovelace' to target the built-in default dashboard",
                "WHEN TO USE WHICH MODE:",
                "- python_transform: RECOMMENDED for edits. Surgical/pattern-based updates, works on all platforms.",
                "- jq_transform: Legacy mode. Requires jq binary (not available on Windows ARM64).",
                "- config: New dashboards only, or full restructure. Replaces everything.",
                "IMPORTANT: After delete/add operations, indices shift! Subsequent transform calls",
                "must use fresh config_hash from ha_dashboard_find_card() or ha_config_get_dashboard().",
                "Chain multiple ops in ONE expression when possible.",
                "TIP: Use ha_dashboard_find_card() to get the jq_path for any card.",
                "Note: If dashboard exists, only the config is updated. To change metadata (title, icon), use ha_config_update_dashboard_metadata().",
                "Strategy dashboards cannot be converted to custom dashboards via this tool. Use 'Take Control' in the HA interface.",
            ],
            "modern_best_practices": [
                "Use 'sections' view type (default) with grid-based layouts",
                "Use 'tile' cards as primary card type (replaces legacy entity/light/climate cards)",
                "Use 'grid' cards for multi-column layouts within sections",
                "Create multiple views with navigation paths (avoid single-view endless scrolling)",
                "Use 'area' cards with navigation for hierarchical organization",
            ],
            "entity_discovery": [
                "Do NOT guess entity IDs - use these tools to find exact entity IDs:",
                "1. ha_get_overview(include_entity_id=True) - Get all entities organized by domain/area",
                "2. ha_search_entities(query, domain_filter, area_filter) - Find specific entities",
                "3. ha_deep_search(query) - Comprehensive search across entities, areas, automations",
                "If unsure about entity IDs, ALWAYS use one of these tools first.",
            ],
            "discovery_workflow": [
                "1. ha_get_overview(include_entity_id=True) - Get all entities by domain/area",
                "2. ha_search_entities(query, domain_filter, area_filter) - Find specific entities",
                "3. ha_get_dashboard_guide() - Complete structure/cards/features guide",
                "4. ha_get_card_types() - List of all available card types",
                "5. ha_get_card_documentation(card_type) - Card-specific docs",
            ],
            "examples": {
                "jq_update_icon": '.views[0].sections[1].cards[0].icon = "mdi:thermometer"',
                "jq_add_card": '.views[0].cards += [{"type": "button", "entity": "light.bedroom"}]',
                "jq_delete_card": "del(.views[0].sections[0].cards[2])",
                "jq_select_update": '(.views[0].cards[] | select(.entity == "light.living_room")).icon = "mdi:lamp"',
                "jq_multi_op": 'del(.views[0].cards[2]) | .views[0].cards[0].icon = "mdi:new"',
                "jq_multiple_updates": '.views[0].cards[0].icon = "mdi:a" | .views[0].cards[1].icon = "mdi:b"',
                "python_update": "config['views'][0]['cards'][0]['icon'] = 'mdi:lamp'",
                "python_pattern": "for card in config['views'][0]['cards']:\\n  if 'light' in card.get('entity', ''):\\n    card['icon'] = 'mdi:lightbulb'",
                "python_multi_op": "config['views'][0]['cards'][0]['icon'] = 'mdi:a'; config['views'][0]['cards'][1]['icon'] = 'mdi:b'",
                "create_empty": "ha_config_set_dashboard(url_path='mobile-dashboard', title='Mobile View', icon='mdi:cellphone')",
                "create_sections_view": {
                    "_note": "ha_config_set_dashboard(url_path='home-dashboard', title='Home Overview', config={...})",
                    "views": [{
                        "title": "Home",
                        "type": "sections",
                        "sections": [{
                            "title": "Climate",
                            "cards": [{
                                "type": "tile",
                                "entity": "climate.living_room",
                                "features": [{"type": "target-temperature"}],
                            }],
                        }],
                    }],
                },
                "strategy_dashboard": {
                    "_note": "Auto-generated dashboard: ha_config_set_dashboard(url_path='my-home', title='My Home', config={...})",
                    "strategy": {
                        "type": "home",
                        "favorite_entities": ["light.bedroom"],
                    },
                },
            },
        },
        "template": {
            "topic": "Jinja2 template evaluation in Home Assistant",
            "parameters": {
                "template": "The Jinja2 template string to evaluate",
                "timeout": "Maximum evaluation time in seconds (default: 3)",
                "report_errors": "Whether to return detailed error information (default: True)",
            },
            "common_functions": {
                "state_access": [
                    "{{ states('sensor.temperature') }} - Get entity state value",
                    "{{ states.sensor.temperature.state }} - Alternative syntax",
                    "{{ state_attr('light.bedroom', 'brightness') }} - Get entity attribute",
                    "{{ is_state('light.living_room', 'on') }} - Check entity state",
                ],
                "numeric": [
                    "{{ states('sensor.temp') | float(0) }} - Convert to float with default",
                    "{{ states('sensor.humidity') | int }} - Convert to integer",
                    "{{ (states('sensor.temp') | float + 5) | round(1) }} - Math operations",
                ],
                "time_date": [
                    "{{ now() }} - Current datetime",
                    "{{ now().strftime('%H:%M:%S') }} - Format time",
                    "{{ as_timestamp(now()) }} - Convert to Unix timestamp",
                    "{{ now().hour }} - Current hour (0-23)",
                    "{{ now().weekday() }} - Day of week (0=Monday)",
                ],
                "conditional_logic": [
                    "{{ 'Day' if now().hour < 18 else 'Night' }} - Ternary operator",
                    "{% if is_state('sun.sun', 'above_horizon') %}Daytime{% else %}Nighttime{% endif %}",
                ],
                "string_operations": [
                    "{{ states('sensor.weather') | title }} - Title case",
                    "{{ 'Hello ' + states('input_text.name') }} - String concatenation",
                    "{{ states('sensor.data') | regex_replace('pattern', 'replacement') }}",
                ],
                "device_area": [
                    "{{ device_entities('device_id_here') }} - Get entities for device",
                    "{{ area_entities('living_room') }} - Get entities in area",
                    "{{ device_id('light.bedroom') }} - Get device ID for entity",
                ],
                "lists_loops": [
                    "{% for entity in states.light %} {{ entity.entity_id }} {% endfor %}",
                    "{{ states.light | selectattr('state', 'eq', 'on') | list | count }}",
                ],
            },
            "use_cases": {
                "automation_conditions": [
                    "{{ is_state('binary_sensor.workday', 'on') and now().hour >= 7 }}",
                    "{{ states('sensor.outdoor_temp') | float < 0 }}",
                ],
                "dynamic_service_data": [
                    "{{ 255 if now().hour < 22 else 50 }} - Dynamic brightness based on time",
                    "Temperature is {{ states('sensor.temp') }}\u00b0C, humidity {{ states('sensor.humidity') }}%",
                ],
            },
            "examples": {
                "basic_state": 'ha_eval_template("{{ states(\'light.living_room\') }}")',
                "conditional": 'ha_eval_template("{{ \'Day\' if now().hour < 18 else \'Night\' }}")',
                "math": 'ha_eval_template("{{ (states(\'sensor.temperature\') | float + 5) | round(1) }}")',
                "complex_condition": 'ha_eval_template("{{ is_state(\'binary_sensor.workday\', \'on\') and now().hour >= 7 and states(\'sensor.temperature\') | float > 20 }}")',
                "entity_count": 'ha_eval_template("{{ states.light | selectattr(\'state\', \'eq\', \'on\') | list | count }}")',
            },
            "important_notes": [
                "Templates have access to all current Home Assistant states and attributes",
                "Use this tool to test templates before using them in automations or scripts",
                "Template evaluation respects Home Assistant's security model and timeouts",
                "Use default values (e.g., | float(0)) to handle missing or invalid states",
                "Complex templates may affect Home Assistant performance - keep them efficient",
            ],
            "documentation_url": "https://www.home-assistant.io/docs/configuration/templating/",
        },
        "entity": {
            "topic": "Updating entity registry properties",
            "bulk_operations": [
                "When entity_id is a list, ONLY labels and expose_to parameters are supported.",
                "Other parameters (area_id, name, icon, enabled, hidden, aliases) require single entity.",
            ],
            "label_operations": {
                "set": "Replace all labels with the provided list. Use [] to clear all labels.",
                "add": "Add labels to existing ones without removing any.",
                "remove": "Remove specified labels from the entity.",
            },
            "finding_entities": [
                "Use ha_search_entities() or ha_get_device() to find entity IDs",
                "Use ha_config_get_label() to find available label IDs",
            ],
            "expose_to_assistants": {
                "conversation": "Home Assistant Assist",
                "cloud.alexa": "Amazon Alexa via Nabu Casa",
                "cloud.google_assistant": "Google Assistant via Nabu Casa",
            },
            "examples": {
                "single_entity": {
                    "assign_area": 'ha_set_entity("sensor.temp", area_id="living_room")',
                    "rename": 'ha_set_entity("sensor.temp", name="Living Room Temperature")',
                    "set_labels": 'ha_set_entity("light.lamp", labels=["outdoor", "smart"])',
                    "add_labels": 'ha_set_entity("light.lamp", labels=["new_label"], label_operation="add")',
                    "remove_labels": 'ha_set_entity("light.lamp", labels=["old_label"], label_operation="remove")',
                    "clear_labels": 'ha_set_entity("light.lamp", labels=[])',
                    "expose_alexa": 'ha_set_entity("light.lamp", expose_to={"cloud.alexa": True})',
                },
                "bulk_operations": {
                    "set_labels": 'ha_set_entity(["light.a", "light.b"], labels=["outdoor"])',
                    "add_labels": 'ha_set_entity(["light.a", "light.b"], labels=["new"], label_operation="add")',
                    "expose_alexa": 'ha_set_entity(["light.a", "light.b"], expose_to={"cloud.alexa": True})',
                },
            },
            "note": "To rename an entity_id (e.g., sensor.old -> sensor.new), use ha_rename_entity() instead.",
        },
        "history": {
            "topic": "Retrieving entity state history",
            "ha_get_history": {
                "description": "Full-resolution state change history from recorder (~10 day retention)",
                "data_characteristics": "Every state transition captured, ~10 day retention (configurable via recorder.purge_keep_days)",
                "relative_time_formats": "Use '24h', '7d', '2w' for relative start_time (hours/days/weeks)",
                "parameters": {
                    "entity_ids": "Entity ID(s) to query (required)",
                    "start_time": "Start of period - ISO datetime or relative ('24h', '7d', '2w'). Default: 24h ago",
                    "end_time": "End of period - ISO datetime. Default: now",
                    "minimal_response": "Omit attributes for smaller response. Default: true",
                    "significant_changes_only": "Filter to actual state changes. Default: true",
                    "limit": "Max entries per entity. Default: 100, Max: 1000",
                },
                "best_for": "Troubleshooting, pattern analysis, specific event queries, debugging automation triggers",
                "use_cases": [
                    "Why was my bedroom cold last night? - Query temperature sensor history",
                    "Did my garage door open while I was away? - Check cover state changes",
                    "What time does motion usually trigger? - Analyze binary sensor patterns",
                    "Debug automation triggers - See exact state change sequence",
                ],
                "examples": [
                    'ha_get_history(entity_ids="sensor.bedroom_temperature")',
                    'ha_get_history(entity_ids=["sensor.temperature", "sensor.humidity"], start_time="7d", limit=500)',
                    'ha_get_history(entity_ids="light.living_room", start_time="2025-01-25T00:00:00Z", end_time="2025-01-26T00:00:00Z", minimal_response=False)',
                ],
                "returns": "List of entities with their state history. Each entity includes: entity_id, period, states array, count",
                "note": "For long-term trends (>10 days), use ha_get_statistics() instead",
            },
            "ha_get_statistics": {
                "description": "Pre-aggregated long-term statistics (permanent retention)",
                "data_characteristics": "Hourly/daily/monthly statistics, permanent retention, never purged",
                "eligible_entities": "Only entities with state_class attribute (measurement, total, total_increasing)",
                "parameters": {
                    "entity_ids": "Entity ID(s) with state_class attribute (required)",
                    "start_time": "Start of period - ISO datetime or relative ('30d', '6m', '12m'). Default: 30d ago",
                    "end_time": "End of period - ISO datetime. Default: now",
                    "period": "Aggregation: '5minute', 'hour', 'day', 'week', 'month'. Default: 'day'",
                    "statistic_types": "Types to include: 'mean', 'min', 'max', 'sum', 'state', 'change'. Default: all",
                },
                "statistic_types": {
                    "mean": "Average value over the period",
                    "min": "Minimum value during the period",
                    "max": "Maximum value during the period",
                    "sum": "Running total (for total_increasing entities like energy)",
                    "state": "Last known state value",
                    "change": "Change from previous period",
                },
                "use_cases": [
                    "How much electricity did I use this month vs last month? - Monthly sum",
                    "What's my average living room temperature? - Daily/monthly mean",
                    "Show daily energy consumption for the past 2 weeks - Daily sum",
                    "Has my solar production declined year over year? - Monthly comparison",
                ],
                "examples": [
                    'ha_get_statistics(entity_ids="sensor.total_energy_kwh")',
                    'ha_get_statistics(entity_ids="sensor.living_room_temperature", start_time="6m", period="month", statistic_types=["mean", "min", "max"])',
                    'ha_get_statistics(entity_ids=["sensor.solar_production", "sensor.grid_consumption"], start_time="12m", period="month", statistic_types=["sum"])',
                ],
                "returns": "List of entities with their statistics. Each includes: entity_id, period type, statistics array, unit_of_measurement",
                "relative_time_formats": "Use '30d', '6m', '12m' for relative start_time",
                "periods": "5minute, hour, day, week, month",
                "note": "Use ha_search_entities() to find entities with state_class attribute",
            },
        },
        "search": {
            "topic": "Searching and discovering entities",
            "usage_tips": [
                "Try partial names: 'living' finds 'Living Room Light'",
                "Domain search: 'light' finds all light entities",
                "French/English: 'salon' or 'living' both work",
                "Typo tolerant: 'lihgt' finds 'light' entities",
                "List by domain: use domain_filter with empty query (e.g., domain_filter='calendar')",
            ],
            "best_practice": "Before performing searches, call ha_get_overview() first to understand smart home size, language used in entity naming, and available areas/rooms.",
            "workflow": [
                "1. Call ha_get_overview() first to understand available domains and areas",
                "2. Use ha_search_entities() for finding specific entities",
                "3. Use ha_deep_search() to search within automation/script configurations",
                "4. Use ha_get_state() for detailed state of a specific entity",
            ],
            "ha_search_entities": {
                "domain_listing_examples": [
                    "ha_search_entities(query='', domain_filter='calendar') - List all calendars",
                    "ha_search_entities(query='', domain_filter='todo') - List all todo lists",
                    "ha_search_entities(query='', domain_filter='scene') - List all scenes",
                    "ha_search_entities(query='', domain_filter='zone') - List all zones (as entities)",
                ],
            },
            "ha_get_overview_levels": {
                "minimal": "10 entities per domain sample (recommended for quick orientation / searches)",
                "standard": "ALL entities per domain (friendly_name only, default) - for comprehensive tasks",
                "full": "ALL entities with entity_id + friendly_name + state + system_info - for deep analysis",
            },
            "ha_get_overview_returns": "System information including base_url, version, location, timezone, and entity overview",
            "ha_deep_search": {
                "description": "Deep search across automation, script, and helper definitions",
                "search_scope": "Searches entity names and within configuration definitions (triggers, actions, sequences, conditions)",
                "args": {
                    "query": "Search query (can be partial, with typos)",
                    "search_types": 'Types to search (list of strings, default: ["automation", "script", "helper"])',
                    "limit": "Maximum total results to return (default: 20)",
                },
                "examples": [
                    'ha_deep_search("light.turn_on") - Find automations using a service',
                    'ha_deep_search("delay") - Find scripts with delays',
                    'ha_deep_search("option_a") - Find helpers with specific options',
                    'ha_deep_search("sensor.temperature") - Search all types for an entity',
                    'ha_deep_search("motion", search_types=["automation"]) - Search only automations',
                ],
                "return_fields": "entity_id, friendly_name, score, match_in_name, match_in_config, config",
            },
        },
    }

    @mcp.tool(
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "tags": ["information", "documentation"],
            "title": "Get Tool Usage Guide",
        }
    )
    @log_tool_usage
    async def ha_get_tool_guide(
        topic: Annotated[
            str,
            Field(
                description=(
                    "Guide topic: 'automation', 'script', 'dashboard', 'template', "
                    "'entity', 'history', 'search'. Get examples, critical warnings, "
                    "and usage patterns for ha-mcp tools."
                ),
            ),
        ],
    ) -> dict[str, Any]:
        """PREREQUISITE: Must be called before using ha-mcp tools that reference it.

        Returns the complete usage guide for a topic including required/optional fields,
        examples, critical warnings, best practices, and troubleshooting.
        Tools that require this guide will state 'REQUIRED: You MUST call ha_get_tool_guide()'
        in their description. Without this guide, you lack essential context for correct usage."""
        topic_lower = topic.lower().strip()
        if topic_lower in _TOOL_GUIDES:
            return {"success": True, **_TOOL_GUIDES[topic_lower]}
        available_topics = list(_TOOL_GUIDES.keys())
        return create_error_response(
            code=ErrorCode.RESOURCE_NOT_FOUND,
            message=f"Unknown guide topic: '{topic}'",
            suggestions=[
                "Please use one of the supported guide topics.",
                f"Available topics are: {available_topics}",
            ],
            context={"available_topics": available_topics},
        )
