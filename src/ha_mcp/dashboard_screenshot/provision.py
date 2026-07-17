"""Locate the dashboard screenshot engine for the current deployment.

The engine is balloob's **Puppet** add-on (https://github.com/balloob/
home-assistant-addons), a headless-Chromium renderer. ha-mcp does not vendor
it — the user installs it themselves.

Three deployment modes, resolved lazily on first tool use (never at
startup, never silently installed):

1. **Explicit** — ``HAMCP_DASHBOARD_SCREENSHOT_ENGINE_URL`` is set. Used
   verbatim. This is the Docker / Container path (a docker-compose
   sidecar), and also lets HA OS users override auto-discovery.
2. **HA OS / Supervised** — ``SUPERVISOR_TOKEN`` is present. The Puppet
   add-on (slug ``*_puppet``) is discovered via the Supervisor REST API and
   reached on the internal network by its hostname. The user must have
   installed + started it themselves (add balloob's add-on repository, then
   install "Puppet") — we never auto-install.
3. **Neither** (stdio / standalone) — a clear :class:`ToolError` explaining
   how to enable it.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx
from fastmcp.exceptions import ToolError

from ..errors import ErrorCode, create_error_response
from ..tools.helpers import raise_tool_error
from .theme_guard import EngineCredential, addon_credential_from_options

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EngineTarget:
    """A resolved screenshot engine endpoint.

    ``addon_credential`` authenticates as the engine's user (extracted from
    the Puppet add-on's Supervisor options during discovery, so the theme
    guard needs no second round-trip and the raw secret-bearing options
    dict never leaves this module). It is ``None`` for an explicitly
    configured engine URL. The token inside never leaves the server process
    — it must not be logged or surfaced in responses.
    """

    url: str
    addon_credential: EngineCredential | None = None


ENGINE_PORT = 10000
# The Supervisor slug is ``<repo-hash>_puppet`` for balloob's Puppet add-on.
# ``str.endswith`` accepts a tuple, kept as one for easy future extension.
ENGINE_SLUG_SUFFIXES = ("_puppet",)
_PUPPET_OPTION_NAMES = {
    "access_token",
    "home_assistant_url",
    "keep_browser_open",
}

_REPO_URL = "https://github.com/balloob/home-assistant-addons"

# The single source of truth for the "configure the access token" instruction,
# reused across every engine-troubleshooting message here and in capture.py so
# a copy edit only has to happen once.
TOKEN_HINT = (
    "set the add-on's 'access_token' option to a Home Assistant long-lived "
    "access token (create one in Profile > Security, ideally for a dedicated "
    "low-privilege user) and (re)start it"
)

_INSTALL_HELP = (
    "Dashboard screenshot mode is enabled, but the Puppet screenshot engine "
    "add-on is not installed. On HA OS / Supervised: (1) add balloob's add-on "
    f"repository ({_REPO_URL}) under Settings > Add-ons > Add-on Store > "
    "Repositories, then install the 'Puppet' add-on; and (2) it REQUIRES a "
    f"token — {TOKEN_HINT}. Without a token the engine only serves a "
    "configuration-instructions page. On Docker / Container, run the Puppet "
    "image as a sidecar (with its access_token set) and point ha-mcp at it via "
    "HAMCP_DASHBOARD_SCREENSHOT_ENGINE_URL (e.g. http://puppet:10000)."
)

_NOT_STARTED_HELP = (
    "The Puppet screenshot engine add-on is installed but not started. Open "
    f"Settings > Add-ons > Puppet, {TOKEN_HINT} (enable 'Start on boot' to keep "
    "it available)."
)

_STDIO_HELP = (
    "Dashboard screenshot mode needs either HA OS / Supervised (add balloob's "
    "add-on repository and install the 'Puppet' add-on) or a Puppet sidecar "
    "reachable via HAMCP_DASHBOARD_SCREENSHOT_ENGINE_URL. This deployment "
    "(stdio / standalone) can host neither — see docs/beta.md."
)


def _select_started_verified_puppet(
    addon_infos: list[tuple[str, dict[str, Any]]],
) -> tuple[str, dict[str, Any]]:
    """Select one started add-on that exactly matches Puppet's safe surface."""
    verified: list[tuple[str, dict[str, Any]]] = []
    for slug, info in addon_infos:
        schema = info.get("schema")
        schema_names = {
            str(item.get("name"))
            for item in schema or []
            if isinstance(item, dict) and item.get("name") is not None
        }
        if info.get("name") == "Puppet" and schema_names >= _PUPPET_OPTION_NAMES:
            verified.append((slug, info))
    if not verified:
        raise_tool_error(
            create_error_response(
                ErrorCode.CONFIG_VALIDATION_FAILED,
                "Installed *_puppet add-ons did not match Puppet's expected schema.",
                context={"matched_slugs": [slug for slug, _ in addon_infos]},
                suggestions=[
                    "Verify the installed screenshot engine is balloob's Puppet add-on"
                ],
            )
        )
    started = [item for item in verified if item[1].get("state") == "started"]
    if not started:
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                "The schema-verified Puppet add-on is not started.",
                details=_NOT_STARTED_HELP,
                context={"matched_slugs": [slug for slug, _ in verified]},
            )
        )
    if len(started) > 1:
        raise_tool_error(
            create_error_response(
                ErrorCode.CONFIG_VALIDATION_FAILED,
                "Multiple started Puppet add-ons matched the expected schema; "
                "refusing an ambiguous target.",
                context={"matched_slugs": [slug for slug, _ in started]},
            )
        )
    return started[0]


