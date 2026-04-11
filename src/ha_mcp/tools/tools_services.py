"""
Service discovery tools for Home Assistant MCP server.

This module provides service listing and discovery capabilities,
allowing AI to explore available Home Assistant services/actions.
"""

import logging
from typing import Annotated, Any, Literal

from fastmcp.exceptions import ToolError
from fastmcp.tools import tool
from pydantic import Field

from ..errors import ErrorCode, create_error_response
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)
from .util_helpers import build_pagination_metadata, coerce_int_param

logger = logging.getLogger(__name__)


class ServiceDiscoveryTools:
    """Service discovery tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @tool(
        name="ha_list_services",
        tags={"Service & Device Control"},
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "title": "List Available Services",
        },
    )
    @log_tool_usage
    async def ha_list_services(
        self,
        domain: str | None = None,
        query: str | None = None,
        limit: Annotated[
            int | str,
            Field(
                default=50,
                description="Max services to return per page (default: 50)",
            ),
        ] = 50,
        offset: Annotated[
            int | str,
            Field(
                default=0,
                description="Number of services to skip for pagination (default: 0)",
            ),
        ] = 0,
        detail_level: Annotated[
            Literal["summary", "full"],
            Field(
                default="summary",
                description=(
                    "'summary': service name + description only (default). "
                    "'full': include parameter field schemas."
                ),
            ),
        ] = "summary",
    ) -> dict[str, Any]:
        """List available Home Assistant services with optional pagination and detail control.

        Discovers services/actions that can be called via ha_call_service.
        Use domain or query filters to narrow results. Defaults to summary mode
        (name + description only) to keep responses compact.

        Args:
            domain: Filter by domain (e.g., 'light', 'switch', 'climate').
            query: Search in service names and descriptions.
            limit: Max services per page (default: 50).
            offset: Pagination offset (default: 0).
            detail_level: 'summary' (default) returns name/description only;
                         'full' includes parameter field schemas.

        Examples:
            # Browse first page of all services (compact)
            ha_list_services()

            # List all light services with full parameter details
            ha_list_services(domain="light", detail_level="full")

            # Search for temperature-related services
            ha_list_services(query="temperature")

            # Paginate through all services
            ha_list_services(offset=50)
        """
        try:
            limit_int = coerce_int_param(
                limit, "limit", default=50, min_value=1, max_value=200
            )
            offset_int = coerce_int_param(offset, "offset", default=0, min_value=0)

            # Get services from REST API (includes parameter definitions)
            rest_services = await self._client.get_services()

            # Get translations for service descriptions via WebSocket
            translations = await _get_service_translations(self._client)

            # Process and filter services
            result = _process_services(
                rest_services=rest_services,
                translations=translations,
                domain_filter=domain,
                query_filter=query,
                limit=limit_int,
                offset=offset_int,
                detail_level=detail_level,
            )

            return result

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Failed to list services: {e}")
            exception_to_structured_error(
                e,
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify WebSocket API is available",
                    "Try with a specific domain filter",
                ],
            )


def register_services_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register service discovery tools with the MCP server."""
    register_tool_methods(mcp, ServiceDiscoveryTools(client))


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
    limit: int = 50,
    offset: int = 0,
    detail_level: Literal["summary", "full"] = "summary",
) -> dict[str, Any]:
    """
    Process raw service data into structured output.

    Args:
        rest_services: Raw services from REST API
        translations: Service translations from WebSocket
        domain_filter: Optional domain to filter by
        query_filter: Optional search query
        limit: Maximum number of services per page
        offset: Number of services to skip
        detail_level: 'summary' or 'full'

    Returns:
        Processed service dictionary with pagination metadata
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
        raise_tool_error(
            create_error_response(
                ErrorCode.INTERNAL_UNEXPECTED,
                "Unexpected service data format",
                suggestions=[
                    "Retry the request — this may be a transient issue",
                    "Check Home Assistant is running and responding correctly",
                ],
            )
        )

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
            entry = _build_service_entry(
                domain, service_name, service_def, translations,
                query_lower, detail_level,
            )
            if entry is not None:
                services[service_key] = entry

    # Sort domains alphabetically
    sorted_domains = sorted(domains_seen)

    # Apply pagination to the collected services
    all_keys = list(services.keys())
    total_count = len(all_keys)
    paginated_keys = all_keys[offset : offset + limit]
    paginated_services = {k: services[k] for k in paginated_keys}

    return {
        "success": True,
        "domains": sorted_domains,
        "services": paginated_services,
        **build_pagination_metadata(
            total_count, offset, limit, len(paginated_services)
        ),
        "detail_level": detail_level,
        "filters_applied": {
            "domain": domain_filter,
            "query": query_filter,
        },
    }


def _build_service_entry(
    domain: str,
    service_name: str,
    service_def: dict[str, Any],
    translations: dict[str, Any],
    query_lower: str | None,
    detail_level: Literal["summary", "full"],
) -> dict[str, Any] | None:
    """Build a single service entry, returning None if it doesn't match the query."""
    translation_key = f"component.{domain}.services.{service_name}"
    service_trans = translations.get(translation_key, {})

    name = service_trans.get("name", service_name.replace("_", " ").title())
    description = service_trans.get(
        "description",
        service_def.get("description", ""),
    )

    # Apply query filter
    if query_lower:
        searchable = f"{domain}.{service_name} {name} {description}".lower()
        if query_lower not in searchable:
            return None

    entry: dict[str, Any] = {
        "name": name,
        "description": description,
        "domain": domain,
        "service": service_name,
    }

    # Include full field schemas only in 'full' detail mode
    if detail_level == "full":
        entry["fields"] = _process_service_fields(
            service_def.get("fields", {}),
            service_trans.get("fields", {}),
        )

    # Add target only if present
    target = service_def.get("target")
    if target is not None:
        entry["target"] = target

    return entry


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


