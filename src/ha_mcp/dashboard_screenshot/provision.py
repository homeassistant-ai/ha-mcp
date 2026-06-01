"""Locate the dashboard screenshot engine for the current deployment.

Three deployment modes, resolved lazily on first tool use (never at
startup, never silently installed):

1. **Explicit** — ``HAMCP_DASHBOARD_SCREENSHOT_ENGINE_URL`` is set. Used
   verbatim. This is the Docker / Container path (a docker-compose
   sidecar), and also lets HA OS users override auto-discovery.
2. **HA OS / Supervised** — ``SUPERVISOR_TOKEN`` is present. The engine
   add-on (``*_ha_mcp_screenshot``) is discovered via the Supervisor REST
   API and reached on the internal network by its hostname. The user must
   have installed + started it themselves (one click from this repo's
   add-on store) — we never auto-install.
3. **Neither** (stdio / standalone) — a clear :class:`ToolError` explaining
   how to enable it.
"""

from __future__ import annotations

import logging
import os

import httpx
from fastmcp.exceptions import ToolError

from ..errors import ErrorCode, create_error_response
from ..tools.helpers import raise_tool_error

logger = logging.getLogger(__name__)

ENGINE_PORT = 10000
# Full Supervisor slug is ``<repo-hash>_ha_mcp_screenshot``; match the suffix.
ENGINE_SLUG_SUFFIX = "_ha_mcp_screenshot"

_INSTALL_HELP = (
    "Dashboard screenshot mode is enabled, but the screenshot engine add-on "
    "is not installed. On HA OS / Supervised: (1) install + start it — either "
    "let the assistant do it via "
    "ha_manage_addon(slug='<prefix>_ha_mcp_screenshot', action='install') then "
    "action='start' (find the exact slug with ha_get_addon(source='available', "
    "query='screenshot')), or install the 'HA MCP Dashboard Screenshot Engine' "
    "add-on yourself from this repository's store under Settings > Add-ons; and "
    "(2) it REQUIRES a Home Assistant long-lived access token — create one in "
    "Profile > Security (ideally for a dedicated low-privilege user), set it as "
    "the add-on's 'access_token' option, and (re)start the add-on. Without a "
    "token the engine only serves a configuration-instructions page. On "
    "Docker / Container, run the engine as a sidecar (with its access_token "
    "set) and point ha-mcp at it via "
    "HAMCP_DASHBOARD_SCREENSHOT_ENGINE_URL (e.g. http://ha-mcp-screenshot:10000)."
)

_NOT_STARTED_HELP = (
    "The 'HA MCP Dashboard Screenshot Engine' add-on is installed but not "
    "started. Open Settings > Add-ons > HA MCP Dashboard Screenshot Engine, "
    "set its 'access_token' option to a Home Assistant long-lived access token "
    "(Profile > Security) if you haven't, and start it (enable 'Start on boot' "
    "to keep it available)."
)

_STDIO_HELP = (
    "Dashboard screenshot mode needs either HA OS / Supervised (install the "
    "screenshot engine add-on from this repo's store) or a screenshot sidecar "
    "reachable via HAMCP_DASHBOARD_SCREENSHOT_ENGINE_URL. This deployment "
    "(stdio / standalone) can host neither — see docs/beta.md."
)


async def resolve_engine_url() -> str:
    """Return the base URL of the screenshot engine, or raise ToolError.

    See module docstring for the three-mode resolution order.
    """
    from ..config import get_global_settings

    explicit = (get_global_settings().dashboard_screenshot_engine_url or "").strip()
    if explicit:
        return explicit.rstrip("/")

    if os.environ.get("SUPERVISOR_TOKEN"):
        return await _discover_engine_url_via_supervisor()

    raise_tool_error(
        create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            "Dashboard screenshot mode is not available in this deployment.",
            details=_STDIO_HELP,
            suggestions=[
                "Use HA OS / Supervised and install the screenshot engine add-on",
                "Or run the engine as a sidecar and set "
                "HAMCP_DASHBOARD_SCREENSHOT_ENGINE_URL",
            ],
        )
    )


async def _discover_engine_url_via_supervisor() -> str:
    """Find the engine add-on via the Supervisor and return its internal URL.

    Requires the ha-mcp add-on's ``manager`` role (already declared) for the
    read-only ``/addons`` + ``/addons/<slug>/info`` endpoints. Raises a
    ToolError with actionable guidance when the engine is missing or stopped.
    """
    from ..client.supervisor_client import make_supervisor_httpx_client

    try:
        async with make_supervisor_httpx_client(timeout=15.0, verify=True) as sup:
            listing = await sup.get("/addons")
            listing.raise_for_status()
            addons = listing.json().get("data", {}).get("addons", [])

            matches = [
                a for a in addons if str(a.get("slug", "")).endswith(ENGINE_SLUG_SUFFIX)
            ]
            if not matches:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.RESOURCE_NOT_FOUND,
                        "The screenshot engine add-on is not installed.",
                        details=_INSTALL_HELP,
                    )
                )

            # The /addons list is only reliable for slug discovery (the
            # repo-hash prefix is not known ahead of time). Per-addon ``state``
            # and ``hostname`` are authoritative on /addons/<slug>/info — the
            # same source ha_manage_addon and the start fixture trust. The list
            # can report a stale/absent ``state`` for a freshly-started local
            # add-on, so read state from /info, not from the listing.
            slug = matches[0]["slug"]
            info = await sup.get(f"/addons/{slug}/info")
            info.raise_for_status()
            data = info.json().get("data", {})

            if data.get("state") != "started":
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        "The screenshot engine add-on is installed but not started.",
                        details=_NOT_STARTED_HELP,
                        context={"slug": slug, "state": data.get("state")},
                    )
                )

            hostname = data.get("hostname") or data.get("ip_address")
            if not hostname:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        f"Screenshot engine add-on '{slug}' is started but the "
                        "Supervisor returned no hostname/ip_address for it.",
                        context={"slug": slug},
                    )
                )
            return f"http://{hostname}:{ENGINE_PORT}"
    except ToolError:
        raise
    except (httpx.HTTPError, KeyError, ValueError) as e:
        logger.warning("Screenshot engine discovery via Supervisor failed: %s", e)
        raise_tool_error(
            create_error_response(
                ErrorCode.CONNECTION_FAILED,
                "Could not query the Supervisor to locate the screenshot "
                "engine add-on.",
                details=str(e),
                suggestions=[
                    "Verify the add-on is installed and started",
                    "Or set HAMCP_DASHBOARD_SCREENSHOT_ENGINE_URL explicitly",
                ],
            )
        )