def _supervisor_response_data(payload: Any, endpoint: str) -> dict[str, Any]:
    """Return a Supervisor response's object-shaped data payload."""
    if not isinstance(payload, dict):
        raise ValueError(f"Supervisor {endpoint} returned a non-object payload")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError(f"Supervisor {endpoint} returned invalid data")
    return data


def _supervisor_addon_listing(payload: Any) -> list[dict[str, Any]]:
    """Return the validated add-on objects from a Supervisor list response."""
    addons = _supervisor_response_data(payload, "/addons").get("addons")
    if not isinstance(addons, list) or not all(
        isinstance(addon, dict) for addon in addons
    ):
        raise ValueError("Supervisor /addons returned an invalid addons list")
    return addons


async def resolve_engine() -> EngineTarget:
    """Resolve the screenshot engine endpoint, or raise ToolError.

    See module docstring for the three-mode resolution order.
    """
    from ..config import get_global_settings

    explicit = (get_global_settings().dashboard_screenshot_engine_url or "").strip()
    if explicit:
        return EngineTarget(url=explicit.rstrip("/"))

    if os.environ.get("SUPERVISOR_TOKEN"):
        return await _discover_engine_via_supervisor()

    raise_tool_error(
        create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            "Dashboard screenshot mode is not available in this deployment.",
            details=_STDIO_HELP,
            suggestions=[
                "Use HA OS / Supervised and install the screenshot engine add-on",
                "Or run the engine as a sidecar and set "
                + "HAMCP_DASHBOARD_SCREENSHOT_ENGINE_URL",
            ],
        )
    )
    # raise_tool_error is typed -> NoReturn, but CodeQL cannot see that, so it
    # reports py/mixed-returns for the implicit None fall-through past it. Keep
    # this terminal statement to suppress that false positive (repo convention).
    raise AssertionError("unreachable: raise_tool_error always raises")


async def _discover_engine_via_supervisor() -> EngineTarget:
    """Find the Puppet add-on via the Supervisor and return its endpoint.

    Requires the ha-mcp add-on's ``manager`` role (already declared) for the
    read-only ``/addons`` + ``/addons/<slug>/info`` endpoints. Raises a
    ToolError with actionable guidance when the engine is missing or stopped.
    """
    from ..client.supervisor_client import make_supervisor_httpx_client

    try:
        async with make_supervisor_httpx_client(timeout=15.0, verify=True) as sup:
            listing = await sup.get("/addons")
            listing.raise_for_status()
            addons = _supervisor_addon_listing(listing.json())

            matches = [
                a
                for a in addons
                if str(a.get("slug", "")).endswith(ENGINE_SLUG_SUFFIXES)
            ]
            if not matches:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.RESOURCE_NOT_FOUND,
                        "The Puppet screenshot engine add-on is not installed.",
                        details=_INSTALL_HELP,
                    )
                )

            # The /addons list is only reliable for slug discovery (the
            # repo-hash prefix is not known ahead of time). Per-addon details
            # are authoritative on /addons/<slug>/info — the same source
            # ha_manage_addon trusts.
            addon_infos: list[tuple[str, dict[str, Any]]] = []
            for match in matches:
                slug = str(match["slug"])
                info = await sup.get(f"/addons/{slug}/info")
                info.raise_for_status()
                data = _supervisor_response_data(info.json(), f"/addons/{slug}/info")
                addon_infos.append((slug, data))
            slug, data = _select_started_verified_puppet(addon_infos)
            hostname = data.get("hostname") or data.get("ip_address")
            if not hostname:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        f"Screenshot engine add-on '{slug}' is started but "
                        "the Supervisor returned no hostname/ip_address.",
                        context={"slug": slug},
                    )
                )
            options = data.get("options")
            return EngineTarget(
                url=f"http://{hostname}:{ENGINE_PORT}",
                addon_credential=addon_credential_from_options(
                    options if isinstance(options, dict) else None
                ),
            )
    except ToolError:
        raise
    except (httpx.HTTPError, KeyError, ValueError) as e:
        logger.warning("Screenshot engine discovery via Supervisor failed: %s", e)
        raise_tool_error(
            create_error_response(
                ErrorCode.CONNECTION_FAILED,
                "Could not query the Supervisor to locate the Puppet "
                "screenshot engine add-on.",
                details=str(e),
                suggestions=[
                    "Verify the Puppet add-on is installed and started",
                    "Or set HAMCP_DASHBOARD_SCREENSHOT_ENGINE_URL explicitly",
                ],
            )
        )