_SIMPLE_SELECTOR_TYPES: dict[str, str] = {
    "boolean": "boolean",
    "text": "text",
    "target": "target (entity/area/device)",
    "time": "time",
    "date": "date",
    "datetime": "datetime",
    "color_temp": "color_temp_kelvin",
    "color_temp_kelvin": "color_temp_kelvin",
    "color_rgb": "color_rgb",
    "object": "object",
    "template": "template",
    "area": "area",
    "device": "device",
    "duration": "duration",
}


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

    for key, type_name in _SIMPLE_SELECTOR_TYPES.items():
        if key in selector:
            return type_name
    if "number" in selector:
        return _get_number_type(selector["number"])

    if "select" in selector:
        return _get_select_type(selector["select"])

    if "entity" in selector:
        return _get_entity_type(selector["entity"])

    # Return the first key as type name
    selector_types = list(selector.keys())
    if selector_types:
        return selector_types[0]

    return "any"


def _get_number_type(num_sel: Any) -> str:
    """Format number selector type with optional range."""
    if isinstance(num_sel, dict) and "min" in num_sel and "max" in num_sel:
        return f"number ({num_sel['min']}-{num_sel['max']})"
    return "number"


def _get_select_type(select_sel: Any) -> str:
    """Format select selector type with inline options for small lists."""
    if isinstance(select_sel, dict):
        options = select_sel.get("options", [])
        if options and len(options) <= 5:
            option_values = [
                opt.get("value", opt) if isinstance(opt, dict) else opt
                for opt in options
            ]
            return f"select ({', '.join(str(v) for v in option_values)})"
    return "select"


def _get_entity_type(entity_sel: Any) -> str:
    """Format entity selector type with domain constraint."""
    if isinstance(entity_sel, dict) and "domain" in entity_sel:
        domains = entity_sel["domain"]
        if isinstance(domains, list):
            return f"entity ({', '.join(domains)})"
        return f"entity ({domains})"
    return "entity"
