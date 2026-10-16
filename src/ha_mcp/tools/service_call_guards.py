"""Guards on what ``ha_call_service`` may forward to Home Assistant.

Each one closes a bypass around a dedicated tool: the ``ha_mcp_tools``
services have their own wrappers, and reloading ha-mcp's own config entry is
refused like every other generic write to it.
"""

from typing import Any

from ..client.rest_client import HomeAssistantAPIError
from ..errors import create_validation_error
from .backup_access import guard_snapshot_route
from .config_entry_flow import is_component_domain, refuse_component_entry
from .helpers import raise_tool_error


def validate_service_call_params(
    domain: str | None, service: str | None
) -> tuple[str, str]:
    """Validate service-mode params and return the (domain, service) pair.

    Raises a structured ToolError when domain/service are missing (the caller
    likely wants the ws_command escape hatch), when the domain targets the
    reserved ha_mcp_tools namespace, or when backup controls block the service.
    """
    if not domain or not service:
        raise_tool_error(
            create_validation_error(
                "domain and service are required for a service call. To send "
                "a raw WebSocket command instead, pass ws_command.",
                parameter="domain" if not domain else "service",
            )
        )
    # ha_mcp_tools.* services are restricted to the ha-mcp server's dedicated
    # wrappers (which inject the required caller token). Block ha_call_service
    # from forwarding to that domain — it would otherwise be a bypass path
    # around the dedicated tools. HA core's service registry lowercases the
    # domain on fallback lookup (homeassistant/core.py
    # ServiceRegistry.async_call); is_component_domain normalises, so a
    # mixed-case `HA_MCP_TOOLS` can't slip past and still resolve downstream.
    if is_component_domain(domain):
        raise_tool_error(
            create_validation_error(
                (
                    "ha_call_service cannot invoke services in the "
                    "'ha_mcp_tools' domain. Use the dedicated MCP tool "
                    "instead: ha_list_files, ha_read_file, ha_write_file, "
                    "ha_delete_file, or ha_config_set_yaml."
                ),
                parameter="domain",
            )
        )
    guard_snapshot_route(domain=domain, service=service)
    return domain, service


async def refuse_component_entry_reload(
    client: Any, domain: str, service: str, service_data: dict[str, Any]
) -> None:
    """Refuse ``homeassistant.reload_config_entry`` on ha-mcp's own entry.

    Only an explicit ``entry_id`` is checked; Core also resolves entries from
    entity, device and area targets. An entry Core does not have is left to
    Core's own error.
    """
    entry_id = service_data.get("entry_id")
    if (
        domain.strip().lower() != "homeassistant"
        or service.strip().lower() != "reload_config_entry"
        or not isinstance(entry_id, str)
    ):
        return
    try:
        entry = await client.get_config_entry(entry_id)
    except HomeAssistantAPIError as err:
        if err.status_code == 404:
            return
        raise
    refuse_component_entry(entry.get("domain"), action="reload", entry_id=entry_id)
