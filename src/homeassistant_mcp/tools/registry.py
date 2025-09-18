"""
Tools registry for Smart MCP Server - manages registration of all MCP tools.
"""

import asyncio
import functools
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Union, cast

import httpx

from ..utils.usage_logger import log_tool_call

logger = logging.getLogger(__name__)


def parse_json_param(
    param: str | dict | list | None, param_name: str = "parameter"
) -> dict | list | None:
    """
    Parse flexibly JSON string or return existing dict/list.

    Args:
        param: JSON string, dict, list, or None
        param_name: Parameter name for error context

    Returns:
        Parsed dict/list or original value if already correct type

    Raises:
        ValueError: If JSON parsing fails
    """
    if param is None:
        return None

    if isinstance(param, (dict, list)):
        return param

    if isinstance(param, str):
        try:
            parsed = json.loads(param)
            if not isinstance(parsed, (dict, list)):
                raise ValueError(
                    f"{param_name} must be a JSON object or array, got {type(parsed).__name__}"
                )
            return parsed
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in {param_name}: {e}")

    raise ValueError(
        f"{param_name} must be string, dict, list, or None, got {type(param).__name__}"
    )


def parse_string_list_param(
    param: str | list[str] | None, param_name: str = "parameter"
) -> list[str] | None:
    """Parse JSON string array or return existing list of strings."""
    if param is None:
        return None

    if isinstance(param, list):
        if all(isinstance(item, str) for item in param):
            return param
        raise ValueError(f"{param_name} must be a list of strings")

    if isinstance(param, str):
        try:
            parsed = json.loads(param)
            if not isinstance(parsed, list):
                raise ValueError(f"{param_name} must be a JSON array")
            if not all(isinstance(item, str) for item in parsed):
                raise ValueError(f"{param_name} must be a JSON array of strings")
            return parsed
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in {param_name}: {e}")

    raise ValueError(f"{param_name} must be string, list, or None")


async def add_timezone_metadata(client: Any, data: dict[str, Any]) -> dict[str, Any]:
    """Add timezone metadata to tool responses containing timestamps."""
    try:
        config = await client.get_config()
        ha_timezone = config.get("time_zone", "UTC")

        return {
            "data": data,
            "metadata": {
                "home_assistant_timezone": ha_timezone,
                "timestamp_format": "ISO 8601 (UTC)",
                "note": f"All timestamps are in UTC. Home Assistant timezone is {ha_timezone}.",
            },
        }
    except Exception:
        # Fallback if config fetch fails
        return {
            "data": data,
            "metadata": {
                "home_assistant_timezone": "Unknown",
                "timestamp_format": "ISO 8601 (UTC)",
                "note": "All timestamps are in UTC. Could not fetch Home Assistant timezone.",
            },
        }


def log_tool_usage(func: Any) -> Any:
    """Decorator to automatically log MCP tool usage."""

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        start_time = time.time()
        tool_name = func.__name__
        success = True
        error_message = None
        response_size = None

        try:
            result = await func(*args, **kwargs)
            if isinstance(result, str):
                response_size = len(result.encode("utf-8"))
            elif hasattr(result, "__len__"):
                response_size = len(str(result).encode("utf-8"))
            return result
        except Exception as e:
            success = False
            error_message = str(e)
            raise
        finally:
            execution_time_ms = (time.time() - start_time) * 1000
            log_tool_call(
                tool_name=tool_name,
                parameters=kwargs,
                execution_time_ms=execution_time_ms,
                success=success,
                error_message=error_message,
                response_size_bytes=response_size,
            )

    return wrapper


