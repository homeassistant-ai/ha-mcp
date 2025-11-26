"""
Service discovery tools for Home Assistant MCP server.

This module provides service listing and discovery capabilities,
allowing AI to explore available Home Assistant services/actions.
"""

import logging
from typing import Any

from .helpers import log_tool_usage

logger = logging.getLogger(__name__)


def register_services_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register service discovery tools with the MCP server."""

    @mcp.tool(annotations={"readOnlyHint": True})
    @log_tool_usage
    async def ha_list_services(
        domain: str | None = None,
        query: str | None = None,
    ) -> dict[str, Any]:
        """
        List available Home Assistant services with their parameters.

        Discovers services/actions that can be called via ha_call_service.
        Returns service definitions including field names, types, and descriptions.

        Args:
            domain: Filter by domain (e.g., 'light', 'switch', 'climate').
                   If not provided, returns services from all domains.
            query: Search in service names and descriptions.
                   Matches against service IDs and their descriptions.

        Returns:
            Dictionary with:
            - success: Whether the operation succeeded
            - domains: List of available domains (when no domain filter)
            - services: Dictionary of service definitions keyed by domain.service
            - total_count: Total number of services returned

        Examples:
            # List all light services
            ha_list_services(domain="light")

            # Search for services related to temperature
            ha_list_services(query="temperature")

            # Get all available services (may be large)
            ha_list_services()
        """
        try:
            # Get services from REST API (includes parameter definitions)
            rest_services = await client.get_services()

            # Get translations for service descriptions via WebSocket
            translations = await _get_service_translations(client)

            # Process and filter services
            result = _process_services(
                rest_services=rest_services,
                translations=translations,
                domain_filter=domain,
                query_filter=query,
            )

            return result

        except Exception as e:
            logger.error(f"Failed to list services: {e}")
            return {
                "success": False,
                "error": str(e),
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify WebSocket API is available",
                    "Try with a specific domain filter",
                ],
            }


async def _get_service_translations(client: Any) -> dict[str, Any]:
    """
    Get service translations from Home Assistant via WebSocket.

    Uses the frontend/get_translations command to retrieve
    human-readable service names and descriptions.
    """
    try:
        response = await client.send_websocket_message(
            {
                "type": "frontend/get_translations",
                "language": "en",
                "category": "services",
            }
        )

        if response.get("success") and response.get("result"):
            result = response["result"]
            if isinstance(result, dict):
                resources: dict[str, Any] = result.get("resources", {})
                return resources
        return {}

    except Exception as e:
        logger.warning(f"Failed to get service translations: {e}")
        return {}


def _process_services(
    rest_services: Any,
    translations: dict[str, Any],
    domain_filter: str | None = None,
    query_filter: str | None = None,
) -> dict[str, Any]:
    """
    Process raw service data into structured output.

    Args:
        rest_services: Raw services from REST API
        translations: Service translations from WebSocket
        domain_filter: Optional domain to filter by
        query_filter: Optional search query

    Returns:
        Processed service dictionary
    """
    services: dict[str, dict[str, Any]] = {}
    domains_seen: set[str] = set()

    # Handle both list and dict formats from REST API
    if isinstance(rest_services, list):
        # Format: [{"domain": "light", "services": {...}}, ...]
        service_data = rest_services
    elif isinstance(rest_services, dict):
        # Format: {"light": {"services": {...}}, ...}
        service_data = [
            {"domain": domain, "services": data.get("services", data)}
            for domain, data in rest_services.items()
        ]
    else:
        return {
            "success": False,
            "error": "Unexpected service data format",
            "services": {},
            "domains": [],
            "total_count": 0,
        }

    query_lower = query_filter.lower() if query_filter else None

    for domain_entry in service_data:
        domain = domain_entry.get("domain", "")
        if not domain:
            continue

        # Apply domain filter
        if domain_filter and domain != domain_filter:
            continue

        domains_seen.add(domain)
        domain_services = domain_entry.get("services", {})

        for service_name, service_def in domain_services.items():
            service_key = f"{domain}.{service_name}"

            # Get translations for this service
            translation_key = f"component.{domain}.services.{service_name}"
            service_trans = translations.get(translation_key, {})

            # Build service description
            name = service_trans.get("name", service_name.replace("_", " ").title())
            description = service_trans.get(
                "description",
                service_def.get("description", ""),
            )

            # Apply query filter
            if query_lower:
                searchable = f"{service_key} {name} {description}".lower()
                if query_lower not in searchable:
                    continue

            # Process fields/parameters
            fields = _process_service_fields(
                service_def.get("fields", {}),
                service_trans.get("fields", {}),
            )

            # Build service entry
            services[service_key] = {
                "name": name,
                "description": description,
                "domain": domain,
                "service": service_name,
                "fields": fields,
            }

            # Add target only if present
            target = service_def.get("target")
            if target is not None:
                services[service_key]["target"] = target

    # Sort domains alphabetically
    sorted_domains = sorted(domains_seen)

    return {
        "success": True,
        "domains": sorted_domains,
        "services": services,
        "total_count": len(services),
        "filters_applied": {
            "domain": domain_filter,
            "query": query_filter,
        },
    }


def _process_service_fields(
    fields_def: dict[str, Any],
    fields_trans: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """
    Process service field definitions into structured output.

    Args:
        fields_def: Field definitions from REST API
        fields_trans: Field translations from WebSocket

    Returns:
        Dictionary of processed field definitions
    """
    processed: dict[str, dict[str, Any]] = {}

    for field_name, field_info in fields_def.items():
        trans = fields_trans.get(field_name, {})

        # Determine field type from selector
        selector = field_info.get("selector", {})
        field_type = _get_field_type(selector)

        processed[field_name] = {
            "name": trans.get("name", field_name.replace("_", " ").title()),
            "description": trans.get(
                "description",
                field_info.get("description", ""),
            ),
            "required": field_info.get("required", False),
            "type": field_type,
            "example": trans.get("example", field_info.get("example")),
        }

        # Add selector details for complex types
        if selector:
            processed[field_name]["selector"] = selector

        # Add default value if present
        if "default" in field_info:
            processed[field_name]["default"] = field_info["default"]

    return processed


def _get_field_type(selector: dict[str, Any]) -> str:
    """
    Determine field type from selector definition.

    Args:
        selector: Field selector from service definition

    Returns:
        Human-readable type string
    """
    if not selector:
        return "any"

    # Check for common selector types
    if "number" in selector:
        num_sel = selector["number"]
        if isinstance(num_sel, dict) and "min" in num_sel and "max" in num_sel:
            return f"number ({num_sel['min']}-{num_sel['max']})"
        return "number"

    if "boolean" in selector:
        return "boolean"

    if "text" in selector:
        return "text"

    if "select" in selector:
        select_sel = selector["select"]
        if isinstance(select_sel, dict):
            options = select_sel.get("options", [])
            if options and len(options) <= 5:
                # Show options inline for small lists
                option_values = [
                    opt.get("value", opt) if isinstance(opt, dict) else opt
                    for opt in options
                ]
                return f"select ({', '.join(str(v) for v in option_values)})"
        return "select"

    if "entity" in selector:
        entity_sel = selector["entity"]
        if isinstance(entity_sel, dict) and "domain" in entity_sel:
            domains = entity_sel["domain"]
            if isinstance(domains, list):
                return f"entity ({', '.join(domains)})"
            return f"entity ({domains})"
        return "entity"

    if "target" in selector:
        return "target (entity/area/device)"

    if "time" in selector:
        return "time"

    if "date" in selector:
        return "date"

    if "datetime" in selector:
        return "datetime"

    if "color_temp" in selector:
        return "color_temp"

    if "color_rgb" in selector:
        return "color_rgb"

    if "object" in selector:
        return "object"

    if "template" in selector:
        return "template"

    if "area" in selector:
        return "area"

    if "device" in selector:
        return "device"

    if "duration" in selector:
        return "duration"

    # Return the first key as type name
    selector_types = list(selector.keys())
    if selector_types:
        return selector_types[0]

    return "any"
