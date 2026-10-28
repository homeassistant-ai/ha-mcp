"""Locate a supported dashboard screenshot engine for this deployment.

The supported engines are balloob's **Puppet** app and the **WPE Screenshot
Engine** app. ha-mcp does not vendor either engine — the user installs them.

Three deployment modes, resolved lazily on first tool use (never at
startup, never silently installed):

1. **Explicit** — ``HAMCP_DASHBOARD_SCREENSHOT_ENGINE_URL`` is set. Used
   verbatim. This is the Docker / Container path (a docker-compose
   sidecar), and also lets HA OS users override auto-discovery.
2. **HA OS / Supervised** — ``SUPERVISOR_TOKEN`` is present. A supported app
   (slug ``*_puppet`` or ``*_wpe_screenshot_engine``) is verified through the
   Supervisor REST API and reached on the internal network by its hostname.
   The user must install and start the app themselves — we never auto-install.
3. **Neither** (stdio / standalone) — a clear :class:`ToolError` explaining
   how to enable it.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from ha_mcp._vendor.fastmcp.exceptions import ToolError

from ..errors import ErrorCode, create_error_response
from ..tools.helpers import raise_tool_error
from .theme_guard import EngineCredential, addon_credential_from_options

logger = logging.getLogger(__name__)

EngineKind = Literal["puppet", "wpe"]


@dataclass(frozen=True, slots=True)
class EngineTarget:
    """A resolved screenshot engine endpoint.

    ``addon_credential`` authenticates as Puppet's user (extracted from its
    Supervisor options during discovery, so the theme guard needs no second
    round-trip and the raw secret-bearing options dict never leaves this
    module). ``engine_kind`` and ``engine_version`` are populated only when
    Supervisor verifies the selected app. An explicit URL does not prove which
    engine or version it targets. WPE temporarily applies frontend preferences
    and does not need the guard credential. An explicit URL may still carry
    Puppet's credential when Supervisor discovery finds it. The token never
    leaves the server process — it must not be logged or surfaced in responses.
    """

    url: str
    engine_kind: EngineKind | None = None
    engine_version: str | None = None
    addon_credential: EngineCredential | None = None


@dataclass(frozen=True, slots=True)
class _EngineProfile:
    """Supervisor identity and network details for a supported renderer."""

    name: str
    kind: EngineKind
    slug_suffix: str
    port: int
    required_schema_options: frozenset[str]
    has_persistent_theme_override: bool = False


_PUPPET = _EngineProfile(
    name="Puppet",
    kind="puppet",
    slug_suffix="_puppet",
    port=10000,
    required_schema_options=frozenset(
        {"access_token", "home_assistant_url", "keep_browser_open"}
    ),
    has_persistent_theme_override=True,
)
_WPE = _EngineProfile(
    name="WPE Screenshot Engine",
    kind="wpe",
    slug_suffix="_wpe_screenshot_engine",
    port=8080,
    required_schema_options=frozenset(
        {"home_assistant_url", "home_assistant_token", "dashboard_path"}
    ),
)
_SUPPORTED_ENGINES = (_PUPPET, _WPE)

_PUPPET_REPO_URL = "https://github.com/balloob/home-assistant-addons"
_WPE_REPO_URL = "https://github.com/Nischay-Pro/ha-wpe-screenshot-engine"

# The single source of truth for the "configure the engine token" instruction,
# reused across every engine-troubleshooting message here and in capture.py so
# a copy edit only has to happen once.
TOKEN_HINT = (
    "set the engine's token option ('access_token' for "
    "Puppet or 'home_assistant_token' for WPE Screenshot Engine) to a "
    "Home Assistant long-lived access token (create one in Profile > Security, "
    "ideally for a dedicated low-privilege user) and (re)start it"
)

_INSTALL_HELP = (
    "Dashboard screenshot mode is enabled, but no supported screenshot "
    "engine app is installed. On HA OS / Supervised, add either balloob's "
    f"repository ({_PUPPET_REPO_URL}) or WPE Screenshot Engine's repository "
    f"({_WPE_REPO_URL}) under Settings > Apps > Install app > ⋮ > Repositories "
    "(Settings > Add-ons > Add-on store before Home Assistant 2026.2), then "
    "install and start Puppet or WPE Screenshot Engine. "
    f"{TOKEN_HINT}. On Docker / Container, run "
    "one of these engines as a sidecar and set "
    "HAMCP_DASHBOARD_SCREENSHOT_ENGINE_URL to its reachable URL."
)

_NOT_STARTED_HELP = (
    "The supported screenshot engine app is installed but not started. Open "
    "Settings > Apps > the selected engine (Settings > Add-ons before Home "
    f"Assistant 2026.2), {TOKEN_HINT}. "
    "Enable 'Start on boot' to keep it available."
)

_STDIO_HELP = (
    "Dashboard screenshot mode needs either HA OS / Supervised with a "
    "supported screenshot engine app installed, or a screenshot-engine "
    "sidecar reachable via HAMCP_DASHBOARD_SCREENSHOT_ENGINE_URL. This "
    "deployment (stdio / standalone) can host neither — see docs/beta.md."
)


def _verified_engine_profile(
    slug: str, info: dict[str, Any], profiles: tuple[_EngineProfile, ...]
) -> _EngineProfile | None:
    """Return the profile whose slug, app name and schema all match."""
    schema = info.get("schema")
    schema_names = {
        str(item.get("name"))
        for item in schema or []
        if isinstance(item, dict) and item.get("name") is not None
    }
    for profile in profiles:
        if (
            slug.endswith(profile.slug_suffix)
            and info.get("name") == profile.name
            and profile.required_schema_options <= schema_names
        ):
            return profile
    return None


def _select_started_verified_engine(
    addon_infos: list[tuple[str, dict[str, Any]]],
    profiles: tuple[_EngineProfile, ...] = _SUPPORTED_ENGINES,
) -> tuple[str, dict[str, Any], _EngineProfile]:
    """Select one started app that exactly matches a supported engine."""
    verified: list[tuple[str, dict[str, Any], _EngineProfile]] = []
    for slug, info in addon_infos:
        profile = _verified_engine_profile(slug, info, profiles)
        if profile is not None:
            verified.append((slug, info, profile))
    if not verified:
        raise_tool_error(
            create_error_response(
                ErrorCode.CONFIG_VALIDATION_FAILED,
                "Installed screenshot engine apps did not match a supported "
                "engine's expected identity and schema.",
                context={"matched_slugs": [slug for slug, _ in addon_infos]},
                suggestions=[
                    "Verify the app is Puppet or WPE Screenshot Engine and "
                    "matches its repository configuration"
                ],
            )
        )
    started = [item for item in verified if item[1].get("state") == "started"]
    if not started:
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                "The schema-verified screenshot engine app is not started.",
                details=_NOT_STARTED_HELP,
                context={"matched_slugs": [slug for slug, _, _ in verified]},
            )
        )
    if len(started) > 1:
        raise_tool_error(
            create_error_response(
                ErrorCode.CONFIG_VALIDATION_FAILED,
                "Multiple started screenshot engine apps matched supported "
                "schemas; "
                "refusing an ambiguous target.",
                context={"matched_slugs": [slug for slug, _, _ in started]},
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
        return EngineTarget(
            url=explicit.rstrip("/"),
            addon_credential=await _addon_credential_best_effort(),
        )

    if os.environ.get("SUPERVISOR_TOKEN"):
        return await _discover_engine_via_supervisor()

    raise_tool_error(
        create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            "Dashboard screenshot mode is not available in this deployment.",
            details=_STDIO_HELP,
            suggestions=[
                "Use HA OS / Supervised and install the screenshot engine app",
                "Or run the engine as a sidecar and set "
                + "HAMCP_DASHBOARD_SCREENSHOT_ENGINE_URL",
            ],
        )
    )
    # raise_tool_error is typed -> NoReturn, but CodeQL cannot see that, so it
    # reports py/mixed-returns for the implicit None fall-through past it. Keep
    # this terminal statement to suppress that false positive (repo convention).
    raise AssertionError("unreachable: raise_tool_error always raises")


async def _addon_credential_best_effort() -> EngineCredential | None:
    """Best-effort theme-guard credential for an explicitly configured URL.

    HA OS / Supervised users may set ``HAMCP_DASHBOARD_SCREENSHOT_ENGINE_URL``
    to override auto-discovery while still running Puppet; without its token
    the theme guard would go inactive exactly where the app credential exists
    (``_client_credential`` refuses Supervisor-proxy auth).
    Any failure — no Supervisor, no verified add-on, not started — simply
    means "no credential": the explicit URL may point at a non-add-on
    engine, so this must never raise. If the explicit URL targets a
    different engine than the discovered add-on, the guard still only ever
    writes back a value that the discovered add-on's own user changed
    mid-capture, which only that add-on's renders would do — a safe no-op.
    """
    if not os.environ.get("SUPERVISOR_TOKEN"):
        return None
    try:
        # The theme guard is specific to Puppet, whose current renderer writes
        # theme changes to the Home Assistant user profile. WPE applies these
        # options temporarily and suppresses the corresponding user-data write.
        return (
            await _discover_engine_via_supervisor(preferred_engine="Puppet")
        ).addon_credential
    except Exception:
        logger.debug(
            "No Supervisor-discoverable engine credential for the explicit "
            "engine URL; the theme guard stays inactive.",
            exc_info=True,
        )
        return None


async def _discover_engine_via_supervisor(
    *, preferred_engine: str | None = None
) -> EngineTarget:
    """Find one supported app through the Supervisor and return its endpoint.

    Requires the ha-mcp add-on's ``manager`` role (already declared) for the
    read-only ``/addons`` + ``/addons/<slug>/info`` endpoints. Raises a
    ToolError with actionable guidance when no matching engine is available.
    ``preferred_engine`` is used only for the best-effort Puppet theme guard
    when the operator configured an explicit URL.
    """
    from ..client.supervisor_client import make_supervisor_httpx_client

    try:
        async with make_supervisor_httpx_client(timeout=15.0, verify=True) as sup:
            listing = await sup.get("/addons")
            listing.raise_for_status()
            addons = _supervisor_addon_listing(listing.json())

            profiles = tuple(
                profile
                for profile in _SUPPORTED_ENGINES
                if preferred_engine is None or profile.name == preferred_engine
            )
            matches = [
                a
                for a in addons
                if str(a.get("slug", "")).endswith(
                    tuple(profile.slug_suffix for profile in profiles)
                )
            ]
            if not matches:
                engine_names = (
                    [preferred_engine]
                    if preferred_engine
                    else [profile.name for profile in _SUPPORTED_ENGINES]
                )
                raise_tool_error(
                    create_error_response(
                        ErrorCode.RESOURCE_NOT_FOUND,
                        "A supported screenshot engine app is not installed.",
                        context={"supported_engines": engine_names},
                        details=_INSTALL_HELP,
                    )
                )

            # The /addons list is only reliable for slug discovery (the
            # repo-hash prefix is not known ahead of time). Per-addon details
            # are authoritative on /addons/<slug>/info — the same source
            # ha_manage_app trusts.
            addon_infos: list[tuple[str, dict[str, Any]]] = []
            for match in matches:
                slug = str(match["slug"])
                info = await sup.get(f"/addons/{slug}/info")
                info.raise_for_status()
                data = _supervisor_response_data(info.json(), f"/addons/{slug}/info")
                addon_infos.append((slug, data))
            slug, data, profile = _select_started_verified_engine(addon_infos, profiles)
            hostname = data.get("hostname") or data.get("ip_address")
            if not hostname:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        f"Screenshot engine app '{slug}' is started but "
                        "the Supervisor returned no hostname/ip_address.",
                        context={"slug": slug},
                    )
                )
            options = data.get("options")
            app_version = data.get("version")
            return EngineTarget(
                url=f"http://{hostname}:{profile.port}",
                engine_kind=profile.kind,
                engine_version=(
                    app_version.strip()
                    if isinstance(app_version, str) and app_version.strip()
                    else None
                ),
                addon_credential=(
                    addon_credential_from_options(
                        options if isinstance(options, dict) else None
                    )
                    if profile.has_persistent_theme_override
                    else None
                ),
            )
    except ToolError:
        raise
    except (httpx.HTTPError, KeyError, ValueError) as e:
        logger.warning("Screenshot engine discovery via Supervisor failed: %s", e)
        raise_tool_error(
            create_error_response(
                ErrorCode.CONNECTION_FAILED,
                "Could not query the Supervisor to locate the screenshot engine app.",
                details=str(e),
                suggestions=[
                    "Verify the screenshot engine app is installed and started",
                    "Or set HAMCP_DASHBOARD_SCREENSHOT_ENGINE_URL explicitly",
                ],
            )
        )