class ToolsRegistry:
    """Manages registration of all MCP tools for the smart server."""

    def __init__(self, server: Any) -> None:
        self.server = server
        self.client = server.client
        self.mcp = server.mcp
        self.smart_tools = server.smart_tools
        self.device_tools = server.device_tools
        self.convenience_tools = server.convenience_tools

    def register_all_tools(self) -> None:
        """Register all tools with the MCP server."""
        self._register_smart_search_tools()
        self._register_device_control_tools()
        self._register_convenience_tools()

    def _register_smart_search_tools(self) -> None:
        """Register smart search and discovery tools."""

        @self.mcp.tool
        @log_tool_usage
        async def ha_search_entities(
            query: str,
            domain_filter: str | None = None,
            area_filter: str | None = None,
            limit: int = 10,
            group_by_domain: bool = False,
        ) -> dict[str, Any]:
            """Comprehensive entity search with fuzzy matching, domain/area filtering, and optional grouping."""
            try:
                # If area_filter is provided, use area-based search
                if area_filter:
                    area_result = await self.smart_tools.get_entities_by_area(
                        area_filter, group_by_domain=True
                    )

                    # If we also have a query, filter the area results
                    if query and query.strip():
                        # Get all entities from all areas in the result
                        all_area_entities = []
                        if "areas" in area_result:
                            for area_data in area_result["areas"].values():
                                if "entities" in area_data:
                                    if isinstance(
                                        area_data["entities"], dict
                                    ):  # grouped by domain
                                        for domain_entities in area_data[
                                            "entities"
                                        ].values():
                                            all_area_entities.extend(domain_entities)
                                    else:  # flat list
                                        all_area_entities.extend(area_data["entities"])

                        # Apply fuzzy search to area entities
                        from ..utils.fuzzy_search import create_fuzzy_searcher

                        fuzzy_searcher = create_fuzzy_searcher(threshold=80)

                        # Convert to format expected by fuzzy searcher
                        entities_for_search = []
                        for entity in all_area_entities:
                            entities_for_search.append(
                                {
                                    "entity_id": entity.get("entity_id", ""),
                                    "attributes": {
                                        "friendly_name": entity.get("friendly_name", "")
                                    },
                                    "state": entity.get("state", "unknown"),
                                }
                            )

                        matches = fuzzy_searcher.search_entities(
                            entities_for_search, query, limit
                        )

                        # Format matches similar to smart_entity_search
                        results = []
                        for match in matches:
                            results.append(
                                {
                                    "entity_id": match["entity_id"],
                                    "friendly_name": match["friendly_name"],
                                    "domain": match["domain"],
                                    "state": match["state"],
                                    "score": match["score"],
                                    "match_type": match["match_type"],
                                    "area_filter": area_filter,
                                }
                            )

                        # Group by domain if requested
                        if group_by_domain:
                            by_domain: dict[str, list[dict[str, Any]]] = {}
                            for result in results:
                                domain = result["domain"]
                                if domain not in by_domain:
                                    by_domain[domain] = []
                                by_domain[domain].append(result)

                            search_data = {
                                "success": True,
                                "query": query,
                                "area_filter": area_filter,
                                "total_matches": len(results),
                                "results": results,
                                "by_domain": by_domain,
                                "search_type": "area_filtered_query",
                            }
                            return await add_timezone_metadata(self.client, search_data)
                        else:
                            search_data = {
                                "success": True,
                                "query": query,
                                "area_filter": area_filter,
                                "total_matches": len(results),
                                "results": results,
                                "search_type": "area_filtered_query",
                            }
                            return await add_timezone_metadata(self.client, search_data)
                    else:
                        # Just area filter, return area results with enhanced format
                        if "areas" in area_result and area_result["areas"]:
                            first_area = next(iter(area_result["areas"].values()))
                            by_domain = first_area.get("entities", {})

                            # Flatten for results while keeping by_domain structure
                            all_results = []
                            for domain, entities in by_domain.items():
                                for entity in entities:
                                    entity["domain"] = domain
                                    all_results.append(entity)

                            area_search_data = {
                                "success": True,
                                "area_filter": area_filter,
                                "total_matches": len(all_results),
                                "results": all_results,
                                "by_domain": by_domain,
                                "search_type": "area_only",
                                "area_name": first_area.get("area_name", area_filter),
                            }
                            return await add_timezone_metadata(
                                self.client, area_search_data
                            )
                        else:
                            empty_area_data = {
                                "success": True,
                                "area_filter": area_filter,
                                "total_matches": 0,
                                "results": [],
                                "by_domain": {},
                                "search_type": "area_only",
                                "message": f"No entities found in area: {area_filter}",
                            }
                            return await add_timezone_metadata(
                                self.client, empty_area_data
                            )

                # Regular entity search (no area filter)
                result = await self.smart_tools.smart_entity_search(query, limit)

                # Convert 'matches' to 'results' for backward compatibility
                if "matches" in result:
                    result["results"] = result.pop("matches")

                # Apply domain filter if provided
                if domain_filter and "results" in result:
                    filtered_results = [
                        r for r in result["results"] if r.get("domain") == domain_filter
                    ]
                    result["results"] = filtered_results
                    result["total_matches"] = len(filtered_results)
                    result["domain_filter"] = domain_filter

                # Group by domain if requested
                if group_by_domain and "results" in result:
                    by_domain = {}
                    for entity in result["results"]:
                        domain = entity.get("domain", entity["entity_id"].split(".")[0])
                        if domain not in by_domain:
                            by_domain[domain] = []
                        by_domain[domain].append(entity)
                    result["by_domain"] = by_domain

                result["search_type"] = "fuzzy_search"
                return await add_timezone_metadata(self.client, result)

            except Exception as e:
                error_data = {
                    "error": str(e),
                    "query": query,
                    "domain_filter": domain_filter,
                    "area_filter": area_filter,
                    "suggestions": [
                        "Check Home Assistant connection",
                        "Try simpler search terms",
                        "Check area/domain filter spelling",
                    ],
                }
                return await add_timezone_metadata(self.client, error_data)

        @self.mcp.tool
        @log_tool_usage
        async def ha_get_overview() -> dict[str, Any]:
            """Get AI-friendly system overview with intelligent categorization."""
            result = await self.smart_tools.get_system_overview()
            return cast(dict[str, Any], result)

        @self.mcp.tool
        @log_tool_usage
        async def ha_get_state(entity_id: str) -> dict[str, Any]:
            """Get detailed state information for a Home Assistant entity with timezone metadata."""
            try:
                result = await self.client.get_entity_state(entity_id)
                return await add_timezone_metadata(self.client, result)
            except Exception as e:
                error_data = {
                    "entity_id": entity_id,
                    "error": str(e),
                    "suggestions": [
                        f"Verify entity {entity_id} exists",
                        "Check Home Assistant connection",
                        "Try ha_search_entities() to find correct entity",
                    ],
                }
                return await add_timezone_metadata(self.client, error_data)

        @self.mcp.tool
        @log_tool_usage
        async def ha_call_service(
            domain: str,
            service: str,
            entity_id: str | None = None,
            data: str | dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            """
            Execute Home Assistant services with comprehensive validation and examples.

            This is the universal tool for controlling all Home Assistant entities and executing automations.

            **Common Usage Examples:**

            **Light Control:**
            ```python
            # Turn on light
            ha_call_service("light", "turn_on", entity_id="light.living_room")

            # Turn on with brightness and color
            ha_call_service("light", "turn_on", entity_id="light.bedroom",
                          data={"brightness_pct": 75, "color_temp_kelvin": 2700})

            # Turn off all lights
            ha_call_service("light", "turn_off")
            ```

            **Climate Control:**
            ```python
            # Set temperature
            ha_call_service("climate", "set_temperature",
                          entity_id="climate.thermostat", data={"temperature": 22})

            # Change mode
            ha_call_service("climate", "set_hvac_mode",
                          entity_id="climate.living_room", data={"hvac_mode": "heat"})
            ```

            **Automation Control:**
            ```python
            # Trigger automation (replaces ha_trigger_automation)
            ha_call_service("automation", "trigger", entity_id="automation.morning_routine")

            # Turn automation on/off
            ha_call_service("automation", "turn_off", entity_id="automation.night_mode")
            ha_call_service("automation", "turn_on", entity_id="automation.security_check")
            ```

            **Scene Activation:**
            ```python
            # Activate scene
            ha_call_service("scene", "turn_on", entity_id="scene.movie_night")
            ha_call_service("scene", "turn_on", entity_id="scene.bedtime")
            ```

            **Input Helpers:**
            ```python
            # Set input number
            ha_call_service("input_number", "set_value",
                          entity_id="input_number.temp_offset", data={"value": 2.5})

            # Toggle input boolean
            ha_call_service("input_boolean", "toggle", entity_id="input_boolean.guest_mode")

            # Set input text
            ha_call_service("input_text", "set_value",
                          entity_id="input_text.status", data={"value": "Away"})
            ```

            **Universal Controls (works with any entity):**
            ```python
            # Universal toggle
            ha_call_service("homeassistant", "toggle", entity_id="switch.porch_light")

            # Universal turn on/off
            ha_call_service("homeassistant", "turn_on", entity_id="media_player.spotify")
            ha_call_service("homeassistant", "turn_off", entity_id="fan.ceiling_fan")
            ```

            **Script Execution:**
            ```python
            # Run script
            ha_call_service("script", "turn_on", entity_id="script.bedtime_routine")
            ha_call_service("script", "good_night_sequence")
            ```

            **Media Player Control:**
            ```python
            # Volume control
            ha_call_service("media_player", "volume_set",
                          entity_id="media_player.living_room", data={"volume_level": 0.5})

            # Play media
            ha_call_service("media_player", "play_media",
                          entity_id="media_player.spotify",
                          data={"media_content_type": "music", "media_content_id": "spotify:playlist:123"})
            ```

            **Cover Control:**
            ```python
            # Open/close covers
            ha_call_service("cover", "open_cover", entity_id="cover.garage_door")
            ha_call_service("cover", "close_cover", entity_id="cover.living_room_blinds")

            # Set position
            ha_call_service("cover", "set_cover_position",
                          entity_id="cover.bedroom_curtains", data={"position": 50})
            ```

            **Parameter Guidelines:**
            - **entity_id**: Optional for services that affect all entities of a domain
            - **data**: Service-specific parameters (brightness, temperature, volume, etc.)
            - Use ha_get_state() first to check current values and supported features
            - Use ha_get_domain_docs() for detailed service documentation
            """
            try:
                # Parse JSON data if provided as string
                try:
                    parsed_data = parse_json_param(data, "data")
                except ValueError as e:
                    return {
                        "success": False,
                        "error": f"Invalid data parameter: {e}",
                        "provided_data_type": type(data).__name__,
                    }

                # Ensure service_data is a dict
                service_data: dict[str, Any] = {}
                if parsed_data is not None:
                    if isinstance(parsed_data, dict):
                        service_data = parsed_data
                    else:
                        return {
                            "success": False,
                            "error": "Data parameter must be a JSON object",
                            "provided_type": type(parsed_data).__name__,
                        }

                if entity_id:
                    service_data["entity_id"] = entity_id
                result = await self.client.call_service(domain, service, service_data)

                return {
                    "success": True,
                    "domain": domain,
                    "service": service,
                    "entity_id": entity_id,
                    "parameters": data,
                    "result": result,
                    "message": f"Successfully executed {domain}.{service}",
                }
            except Exception as error:
                return {
                    "success": False,
                    "error": str(error),
                    "domain": domain,
                    "service": service,
                    "entity_id": entity_id,
                    "suggestions": [
                        f"Verify {entity_id} exists using ha_get_state()",
                        f"Check available services for {domain} domain using ha_get_domain_docs()",
                        f"For automation: ha_call_service('automation', 'trigger', entity_id='{entity_id}')",
                        f"For universal control: ha_call_service('homeassistant', 'toggle', entity_id='{entity_id}')",
                        "Use ha_search_entities() to find correct entity IDs",
                    ],
                    "examples": {
                        "automation_trigger": f"ha_call_service('automation', 'trigger', entity_id='{entity_id}')",
                        "universal_toggle": f"ha_call_service('homeassistant', 'toggle', entity_id='{entity_id}')",
                        "light_control": "ha_call_service('light', 'turn_on', entity_id='light.bedroom', data={'brightness_pct': 75})",
                    },
                }

    def _register_device_control_tools(self) -> None:
        """Register WebSocket-enabled device control tools."""

        @self.mcp.tool
        async def ha_get_operation_status(
            operation_id: str, timeout_seconds: int = 10
        ) -> dict[str, Any]:
            """Check status of device operation with real-time WebSocket verification."""
            result = await self.device_tools.get_device_operation_status(
                operation_id=operation_id, timeout_seconds=timeout_seconds
            )
            return cast(dict[str, Any], result)

        @self.mcp.tool
        async def ha_bulk_control(
            operations: str | list[dict[str, Any]], parallel: bool = True
        ) -> dict[str, Any]:
            """Control multiple devices with bulk operation support and WebSocket tracking."""
            # Parse JSON operations if provided as string
            try:
                parsed_operations = parse_json_param(operations, "operations")
            except ValueError as e:
                return {
                    "success": False,
                    "error": f"Invalid operations parameter: {e}",
                    "provided_operations_type": type(operations).__name__,
                }

            # Ensure operations is a list of dicts
            if parsed_operations is None or not isinstance(parsed_operations, list):
                return {
                    "success": False,
                    "error": "Operations parameter must be a list",
                    "provided_type": type(parsed_operations).__name__,
                }

            operations_list = cast(list[dict[str, Any]], parsed_operations)
            result = await self.device_tools.bulk_device_control(
                operations=operations_list, parallel=parallel
            )
            return cast(dict[str, Any], result)

        @self.mcp.tool
        async def ha_get_bulk_status(operation_ids: list[str]) -> dict[str, Any]:
            """Check status of multiple WebSocket-monitored operations."""
            result = await self.device_tools.get_bulk_operation_status(
                operation_ids=operation_ids
            )
            return cast(dict[str, Any], result)

    def _register_convenience_tools(self) -> None:
        """Register convenience tools for scenes, automations, and more."""

        @self.mcp.tool
        async def ha_activate_scene(scene_name: str) -> dict[str, Any]:
            """Activate a Home Assistant scene by name or entity ID."""
            result = await self.convenience_tools.activate_scene(scene_name=scene_name)
            return cast(dict[str, Any], result)

        @self.mcp.tool
        async def ha_get_weather(location: str | None = None) -> dict[str, Any]:
            """Get current weather information from Home Assistant weather entities."""
            result = await self.convenience_tools.get_weather_info(location=location)
            return cast(dict[str, Any], result)

        @self.mcp.tool
        async def ha_get_energy(period: str = "today") -> dict[str, Any]:
            """Get energy usage information from Home Assistant energy monitoring."""
            result = await self.convenience_tools.get_energy_usage(period=period)
            return cast(dict[str, Any], result)

        @self.mcp.tool
        @log_tool_usage
        async def ha_get_logbook(
            hours_back: int = 1,
            entity_id: str | None = None,
            end_time: str | None = None,
        ) -> dict[str, Any]:
            """Get Home Assistant logbook entries for the specified time period."""

            # Calculate start time
            if end_time:
                end_dt = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
            else:
                end_dt = datetime.now(UTC)

            start_dt = end_dt - timedelta(hours=hours_back)
            start_timestamp = start_dt.isoformat()

            try:
                response = await self.client.get_logbook(
                    entity_id=entity_id, start_time=start_timestamp, end_time=end_time
                )

                if not response:
                    no_entries_data = {
                        "success": False,
                        "error": "No logbook entries found",
                        "period": f"{hours_back} hours back from {end_dt.isoformat()}",
                    }
                    return await add_timezone_metadata(self.client, no_entries_data)

                logbook_data = {
                    "success": True,
                    "entries": response,
                    "period": f"{hours_back} hours back from {end_dt.isoformat()}",
                    "start_time": start_timestamp,
                    "end_time": end_dt.isoformat(),
                    "entity_filter": entity_id,
                    "total_entries": len(response) if isinstance(response, list) else 1,
                }
                return await add_timezone_metadata(self.client, logbook_data)

            except Exception as e:
                error_data = {
                    "success": False,
                    "error": f"Failed to retrieve logbook: {str(e)}",
                    "period": f"{hours_back} hours back from {end_dt.isoformat()}",
                }
                return await add_timezone_metadata(self.client, error_data)

        @self.mcp.tool
        @log_tool_usage
        async def ha_manage_helper(
            action: str,
            helper_type: str,
            name: str,
            helper_id: str | None = None,
            icon: str | None = None,
            area_id: str | None = None,
            labels: str | list[str] | None = None,
            min_value: float | None = None,
            max_value: float | None = None,
            step: float | None = None,
            unit_of_measurement: str | None = None,
            options: str | list[str] | None = None,
            initial: str | None = None,
            mode: str | None = None,
            has_date: bool | None = None,
            has_time: bool | None = None,
        ) -> dict[str, Any]:
            """
            Manage Home Assistant helpers - create, update, and delete helper entities for automation and UI control.

            SUPPORTED HELPER TYPES (6/29 total Home Assistant helpers):
            - input_button: Virtual buttons for triggering automations
            - input_boolean: Toggle switches/checkboxes
            - input_datetime: Date and time pickers
            - input_number: Numeric sliders or input boxes
            - input_select: Dropdown selection lists
            - input_text: Text input fields

            ACTIONS: 'create', 'update', 'delete'

            EXAMPLES:
            - Create button: ha_manage_helper("create", "input_button", "My Button", icon="mdi:bell")
            - Create boolean: ha_manage_helper("create", "input_boolean", "My Switch", icon="mdi:toggle-switch")
            - Create select: ha_manage_helper("create", "input_select", "My Options", options=["opt1", "opt2", "opt3"])
            - Create number: ha_manage_helper("create", "input_number", "Temperature", min_value=0, max_value=100, step=0.5, unit_of_measurement="°C")
            - Create datetime: ha_manage_helper("create", "input_datetime", "My DateTime", has_date=True, has_time=True, initial="2023-12-25 09:00:00")
            - Create date-only: ha_manage_helper("create", "input_datetime", "My Date", has_date=True, has_time=False, initial="2023-12-25")
            - Update helper: ha_manage_helper("update", "input_button", "New Name", helper_id="my_button", area_id="living_room", labels=["automation"])
            - Delete helper: ha_manage_helper("delete", "input_button", "", helper_id="my_button")

            OTHER HOME ASSISTANT HELPERS (not yet supported):
            Mathematical: bayesian, derivative, filter, integration, min_max, random, statistics, threshold, trend, utility_meter
            Time-based: history_stats, schedule, timer, tod
            Control: counter, generic_hygrostat, generic_thermostat, group, manual, switch_as_x, template
            Environmental: mold_indicator

            **FOR DETAILED HELPER DOCUMENTATION:** Use ha_get_domain_docs() with the specific helper domain.
            For example: ha_get_domain_docs("input_button"), ha_get_domain_docs("input_boolean"), etc.
            This provides comprehensive configuration options, limitations, and advanced features for each helper type.

            **IMPORTANT:** To get help with any specific helper type, use ha_get_domain_docs() with that helper's domain name.
            For instance, to understand all options for input_number helpers, call: ha_get_domain_docs("input_number")
            """
            try:
                # Parse JSON list parameters if provided as strings
                try:
                    labels = parse_string_list_param(labels, "labels")
                    options = parse_string_list_param(options, "options")
                except ValueError as e:
                    return {"success": False, "error": f"Invalid list parameter: {e}"}

                if action not in ["create", "update", "delete"]:
                    return {
                        "success": False,
                        "error": "Invalid action. Must be 'create', 'update', or 'delete'",
                        "valid_actions": ["create", "update", "delete"],
                    }

                if helper_type not in [
                    "input_button",
                    "input_boolean",
                    "input_select",
                    "input_number",
                    "input_text",
                    "input_datetime",
                ]:
                    return {
                        "success": False,
                        "error": f"Unsupported helper type: {helper_type}",
                        "supported_types": [
                            "input_button",
                            "input_boolean",
                            "input_select",
                            "input_number",
                            "input_text",
                            "input_datetime",
                        ],
                    }

                if action == "delete":
                    if not helper_id:
                        return {
                            "success": False,
                            "error": "helper_id is required for delete action",
                        }

                    # Convert helper_id to full entity_id if needed
                    entity_id = (
                        helper_id
                        if helper_id.startswith(helper_type)
                        else f"{helper_type}.{helper_id}"
                    )

                    # Try to get unique_id with retry logic to handle race conditions
                    unique_id = None
                    registry_result = None
                    max_retries = 3

                    for attempt in range(max_retries):
                        logger.info(
                            f"Getting entity registry for: {entity_id} (attempt {attempt + 1}/{max_retries})"
                        )

                        # Check if entity exists via state API first (faster check)
                        try:
                            state_check = await self.client.get_state(entity_id)
                            if not state_check:
                                # Entity doesn't exist in state, wait a bit for registration
                                if attempt < max_retries - 1:
                                    wait_time = 0.5 * (
                                        2**attempt
                                    )  # Exponential backoff: 0.5s, 1s, 2s
                                    logger.debug(
                                        f"Entity {entity_id} not found in state, waiting {wait_time}s before retry..."
                                    )
                                    await asyncio.sleep(wait_time)
                                    continue
                        except Exception as e:
                            logger.debug(f"State check failed for {entity_id}: {e}")

                        # Try registry lookup
                        registry_msg: dict[str, Any] = {
                            "type": "config/entity_registry/get",
                            "entity_id": entity_id,
                        }

                        try:
                            registry_result = await self.client.send_websocket_message(
                                registry_msg
                            )

                            if registry_result.get("success"):
                                entity_entry = registry_result.get("result", {})
                                unique_id = entity_entry.get("unique_id")
                                if unique_id:
                                    logger.info(
                                        f"Found unique_id: {unique_id} for {entity_id}"
                                    )
                                    break

                            # If registry lookup failed but we haven't exhausted retries, wait and try again
                            if attempt < max_retries - 1:
                                wait_time = 0.5 * (2**attempt)  # Exponential backoff
                                logger.debug(
                                    f"Registry lookup failed for {entity_id}, waiting {wait_time}s before retry..."
                                )
                                await asyncio.sleep(wait_time)

                        except Exception as e:
                            logger.warning(
                                f"Registry lookup attempt {attempt + 1} failed: {e}"
                            )
                            if attempt < max_retries - 1:
                                wait_time = 0.5 * (2**attempt)
                                await asyncio.sleep(wait_time)

                    # Fallback strategy 1: Try deletion with helper_id directly if unique_id not found
                    if not unique_id:
                        logger.info(
                            f"Could not find unique_id for {entity_id}, trying direct deletion with helper_id"
                        )

                        # Try deleting using helper_id directly (fallback approach)
                        delete_msg: dict[str, Any] = {
                            "type": f"{helper_type}/delete",
                            f"{helper_type}_id": helper_id,
                        }

                        logger.info(
                            f"Sending fallback WebSocket delete message: {delete_msg}"
                        )
                        result = await self.client.send_websocket_message(delete_msg)

                        if result.get("success"):
                            return {
                                "success": True,
                                "action": "delete",
                                "helper_type": helper_type,
                                "helper_id": helper_id,
                                "entity_id": entity_id,
                                "method": "fallback_direct_id",
                                "message": f"Successfully deleted {helper_type}: {helper_id} using direct ID (entity: {entity_id})",
                            }

                        # Fallback strategy 2: Check if entity was already deleted
                        try:
                            final_state_check = await self.client.get_state(entity_id)
                            if not final_state_check:
                                logger.info(
                                    f"Entity {entity_id} no longer exists, considering deletion successful"
                                )
                                return {
                                    "success": True,
                                    "action": "delete",
                                    "helper_type": helper_type,
                                    "helper_id": helper_id,
                                    "entity_id": entity_id,
                                    "method": "already_deleted",
                                    "message": f"Helper {helper_id} was already deleted or never properly registered",
                                }
                        except Exception:
                            pass

                        # Final fallback failed
                        return {
                            "success": False,
                            "error": f"Helper not found in entity registry after {max_retries} attempts: {registry_result.get('error', 'Unknown error') if registry_result else 'No registry response'}",
                            "helper_id": helper_id,
                            "entity_id": entity_id,
                            "suggestion": "Helper may not be properly registered or was already deleted. Use ha_search_entities() to verify.",
                        }

                    # Delete helper using unique_id (correct API from docs)
                    delete_message: dict[str, Any] = {
                        "type": f"{helper_type}/delete",
                        f"{helper_type}_id": unique_id,
                    }

                    logger.info(f"Sending WebSocket delete message: {delete_message}")
                    result = await self.client.send_websocket_message(delete_message)
                    logger.info(f"WebSocket delete response: {result}")

                    if result.get("success"):
                        return {
                            "success": True,
                            "action": "delete",
                            "helper_type": helper_type,
                            "helper_id": helper_id,
                            "entity_id": entity_id,
                            "unique_id": unique_id,
                            "method": "standard",
                            "message": f"Successfully deleted {helper_type}: {helper_id} (entity: {entity_id})",
                        }
                    else:
                        error_msg = result.get("error", "Unknown error")
                        # Handle specific HA error messages
                        if isinstance(error_msg, dict):
                            error_msg = error_msg.get("message", str(error_msg))

                        return {
                            "success": False,
                            "error": f"Failed to delete helper: {error_msg}",
                            "helper_id": helper_id,
                            "entity_id": entity_id,
                            "unique_id": unique_id,
                            "suggestion": "Make sure the helper exists and is not being used by automations or scripts",
                        }

                elif action == "create":
                    if not name:
                        return {
                            "success": False,
                            "error": "name is required for create action",
                        }

                    # Build create message based on helper type
                    message: dict[str, Any] = {"type": f"{helper_type}/create", "name": name}

                    if icon:
                        message["icon"] = icon

                    # Type-specific parameters
                    if helper_type == "input_select":
                        if not options:
                            return {
                                "success": False,
                                "error": "options list is required for input_select",
                            }
                        if not isinstance(options, list) or len(options) == 0:
                            return {
                                "success": False,
                                "error": "options must be a non-empty list for input_select",
                            }
                        message["options"] = options
                        if initial and initial in options:
                            message["initial"] = initial

                    elif helper_type == "input_number":
                        # Validate min_value/max_value range
                        if (
                            min_value is not None
                            and max_value is not None
                            and min_value > max_value
                        ):
                            return {
                                "success": False,
                                "error": f"Minimum value ({min_value}) cannot be greater than maximum value ({max_value})",
                                "min_value": min_value,
                                "max_value": max_value,
                            }

                        if min_value is not None:
                            message["min"] = min_value
                        if max_value is not None:
                            message["max"] = max_value
                        if step is not None:
                            message["step"] = step
                        if unit_of_measurement:
                            message["unit_of_measurement"] = unit_of_measurement
                        if mode in ["box", "slider"]:
                            message["mode"] = mode

                    elif helper_type == "input_text":
                        if min_value is not None:
                            message["min"] = int(min_value)
                        if max_value is not None:
                            message["max"] = int(max_value)
                        if mode in ["text", "password"]:
                            message["mode"] = mode
                        if initial:
                            message["initial"] = initial

                    elif helper_type == "input_boolean":
                        if initial is not None:
                            message["initial"] = initial.lower() in [
                                "true",
                                "on",
                                "yes",
                                "1",
                            ]

                    elif helper_type == "input_datetime":
                        # At least one of has_date or has_time must be True
                        if has_date is None and has_time is None:
                            # Default to both if not specified
                            message["has_date"] = True
                            message["has_time"] = True
                        elif has_date is None:
                            message["has_date"] = False
                            message["has_time"] = has_time
                        elif has_time is None:
                            message["has_date"] = has_date
                            message["has_time"] = False
                        else:
                            message["has_date"] = has_date
                            message["has_time"] = has_time

                        # Validate that at least one is True
                        if not message["has_date"] and not message["has_time"]:
                            return {
                                "success": False,
                                "error": "At least one of has_date or has_time must be True for input_datetime",
                            }

                        if initial:
                            message["initial"] = initial

                    result = await self.client.send_websocket_message(message)

                    if result.get("success"):
                        helper_data = result.get("result", {})
                        entity_id = helper_data.get("entity_id")

                        # Wait for entity to be properly registered before proceeding
                        if entity_id:
                            logger.debug(f"Waiting for {entity_id} to be registered...")
                            # Give the entity a moment to register in the system
                            await asyncio.sleep(0.2)

                            # Verify the entity is accessible via state API
                            max_verification_attempts = 5
                            for attempt in range(max_verification_attempts):
                                try:
                                    state_check = await self.client.get_state(entity_id)
                                    if state_check:
                                        logger.debug(
                                            f"Entity {entity_id} verified via state API"
                                        )
                                        break
                                except Exception:
                                    pass

                                if attempt < max_verification_attempts - 1:
                                    wait_time = 0.1 * (
                                        attempt + 1
                                    )  # 0.1s, 0.2s, 0.3s, 0.4s
                                    logger.debug(
                                        f"Entity {entity_id} not yet accessible, waiting {wait_time}s..."
                                    )
                                    await asyncio.sleep(wait_time)

                        # Update entity registry if area_id or labels specified
                        if (area_id or labels) and entity_id:
                            update_message: dict[str, Any] = {
                                "type": "config/entity_registry/update",
                                "entity_id": entity_id,
                            }
                            if area_id:
                                update_message["area_id"] = area_id
                            if labels:
                                update_message["labels"] = labels

                            update_result = await self.client.send_websocket_message(
                                update_message
                            )
                            if update_result.get("success"):
                                helper_data["area_id"] = area_id
                                helper_data["labels"] = labels

                        return {
                            "success": True,
                            "action": "create",
                            "helper_type": helper_type,
                            "helper_data": helper_data,
                            "entity_id": entity_id,
                            "message": f"Successfully created {helper_type}: {name}",
                        }
                    else:
                        return {
                            "success": False,
                            "error": f"Failed to create helper: {result.get('error', 'Unknown error')}",
                            "helper_type": helper_type,
                            "name": name,
                        }

                elif action == "update":
                    if not helper_id:
                        return {
                            "success": False,
                            "error": "helper_id is required for update action",
                        }

                    # For updates, we primarily use entity registry update
                    entity_id = (
                        helper_id
                        if helper_id.startswith(helper_type)
                        else f"{helper_type}.{helper_id}"
                    )

                    update_msg: dict[str, Any] = {
                        "type": "config/entity_registry/update",
                        "entity_id": entity_id,
                    }

                    if name:
                        update_msg["name"] = name
                    if icon:
                        update_msg["icon"] = icon
                    if area_id:
                        update_msg["area_id"] = area_id
                    if labels:
                        update_msg["labels"] = labels

                    result = await self.client.send_websocket_message(update_msg)

                    if result.get("success"):
                        entity_data = result.get("result", {}).get("entity_entry", {})
                        return {
                            "success": True,
                            "action": "update",
                            "helper_type": helper_type,
                            "entity_id": entity_id,
                            "updated_data": entity_data,
                            "message": f"Successfully updated {helper_type}: {entity_id}",
                        }
                    else:
                        return {
                            "success": False,
                            "error": f"Failed to update helper: {result.get('error', 'Unknown error')}",
                            "entity_id": entity_id,
                        }

            except Exception as e:
                return {
                    "success": False,
                    "error": f"Helper management failed: {str(e)}",
                    "action": action,
                    "helper_type": helper_type,
                    "suggestions": [
                        "Check Home Assistant connection",
                        "Verify helper_id exists for update/delete operations",
                        "Ensure required parameters are provided for the helper type",
                    ],
                }

            # This should never be reached due to the action validation above
            return {
                "success": False,
                "error": f"Invalid action: {action}",
                "action": action,
                "helper_type": helper_type,
            }

        @self.mcp.tool
        @log_tool_usage
        async def ha_manage_script(
            action: str,
            script_id: str | None = None,
            config: str | dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            """Manage Home Assistant scripts - get, create, update, or delete script configurations.

            Args:
                action: Action to perform ('get', 'create', 'update', 'delete')
                script_id: Script identifier - required for all actions
                config: Script configuration object - required for create/update

            Actions:
                - 'get': Retrieve script configuration
                - 'create': Create new script
                - 'update': Update existing script
                - 'delete': Delete script

            Required config fields (for create/update):
                - sequence: List of actions to execute

            Optional config fields:
                - alias: Display name (defaults to script_id)
                - description: Script description
                - icon: Icon to display
                - mode: Execution mode ('single', 'restart', 'queued', 'parallel')
                - max: Maximum concurrent executions (for queued/parallel modes)
                - fields: Input parameters for the script

            IMPORTANT: The 'config' parameter must be passed as a proper dictionary/object,
            NOT as a JSON string. Do not escape quotes or stringify the configuration.

            Examples:
                Get script:
                ha_manage_script("get", script_id="morning_routine")

                Create basic delay script:
                ha_manage_script("create", script_id="wait_script", config={
                    "sequence": [{"delay": {"seconds": 5}}],
                    "alias": "Wait 5 Seconds",
                    "description": "Simple delay script"
                })

                Create service call script:
                ha_manage_script("create", script_id="blink_light", config={
                    "sequence": [
                        {"service": "light.turn_on", "target": {"entity_id": "light.living_room"}},
                        {"delay": {"seconds": 2}},
                        {"service": "light.turn_off", "target": {"entity_id": "light.living_room"}}
                    ],
                    "alias": "Light Blink",
                    "mode": "single"
                })

                Create script with parameters:
                ha_manage_script("create", script_id="backup_script", config={
                    "alias": "Backup with Reference",
                    "description": "Create backup with optional reference parameter",
                    "fields": {
                        "reference": {
                            "name": "Reference",
                            "description": "Optional reference for backup identification",
                            "selector": {"text": None}
                        }
                    },
                    "sequence": [
                        {
                            "action": "hassio.backup_partial",
                            "data": {
                                "compressed": False,
                                "homeassistant": True,
                                "homeassistant_exclude_database": True,
                                "name": "Backup_{{ reference | default('auto') }}_{{ now().strftime('%Y%m%d_%H%M%S') }}"
                            }
                        }
                    ]
                })

                Update script:
                ha_manage_script("update", script_id="morning_routine", config={
                    "sequence": [
                        {"service": "light.turn_on", "target": {"area_id": "bedroom"}},
                        {"service": "climate.set_temperature", "target": {"entity_id": "climate.bedroom"}, "data": {"temperature": 22}}
                    ],
                    "alias": "Updated Morning Routine"
                })

                Delete script:
                ha_manage_script("delete", script_id="old_script")

            For detailed script configuration help, use: ha_get_domain_docs("script")

            Note: Scripts use Home Assistant's action syntax. Check the documentation for advanced
            features like conditions, variables, parallel execution, and service call options.
            """
            try:
                if action not in ["get", "create", "update", "delete"]:
                    return {
                        "success": False,
                        "error": "Invalid action. Must be 'get', 'create', 'update', or 'delete'",
                        "valid_actions": ["get", "create", "update", "delete"],
                    }

                if not script_id:
                    return {
                        "success": False,
                        "error": "script_id is required for all actions",
                    }

                if action == "get":
                    config_result = await self.client.get_script_config(script_id)
                    return {
                        "success": True,
                        "action": "get",
                        "script_id": script_id,
                        "config": config_result,
                    }

                elif action in ["create", "update"]:
                    if not config:
                        return {
                            "success": False,
                            "error": f"config is required for {action} action",
                            "required_fields": ["sequence"],
                        }

                    # Parse JSON config if provided as string
                    try:
                        parsed_config = parse_json_param(config, "config")
                    except ValueError as e:
                        return {
                            "success": False,
                            "error": f"Invalid config parameter: {e}",
                            "provided_config_type": type(config).__name__,
                        }

                    # Ensure config is a dict
                    if parsed_config is None or not isinstance(parsed_config, dict):
                        return {
                            "success": False,
                            "error": "Config parameter must be a JSON object",
                            "provided_type": type(parsed_config).__name__,
                        }

                    config_dict = cast(dict[str, Any], parsed_config)
                    result = await self.client.upsert_script_config(config_dict, script_id)
                    return {
                        "success": True,
                        "action": action,
                        **result,
                        "config_provided": config_dict,
                    }

                elif action == "delete":
                    result = await self.client.delete_script_config(script_id)
                    return {"success": True, "action": "delete", **result}

            except Exception as e:
                logger.error(f"Error managing script: {e}")
                return {
                    "success": False,
                    "action": action,
                    "script_id": script_id,
                    "error": str(e),
                    "suggestions": [
                        "Ensure config includes 'sequence' field for create/update",
                        "Validate sequence actions syntax",
                        "Check entity_ids exist if using service calls",
                        "Use ha_search_entities(domain_filter='script') to find scripts",
                        "Use ha_get_domain_docs('script') for configuration help",
                    ],
                }

            # This should never be reached due to the action validation above
            return {
                "success": False,
                "error": f"Invalid action: {action}",
                "action": action,
                "script_id": script_id,
            }

        @self.mcp.tool
        @log_tool_usage
        async def ha_manage_automation(
            action: str,
            identifier: str | None = None,
            config: str | dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            """
            Comprehensive Home Assistant automation management with full configuration support.

            This tool provides complete CRUD operations for Home Assistant automations with extensive documentation
            and examples covering all automation capabilities from basic time-based triggers to advanced templating.

            **ACTIONS:**
            - 'get': Retrieve automation configuration
            - 'create': Create new automation (identifier optional, generates unique_id if not provided)
            - 'update': Update existing automation (requires identifier)
            - 'delete': Delete automation (requires identifier)

            **PARAMETERS:**
            - action: Operation to perform
            - identifier: Automation entity_id (automation.name) or unique_id - required for get/update/delete
            - config: Complete automation configuration dictionary - required for create/update

            **AUTOMATION CONFIGURATION STRUCTURE:**

            **Required Fields:**
            - alias: Human-readable automation name
            - trigger: List of trigger conditions that start the automation
            - action: List of actions to execute when triggered

            **Optional Fields:**
            - description: Detailed automation description
            - condition: Additional conditions that must be met
            - mode: Execution behavior ('single', 'restart', 'queued', 'parallel')
            - max: Maximum concurrent executions (for queued/parallel modes)
            - initial_state: Whether automation starts enabled (true/false)
            - variables: Define variables for use in automation

            **COMPREHENSIVE TRIGGER TYPES:**

            **Time-Based Triggers:**
            ```python
            # Time trigger - specific time
            {"platform": "time", "at": "07:30:00"}
            {"platform": "time", "at": ["06:00:00", "20:00:00"]}  # Multiple times

            # Time pattern - periodic execution
            {"platform": "time_pattern", "minutes": 15}  # Every 15 minutes
            {"platform": "time_pattern", "hours": 2}     # Every 2 hours
            {"platform": "time_pattern", "seconds": "/30"} # Every 30 seconds

            # Sun-based triggers
            {"platform": "sun", "event": "sunrise"}
            {"platform": "sun", "event": "sunset", "offset": "-00:30:00"}  # 30 min before
            ```

            **State-Based Triggers:**
            ```python
            # Entity state change
            {"platform": "state", "entity_id": "light.living_room", "to": "on"}
            {"platform": "state", "entity_id": "sensor.temperature", "above": 25}
            {"platform": "state", "entity_id": "binary_sensor.door", "from": "off", "to": "on"}

            # Numeric state with templates
            {"platform": "numeric_state", "entity_id": "sensor.humidity", "below": 30}
            {"platform": "numeric_state", "entity_id": "sensor.battery",
             "below": 20, "for": {"minutes": 5}}  # Must stay below for 5 min
            ```

            **Event Triggers:**
            ```python
            # Device events (buttons, switches)
            {"platform": "device", "device_id": "abc123", "domain": "zha",
             "type": "remote_button_short_press", "subtype": "turn_on"}

            # Generic events
            {"platform": "event", "event_type": "automation_reloaded"}
            {"platform": "event", "event_type": "call_service",
             "event_data": {"domain": "light", "service": "turn_on"}}
            ```

            **Zone & Location Triggers:**
            ```python
            # Geographic zone entry/exit
            {"platform": "zone", "entity_id": "person.john", "zone": "zone.home", "event": "enter"}
            {"platform": "zone", "entity_id": "device_tracker.phone", "zone": "zone.work", "event": "leave"}

            # Geographic location
            {"platform": "geo_location", "source": "nsw_rural_fire_service_feed", "zone": "zone.home", "event": "enter"}
            ```

            **Template Triggers:**
            ```python
            # Advanced template-based triggers
            {"platform": "template", "value_template": "{{ states('sensor.temperature') | float > 25 }}"}
            {"platform": "template",
             "value_template": "{{ is_state('binary_sensor.workday', 'on') and now().hour == 7 }}"}
            ```

            **COMPREHENSIVE CONDITION TYPES:**

            **State Conditions:**
            ```python
            # Simple state checks
            {"condition": "state", "entity_id": "light.bedroom", "state": "off"}
            {"condition": "state", "entity_id": "person.john", "state": "home", "for": {"minutes": 10}}

            # Numeric conditions
            {"condition": "numeric_state", "entity_id": "sensor.temperature", "above": 20}
            {"condition": "numeric_state", "entity_id": "sensor.humidity", "below": 70, "above": 30}
            ```

            **Time Conditions:**
            ```python
            # Time-based conditions
            {"condition": "time", "after": "22:00:00", "before": "06:00:00"}  # Night time
            {"condition": "time", "weekday": ["mon", "tue", "wed", "thu", "fri"]}  # Weekdays

            # Sun conditions
            {"condition": "sun", "after": "sunset"}
            {"condition": "sun", "before": "sunrise", "after_offset": "-01:00:00"}
            ```

            **Template Conditions:**
            ```python
            # Advanced template conditions
            {"condition": "template", "value_template": "{{ states('sensor.battery') | int > 20 }}"}
            {"condition": "template",
             "value_template": "{{ is_state('binary_sensor.workday', 'on') and now().weekday() < 5 }}"}
            ```

            **Device Conditions:**
            ```python
            # Device-specific conditions
            {"condition": "device", "device_id": "abc123", "domain": "binary_sensor",
             "entity_id": "binary_sensor.motion", "type": "is_off"}
            ```

            **Zone Conditions:**
            ```python
            # Location-based conditions
            {"condition": "zone", "entity_id": "person.john", "zone": "zone.home"}
            {"condition": "zone", "entity_id": "device_tracker.phone", "zone": "zone.work"}
            ```

            **COMPREHENSIVE ACTION TYPES:**

            **Service Call Actions:**
            ```python
            # Basic service calls
            {"service": "light.turn_on", "target": {"entity_id": "light.living_room"}}
            {"service": "climate.set_temperature", "target": {"entity_id": "climate.bedroom"},
             "data": {"temperature": 22}}

            # Service calls with templates
            {"service": "notify.mobile_app", "data": {
                "message": "Temperature is {{ states('sensor.temperature') }}°C",
                "title": "Home Status"
            }}
            ```

            **Control Flow Actions:**
            ```python
            # Delays and waits
            {"delay": {"seconds": 30}}
            {"delay": {"minutes": 5}}
            {"delay": "00:02:00"}  # 2 minutes

            # Wait for state changes
            {"wait_for_trigger": [
                {"platform": "state", "entity_id": "binary_sensor.door", "to": "on"}
            ], "timeout": "00:05:00"}

            # Wait for templates
            {"wait_template": "{{ is_state('light.bedroom', 'on') }}", "timeout": "00:01:00"}
            ```

            **Conditional Actions:**
            ```python
            # If-then-else logic
            {"if": [{"condition": "state", "entity_id": "sun.sun", "state": "below_horizon"}],
             "then": [{"service": "light.turn_on", "target": {"entity_id": "light.porch"}}],
             "else": [{"service": "light.turn_off", "target": {"entity_id": "light.porch"}}]}

            # Choose between multiple options
            {"choose": [
                {"conditions": [{"condition": "state", "entity_id": "sensor.season", "state": "winter"}],
                 "sequence": [{"service": "climate.set_temperature", "data": {"temperature": 22}}]},
                {"conditions": [{"condition": "state", "entity_id": "sensor.season", "state": "summer"}],
                 "sequence": [{"service": "climate.set_temperature", "data": {"temperature": 18}}]}
             ],
             "default": [{"service": "climate.set_temperature", "data": {"temperature": 20}}]}
            ```

            **Loop Actions:**
            ```python
            # Repeat actions
            {"repeat": {
                "count": 3,
                "sequence": [
                    {"service": "light.toggle", "target": {"entity_id": "light.living_room"}},
                    {"delay": {"seconds": 1}}
                ]
            }}

            # Repeat while condition is true
            {"repeat": {
                "while": [{"condition": "state", "entity_id": "binary_sensor.motion", "state": "on"}],
                "sequence": [{"delay": {"seconds": 30}}]
            }}
            ```

            **Parallel Actions:**
            ```python
            # Execute actions simultaneously
            {"parallel": [
                [{"service": "light.turn_on", "target": {"area_id": "living_room"}}],
                [{"service": "media_player.play_media", "target": {"entity_id": "media_player.speakers"},
                  "data": {"media_content_type": "music", "media_content_id": "spotify:playlist:123"}}]
            ]}
            ```

            **EXECUTION MODES:**
            - **single** (default): Only one instance runs, new triggers ignored while running
            - **restart**: Stop current instance and start new one when triggered
            - **queued**: Queue up to 'max' instances, execute sequentially
            - **parallel**: Run up to 'max' instances simultaneously

            **TEMPLATE VARIABLES:**

            **Trigger Variables (available in actions/conditions):**
            - `trigger.platform`: Type of trigger (time, state, etc.)
            - `trigger.entity_id`: Entity that triggered (for state/numeric_state triggers)
            - `trigger.from_state`: Previous state object
            - `trigger.to_state`: New state object
            - `trigger.for`: Duration state was maintained
            - `trigger.now`: Timestamp when trigger fired

            **State Object Variables:**
            - `trigger.to_state.state`: Entity's new state value
            - `trigger.to_state.attributes`: All entity attributes
            - `trigger.to_state.last_changed`: When state last changed
            - `trigger.to_state.last_updated`: When state was last updated

            **This Context:**
            - `this.entity_id`: The automation's own entity ID
            - `this.state`: Current automation state (on/off)
            - `this.attributes`: Automation attributes (last_triggered, etc.)

            **BASIC EXAMPLES:**

            **Simple Time-Based Automation:**
            ```python
            ha_manage_automation("create", config={
                "alias": "Morning Lights",
                "description": "Turn on lights every morning at 7 AM",
                "trigger": [{"platform": "time", "at": "07:00:00"}],
                "action": [{"service": "light.turn_on", "target": {"area_id": "bedroom"}}]
            })
            ```

            **Motion-Activated Lighting:**
            ```python
            ha_manage_automation("create", config={
                "alias": "Motion Light",
                "trigger": [{"platform": "state", "entity_id": "binary_sensor.motion", "to": "on"}],
                "condition": [{"condition": "sun", "after": "sunset"}],
                "action": [
                    {"service": "light.turn_on", "target": {"entity_id": "light.hallway"}},
                    {"delay": {"minutes": 5}},
                    {"service": "light.turn_off", "target": {"entity_id": "light.hallway"}}
                ],
                "mode": "restart"
            })
            ```

            **ADVANCED EXAMPLES:**

            **Climate Control with Multiple Conditions:**
            ```python
            ha_manage_automation("create", config={
                "alias": "Smart Climate Control",
                "trigger": [
                    {"platform": "numeric_state", "entity_id": "sensor.temperature", "above": 25},
                    {"platform": "state", "entity_id": "binary_sensor.presence", "to": "on"}
                ],
                "condition": [
                    {"condition": "time", "after": "08:00:00", "before": "22:00:00"},
                    {"condition": "state", "entity_id": "climate.living_room", "state": "off"}
                ],
                "action": [
                    {"service": "climate.turn_on", "target": {"entity_id": "climate.living_room"}},
                    {"service": "climate.set_temperature", "target": {"entity_id": "climate.living_room"},
                     "data": {"temperature": 22}}
                ]
            })
            ```

            **Advanced Security Automation:**
            ```python
            ha_manage_automation("create", config={
                "alias": "Security Alert System",
                "trigger": [{"platform": "state", "entity_id": "binary_sensor.door", "to": "on"}],
                "condition": [
                    {"condition": "state", "entity_id": "alarm_control_panel.home", "state": "armed_away"},
                    {"condition": "template", "value_template": "{{ not is_state('person.owner', 'home') }}"}
                ],
                "action": [
                    {"service": "alarm_control_panel.alarm_trigger", "target": {"entity_id": "alarm_control_panel.home"}},
                    {"service": "light.turn_on", "target": {"area_id": "all"}, "data": {"brightness_pct": 100}},
                    {"service": "notify.mobile_app", "data": {
                        "message": "SECURITY ALERT: Door opened at {{ now().strftime('%H:%M:%S') }}",
                        "title": "Home Security",
                        "data": {"priority": "high", "ttl": 0}
                    }}
                ],
                "mode": "single"
            })
            ```

            **Dynamic Response Automation:**
            ```python
            ha_manage_automation("create", config={
                "alias": "Adaptive Lighting",
                "trigger": [{"platform": "sun", "event": "sunset", "offset": "-00:30:00"}],
                "variables": {
                    "brightness": "{{ 80 if is_state('binary_sensor.tv', 'on') else 60 }}",
                    "color_temp": "{{ 2700 if now().hour > 20 else 3000 }}"
                },
                "action": [
                    {"service": "light.turn_on", "target": {"area_id": "living_room"}, "data": {
                        "brightness_pct": "{{ brightness }}",
                        "color_temp_kelvin": "{{ color_temp }}"
                    }},
                    {"service": "notify.family", "data": {
                        "message": "Evening lights activated with {{ brightness }}% brightness"
                    }}
                ]
            })
            ```

            **MANAGEMENT EXAMPLES:**

            **Get automation:**
            ```python
            ha_manage_automation("get", identifier="automation.morning_routine")
            ha_manage_automation("get", identifier="my_unique_automation_id")  # By unique_id
            ```

            **Update automation:**
            ```python
            ha_manage_automation("update", identifier="automation.morning_routine", config={
                "alias": "Updated Morning Routine",
                "trigger": [{"platform": "time", "at": "06:30:00"}],  # Changed time
                "action": [
                    {"service": "light.turn_on", "target": {"area_id": "bedroom"}},
                    {"service": "climate.set_temperature", "target": {"entity_id": "climate.bedroom"},
                     "data": {"temperature": 22}}  # Added climate control
                ]
            })
            ```

            **Delete automation:**
            ```python
            ha_manage_automation("delete", identifier="automation.old_automation")
            ```

            **TROUBLESHOOTING TIPS:**
            - Use ha_get_state() to verify entity_ids exist and check their current states
            - Use ha_search_entities() to find correct entity_ids for your automations
            - Use ha_eval_template() to test Jinja2 template expressions before using them in automations
            - Search for similar existing automations with ha_search_entities(domain_filter='automation') for inspiration
            - Use description fields to document automation purpose and logic

            **For complete automation documentation:** ha_get_domain_docs("automation")
            **For template syntax help:** https://www.home-assistant.io/docs/configuration/templating/
            **For service reference:** Use ha_call_service() with different domains to explore available services
            """
            try:
                if action not in ["get", "create", "update", "delete"]:
                    return {
                        "success": False,
                        "error": "Invalid action. Must be 'get', 'create', 'update', or 'delete'",
                        "valid_actions": ["get", "create", "update", "delete"],
                    }

                if action == "get":
                    if not identifier:
                        return {
                            "success": False,
                            "error": "identifier is required for get action",
                        }

                    config_result = await self.client.get_automation_config(identifier)
                    return {
                        "success": True,
                        "action": "get",
                        "identifier": identifier,
                        "config": config_result,
                    }

                elif action in ["create", "update"]:
                    if not config:
                        return {
                            "success": False,
                            "error": f"config is required for {action} action",
                            "required_fields": ["alias", "trigger", "action"],
                        }

                    # Parse JSON config if provided as string
                    try:
                        parsed_config = parse_json_param(config, "config")
                    except ValueError as e:
                        return {
                            "success": False,
                            "error": f"Invalid config parameter: {e}",
                            "provided_config_type": type(config).__name__,
                        }

                    # Ensure config is a dict
                    if parsed_config is None or not isinstance(parsed_config, dict):
                        return {
                            "success": False,
                            "error": "Config parameter must be a JSON object",
                            "provided_type": type(parsed_config).__name__,
                        }

                    if action == "update" and not identifier:
                        return {
                            "success": False,
                            "error": "identifier is required for update action",
                        }

                    config_dict = cast(dict[str, Any], parsed_config)
                    result = await self.client.upsert_automation_config(
                        config_dict, identifier
                    )
                    return {
                        "success": True,
                        "action": action,
                        **result,
                        "config_provided": config_dict,
                    }

                elif action == "delete":
                    if not identifier:
                        return {
                            "success": False,
                            "error": "identifier is required for delete action",
                        }

                    result = await self.client.delete_automation_config(identifier)
                    return {"success": True, "action": "delete", **result}

                # This should never be reached due to the action validation above
                return {
                    "success": False,
                    "error": f"Invalid action: {action}",
                    "action": action,
                    "identifier": identifier,
                }

            except Exception as e:
                # Handle 404 errors gracefully for 'get' action (often used to verify deletion)
                error_str = str(e)
                if action == "get" and (
                    "404" in error_str
                    or "not found" in error_str.lower()
                    or "entity not found" in error_str.lower()
                ):
                    logger.debug(
                        f"Automation {identifier} not found (expected for deletion verification)"
                    )
                    return {
                        "success": False,
                        "action": action,
                        "identifier": identifier,
                        "error": f"Automation {identifier} does not exist",
                        "reason": "not_found",
                    }

                logger.error(f"Error managing automation: {e}")
                return {
                    "success": False,
                    "action": action,
                    "identifier": identifier,
                    "error": str(e),
                    "suggestions": [
                        "Check automation configuration format",
                        "Ensure required fields: alias, trigger, action",
                        "Use entity_id format: automation.morning_routine or unique_id",
                        "Use ha_search_entities(domain_filter='automation') to find automations",
                        "Use ha_get_domain_docs('automation') for configuration help",
                    ],
                }

        @self.mcp.tool
        @log_tool_usage
        async def ha_eval_template(
            template: str, timeout: int = 3, report_errors: bool = True
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
            try:
                # Generate unique ID for the template evaluation request
                import time

                request_id = int(time.time() * 1000) % 1000000  # Simple unique ID

                # Construct WebSocket message following the protocol
                message: dict[str, Any] = {
                    "type": "render_template",
                    "template": template,
                    "timeout": timeout,
                    "report_errors": report_errors,
                    "id": request_id,
                }

                # Send WebSocket message and get response
                result = await self.client.send_websocket_message(message)

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

        @self.mcp.tool
        async def ha_get_domain_docs(domain: str) -> dict[str, Any]:
            """Get comprehensive documentation for Home Assistant entity domains."""
            domain = domain.lower().strip()

            # GitHub URL for Home Assistant integration documentation
            github_url = f"https://raw.githubusercontent.com/home-assistant/home-assistant.io/refs/heads/current/source/_integrations/{domain}.markdown"

            try:
                # Fetch documentation from GitHub
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.get(github_url)

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
