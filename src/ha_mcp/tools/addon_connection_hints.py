"""Connection-failure guidance shared by app HTTP and WebSocket proxies."""

from .._version import is_running_in_addon
from ..client.rest_client import HomeAssistantClient


def addon_connection_failure_suggestions(
    client: HomeAssistantClient, port: int | None, slug: str
) -> list[str]:
    """Suggest setup checks for direct ports and route-specific ingress failures."""
    if port:
        return [
            f"Check ha_get_app(slug='{slug}'): app (add-on) running, port {port} "
            + "enabled under Configuration > Network, service listening. Restart "
            + "after configuration changes.",
            "If the app requires leave_front_door_open for direct access, "
            + "enable it only with explicit user approval: it removes direct-access "
            + "authentication. Restart afterward.",
            "Direct-port access requires the MCP host to share Home "
            + "Assistant's container network. On PyPI/uvx installs, drop "
            + "the 'port' parameter to route through Ingress instead.",
        ]
    if is_running_in_addon():
        return [
            "The target app (add-on) container may not be reachable from this "
            + "MCP app. Check that the target app is running.",
            "If the failure persists, the app (add-on) Docker network may be "
            + "unhealthy — try restarting the target app, then this "
            + "MCP app.",
        ]
    return [
        f"Verify Home Assistant is reachable at {client.base_url}",
        "Check network connectivity from the MCP host to HA Core",
    ]
