"""Connection-failure guidance shared by app HTTP and WebSocket proxies."""

from .._version import is_running_in_addon
from ..client.rest_client import HomeAssistantClient


def addon_connection_failure_suggestions(
    client: HomeAssistantClient,
    port: int | None,
    slug: str,
    *,
    timed_out: bool = False,
) -> list[str]:
    """Reserve listener setup advice for connection failures, not timeouts."""
    if timed_out:
        return [
            "Check app logs for slow or blocked operations. The operation may "
            + "still be running; check its status before retrying.",
        ]
    if port:
        suggestions = [
            f"Check ha_get_app(slug='{slug}'): app (add-on) running and service "
            + f"listening on internal port {port}. Bridge-network apps do not need "
            + "a published host port. Check app logs for listener failures.",
        ]
        if port == 6052 and slug.rsplit("_", 1)[-1] in {
            "esphome",
            "esphome-beta",
            "esphome-dev",
        }:
            suggestions.append(
                "For ESPHome: enable its dashboard port under Configuration > "
                "Network and leave_front_door_open, then restart. This enables its "
                "direct listener without authentication; get explicit user approval "
                "before enabling it."
            )
        return [
            *suggestions,
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
