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

import asyncio
import json
import logging
import os
from typing import Any, NoReturn

import httpx
from fastmcp.exceptions import ToolError

from ..errors import ErrorCode, create_error_response
from ..tools.helpers import raise_tool_error

logger = logging.getLogger(__name__)

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
                + "HAMCP_DASHBOARD_SCREENSHOT_ENGINE_URL",
            ],
        )
    )
    raise AssertionError("unreachable: raise_tool_error always raises")


async def _discover_engine_url_via_supervisor() -> str:
    """Find the Puppet add-on via the Supervisor and return its internal URL.

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
            # ha_manage_addon trusts. Use the same name/schema/ambiguity checks
            # as the configuration path so a combined call cannot mutate one
            # add-on and render through another.
            addon_infos: list[tuple[str, dict[str, Any]]] = []
            for match in matches:
                slug = str(match["slug"])
                info = await sup.get(f"/addons/{slug}/info")
                info.raise_for_status()
                data = info.json().get("data", {})
                if isinstance(data, dict):
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
            return f"http://{hostname}:{ENGINE_PORT}"
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
    raise AssertionError("unreachable: all discovery branches return or raise")


def _raise_puppet_restart_failure(
    error: Exception, *, slug: str, settings_changed: bool
) -> NoReturn:
    """Preserve any already-applied setting when restart/verification fails."""
    try:
        restart_error = json.loads(str(error))
    except (json.JSONDecodeError, TypeError):
        restart_error = None
    if not isinstance(restart_error, dict):
        restart_error = create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            (
                "Puppet settings were applied, but restart verification failed."
                if settings_changed
                else "Puppet restart verification failed."
            ),
            details=str(error),
        )
    restart_error.update(
        {
            "slug": slug,
            "settings_changed": settings_changed,
            "restart_requested": True,
        }
    )
    raise_tool_error(restart_error)
    raise AssertionError("unreachable: raise_tool_error always raises")


async def _maybe_restart_puppet_addon(
    client: Any, *, slug: str, settings_changed: bool, restart: bool
) -> bool | None:
    """Restart only Puppet and verify Supervisor reports it started again."""
    if not restart:
        return None
    from ..tools.tools_addons import _supervisor_api_call

    try:
        await _supervisor_api_call(
            client,
            f"/addons/{slug}/restart",
            method="POST",
            timeout=120,
        )
        for _ in range(20):
            restarted_info = await _supervisor_api_call(client, f"/addons/{slug}/info")
            restarted_data = restarted_info.get("result", {})
            if (
                isinstance(restarted_data, dict)
                and restarted_data.get("state") == "started"
            ):
                return True
            await asyncio.sleep(0.5)
    except Exception as exc:
        _raise_puppet_restart_failure(exc, slug=slug, settings_changed=settings_changed)
    raise_tool_error(
        create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            "Puppet accepted the restart request but did not return to the "
            "started state in time.",
            context={
                "slug": slug,
                "settings_changed": settings_changed,
                "restart_requested": True,
            },
        )
    )
    raise AssertionError("unreachable: raise_tool_error always raises")


async def _select_configurable_puppet_addon(client: Any) -> tuple[str, dict[str, Any]]:
    """Select the same authoritative started Puppet that capture would prefer."""
    from ..tools.tools_addons import _supervisor_api_call

    listing = await _supervisor_api_call(client, "/addons")
    listing_result = listing.get("result")
    addons = (
        listing_result.get("addons", []) if isinstance(listing_result, dict) else []
    )
    matches = [
        addon
        for addon in addons
        if isinstance(addon, dict)
        and str(addon.get("slug", "")).endswith(ENGINE_SLUG_SUFFIXES)
    ]
    if not matches:
        raise_tool_error(
            create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND,
                "The Puppet screenshot engine add-on is not installed.",
                details=_INSTALL_HELP,
            )
        )

    addon_infos: list[tuple[str, dict[str, Any]]] = []
    for match in matches:
        slug = str(match["slug"])
        info_response = await _supervisor_api_call(client, f"/addons/{slug}/info")
        info = info_response.get("result", {})
        if not isinstance(info, dict):
            continue
        addon_infos.append((slug, info))
    return _select_started_verified_puppet(addon_infos)


async def configure_puppet_addon(
    client: Any,
    *,
    keep_browser_open: bool | None,
    restart: bool,
) -> dict[str, Any]:
    """Update the installed Puppet add-on without exposing a caller slug.

    This intentionally reuses the same Supervisor API transport and
    read-merge-write contract as ``ha_manage_addon`` while hard-binding every
    endpoint to a discovered, schema-verified Puppet add-on. Secrets and the
    Home Assistant target URL are never accepted or returned here.
    """
    from ..config import get_global_settings
    from ..tools.tools_addons import _supervisor_api_call

    explicit_engine = (
        get_global_settings().dashboard_screenshot_engine_url or ""
    ).strip()
    if explicit_engine:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_FAILED,
                "Puppet add-on settings can be managed only when the screenshot "
                "engine uses Supervisor auto-discovery.",
                context={"explicit_engine_configured": True},
            )
        )
    if not os.environ.get("SUPERVISOR_TOKEN"):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_FAILED,
                "Puppet add-on settings require HA OS / Supervised auto-discovery.",
                context={"supervisor_available": False},
            )
        )
    slug, info = await _select_configurable_puppet_addon(client)
    options = info.get("options")
    if not isinstance(options, dict):
        raise_tool_error(
            create_error_response(
                ErrorCode.CONFIG_VALIDATION_FAILED,
                "Supervisor did not return Puppet's complete options object; refusing "
                "a full-replacement settings write.",
                context={"slug": slug},
            )
        )
    assert isinstance(options, dict)

    current_options = dict(options)
    current_keep_open = current_options.get("keep_browser_open")
    changed = keep_browser_open is not None and keep_browser_open != current_keep_open
    if changed:
        missing_options = sorted(_PUPPET_OPTION_NAMES - current_options.keys())
        if missing_options:
            raise_tool_error(
                create_error_response(
                    ErrorCode.CONFIG_VALIDATION_FAILED,
                    "Supervisor did not return Puppet's complete options object; "
                    "refusing a full-replacement settings write.",
                    context={"slug": slug, "missing_options": missing_options},
                )
            )
        invalid_option_types = sorted(
            name
            for name, expected_type in {
                "access_token": str,
                "home_assistant_url": str,
                "keep_browser_open": bool,
            }.items()
            if not isinstance(current_options[name], expected_type)
        )
        if invalid_option_types:
            raise_tool_error(
                create_error_response(
                    ErrorCode.CONFIG_VALIDATION_FAILED,
                    "Supervisor returned unexpected Puppet option types; refusing "
                    "a full-replacement settings write.",
                    context={
                        "slug": slug,
                        "invalid_option_types": invalid_option_types,
                    },
                )
            )
        merged_options = {**current_options, "keep_browser_open": keep_browser_open}
        await _supervisor_api_call(
            client,
            f"/addons/{slug}/options",
            method="POST",
            data={"options": merged_options},
        )
    restart_verified = await _maybe_restart_puppet_addon(
        client, slug=slug, settings_changed=changed, restart=restart
    )

    return {
        "slug": slug,
        "keep_browser_open": (
            keep_browser_open if keep_browser_open is not None else current_keep_open
        ),
        "settings_changed": changed,
        "restart_requested": restart,
        "restart_verified": restart_verified,
        "status": (
            "restarted" if restart else ("pending_restart" if changed else "unchanged")
        ),
    }
