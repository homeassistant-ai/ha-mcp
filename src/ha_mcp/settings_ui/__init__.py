"""Web-based settings UI for tool visibility configuration.

Serves a self-contained HTML page at /settings that lets users enable,
disable, and pin MCP tools. Changes apply immediately without server
restart. Persists to a JSON config file alongside the MCP server data.

Works across all installation methods (add-on, Docker, standalone).
"""

from __future__ import annotations

import functools
import html
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from .._version import is_running_in_addon
from ..transforms import DEFAULT_PINNED_TOOLS
from ..utils.data_paths import get_data_dir
from ._handlers_advanced import build_advanced_handlers
from ._handlers_backups import build_backups_handlers
from ._handlers_fs import build_fs_handlers
from ._handlers_server import (
    _PROCESS_INSTANCE_ID,
    _PROCESS_STARTED_AT,
    build_server_handlers,
)
from ._handlers_theme import build_theme_handlers
from ._handlers_tools import build_tools_handlers
from ._persistence import (
    _atomic_write_json,
    _get_backup_settings_override_path,
    _get_config_path,
    _get_override_file_lock,
    _load_backup_settings_override,
    _save_backup_settings_override,
    dump_tool_metadata_cache,
    effective_tool_config,
    env_pinned_tools,
    load_tool_config,
    load_tool_metadata_cache,
    save_tool_config,
)
from ._supervisor import (
    _BACKGROUND_RESTART_TASKS,
    _SUPERVISOR_SELF_RESTART_FLUSH_DELAY_S,
    _schedule_supervisor_self_restart,
    _supervisor_fetch_current_options,
    _supervisor_merge_and_post_options,
    _SupervisorOptionsError,
)
from ._theme import _load_theme_prefs, _sanitize_theme_prefs
from ._tools_meta import (
    FEATURE_GATED_TOOLS,
    MANDATORY_TOOLS,
    TRANSFORM_GENERATED_TOOLS,
    _get_tool_metadata,
    apply_tool_visibility,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from ..config import Settings
    from ..server import HomeAssistantSmartMCPServer


logger = logging.getLogger(__name__)

# The settings-UI client script lives in settings.js (a real file for
# editor/JS tooling). It is a template with two sentinel tokens for the
# Python-injected constant lists; substitute them with the same values the
# inline literal used so the rendered HTML is byte-identical. Injected
# inline (not served) -- the serving model is unchanged.
#
# This is a module-import-time file read: importing settings_ui (done by
# server.py / __main__.py / the sidecar) now depends on settings.js being
# present. If packaging drops it, fail with a packaging-specific ImportError
# rather than a bare FileNotFoundError so the cause is obvious.
_SETTINGS_JS_PATH = Path(__file__).parent / "settings.js"
try:
    _settings_js_template = _SETTINGS_JS_PATH.read_text(encoding="utf-8")
except OSError as exc:  # pragma: no cover - packaging guard
    raise ImportError(
        f"settings.js missing at {_SETTINGS_JS_PATH}. It must ship in "
        "package-data (wheel), MANIFEST.in (sdist), and the PyInstaller datas "
        "(binary) -- this is a packaging bug, not a usage error."
    ) from exc
# str.replace() silently no-ops on an absent token, and a *renamed* sentinel
# (e.g. PINNED_DEFAULTS) slips past both the "__HA_MCP_" not-in test and the
# esbuild/jsdom JS harness (tests/js/harness.mjs) -- `const DEFAULT_PINNED =
# PINNED_DEFAULTS;` is valid JS (only a runtime ReferenceError), so a drifted
# settings.js would ship a broken page green. Assert both sentinels are present
# before substituting.
for _sentinel in ("__HA_MCP_DEFAULT_PINNED__", "__HA_MCP_MANDATORY__"):
    if _sentinel not in _settings_js_template:
        raise ImportError(
            f"settings.js is out of sync: sentinel {_sentinel} not found. "
            "The Python injection and settings.js have drifted."
        )
# sorted(), not list(): DEFAULT_PINNED_TOOLS / MANDATORY_TOOLS are sets, so
# json.dumps(list(...)) is per-process-ordered -- the only reason proving the
# original extraction byte-identical needed PYTHONHASHSEED pinned. sorted()
# makes the two injected arrays deterministic across processes.
_SETTINGS_JS = _settings_js_template.replace(
    "__HA_MCP_DEFAULT_PINNED__", json.dumps(sorted(DEFAULT_PINNED_TOOLS))
).replace("__HA_MCP_MANDATORY__", json.dumps(sorted(MANDATORY_TOOLS)))


# The settings-UI CSS lives in settings.css, extracted the same way as
# settings.js. Unlike the JS it has no Python injection points -- a plain
# read, no token substitution -- and is injected inline between the same
# <style>/</style> tags so the served page stays byte-identical. It carries
# the same import-time packaging dependency as settings.js, so the same
# OSError -> ImportError packaging guard applies.
_SETTINGS_CSS_PATH = Path(__file__).parent / "settings.css"
try:
    _SETTINGS_CSS = _SETTINGS_CSS_PATH.read_text(encoding="utf-8")
except OSError as exc:  # pragma: no cover - packaging guard
    raise ImportError(
        f"settings.css missing at {_SETTINGS_CSS_PATH}. It must ship in "
        "package-data (wheel), MANIFEST.in (sdist), and the PyInstaller datas "
        "(binary) -- this is a packaging bug, not a usage error."
    ) from exc


# The settings page HTML lives in settings.html, extracted the same way as
# settings.js / settings.css (a real file for editor/HTML tooling). It carries
# three substitution markers — two filled once at import, one per request:
#   __HA_MCP_CSS__         -> settings.css contents (inside <style>)
#   __HA_MCP_JS__          -> settings.js contents (inside <script>)
#   __HA_MCP_THEME_PREFS__ -> per-request server-seeded theme prefs JSON,
#                             substituted in _render_settings_html()
# Same import-time packaging dependency as settings.js/css (wheel package-data,
# MANIFEST.in, PyInstaller datas) and the same OSError guard -- but this loader
# raises RuntimeError, not the ImportError that settings.js/css raise.
_SETTINGS_HTML_PATH = Path(__file__).parent / "settings.html"
try:
    _settings_html_template = _SETTINGS_HTML_PATH.read_text(encoding="utf-8")
except OSError as exc:  # pragma: no cover - packaging guard
    raise RuntimeError(
        f"settings.html missing at {_SETTINGS_HTML_PATH}. It must ship in "
        "package-data (wheel), MANIFEST.in (sdist), and the PyInstaller datas "
        "(binary) -- this is a packaging bug, not a usage error."
    ) from exc

# Fail fast if a marker was renamed in settings.html but not here (or vice
# versa) — str.replace() silently no-ops on an absent token, which would ship a
# page missing its CSS, JS, or server-seeded theme prefs. Assert all three
# markers are present.
for _sentinel in ("__HA_MCP_CSS__", "__HA_MCP_JS__", "__HA_MCP_THEME_PREFS__"):
    if _sentinel not in _settings_html_template:
        raise RuntimeError(
            f"settings.html is out of sync: sentinel {_sentinel} not found. "
            "The Python injection and settings.html have drifted."
        )

# CSS and JS are injected once at import; __HA_MCP_THEME_PREFS__ remains in the
# string and is substituted per-request in _render_settings_html().
_SETTINGS_HTML = _settings_html_template.replace(
    "__HA_MCP_CSS__", _SETTINGS_CSS
).replace("__HA_MCP_JS__", _SETTINGS_JS)


def _build_stub_policy_handlers(*, data_dir: Path) -> dict[str, Any]:
    """Sidecar variant of the tool security policies handlers.

    Serves policy config GET/PUT (the on-disk policy file is shared with
    the main server), but returns 503 for pending/approve/deny — those
    routes touch the in-memory ``ApprovalQueue`` which only exists in
    the main server process.
    """
    from pydantic import ValidationError

    from ..policy.model import Policy
    from ..policy.persistence import load_policy, save_policy

    async def get_config(_: Request) -> JSONResponse:
        try:
            return JSONResponse(load_policy(data_dir).model_dump(mode="json"))
        except ValueError as e:
            # Mirror the main-server handler: surface corruption rather
            # than crash the sidecar tab on a 500.
            return JSONResponse(
                {"error": str(e), "policy_file_corrupt": True},
                status_code=500,
            )

    async def put_config(request: Request) -> JSONResponse:
        try:
            new_policy = Policy.model_validate(await request.json())
        except (ValidationError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        # Mirror main-server optimistic concurrency: reject if on-disk
        # version moved between this caller's GET and PUT.
        current = load_policy(data_dir)
        if new_policy.version != current.version:
            return JSONResponse(
                {
                    "error": "Policy version mismatch. Reload before saving.",
                    "current_version": current.version,
                    "current_policy": current.model_dump(mode="json"),
                },
                status_code=409,
            )
        save_policy(data_dir, new_policy)
        return JSONResponse({"saved": True, "version": new_policy.version + 1})

    async def unavailable(_: Request) -> JSONResponse:
        # 503 fires in two distinct situations:
        #   1. The Tool Security Policies feature is turned off in
        #      addon config — the middleware never registered, so no
        #      approval queue exists.
        #   2. The settings UI is running via the stdio sidecar — the
        #      in-memory queue lives in the main server process which
        #      isn't reachable from the sidecar.
        # Either way, point users at the addon log for the real reason
        # (a startup ImportError on the policy package surfaces here as
        # the same 503 with a "ModuleNotFoundError" in the log).
        return JSONResponse(
            {
                "error": (
                    "Tool security policies live approvals are not active. "
                    "Either the feature is turned off in App (add-on) config, the "
                    "settings UI is running in stdio-sidecar mode, or the "
                    "policy package failed to import at startup. Check the "
                    "App (add-on) log for ImportError / RuntimeError details if you "
                    "expected gating to be on."
                )
            },
            status_code=503,
        )

    return {
        "policy_get_config": get_config,
        "policy_put_config": put_config,
        "policy_get_pending": unavailable,
        "policy_post_approve": unavailable,
        "policy_post_deny": unavailable,
        "policy_get_tool_schema": unavailable,
        "policy_get_value_source": unavailable,
    }


def _build_visibility_handlers(*, data_dir: Path) -> dict[str, Any]:
    """Settings-UI handlers for the entity visibility filter config.

    Serves the on-disk ``entity_visibility.json`` GET/PUT with the same
    optimistic-concurrency (version) guard as the tool policy handlers. Pure
    file I/O, so it is always available (no approval-queue dependency).
    """
    from pydantic import ValidationError

    from ..visibility.model import VisibilityConfig
    from ..visibility.persistence import (
        load_visibility_config,
        save_visibility_config,
    )

    async def get_config(_: Request) -> JSONResponse:
        try:
            return JSONResponse(
                load_visibility_config(data_dir).model_dump(mode="json")
            )
        except ValueError as e:
            # Surface corruption rather than crash the tab on a 500.
            return JSONResponse(
                {"error": str(e), "visibility_file_corrupt": True},
                status_code=500,
            )

    async def put_config(request: Request) -> JSONResponse:
        try:
            new_config = VisibilityConfig.model_validate(await request.json())
        except (ValidationError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        # Optimistic concurrency: reject if the on-disk version moved between
        # this caller's GET and PUT.
        current = load_visibility_config(data_dir)
        if new_config.version != current.version:
            return JSONResponse(
                {
                    "error": (
                        "Visibility config version mismatch. Reload before saving."
                    ),
                    "current_version": current.version,
                    "current_config": current.model_dump(mode="json"),
                },
                status_code=409,
            )
        save_visibility_config(data_dir, new_config)
        return JSONResponse({"saved": True, "version": new_config.version + 1})

    return {
        "visibility_get_config": get_config,
        "visibility_put_config": put_config,
    }


def _render_settings_html() -> str:
    """Substitute the persisted theme prefs into the served settings page.

    The ``server-prefs`` head script carries a ``data-prefs`` attribute
    with a placeholder token; request-time substitution keeps
    ``_SETTINGS_HTML`` a static import-time constant (and keeps the script
    body itself parseable at rest for the script-surface tests). Values
    are sanitized enums / vetted hex colors and the JSON is HTML-escaped,
    so the attribute cannot break out of its quoting context.
    """
    payload = html.escape(
        json.dumps(_load_theme_prefs(), separators=(",", ":")), quote=True
    )
    rendered = _SETTINGS_HTML.replace("__HA_MCP_THEME_PREFS__", payload, 1)
    if "__HA_MCP_THEME_PREFS__" in rendered:
        # str.replace silently no-ops when the placeholder vanishes in a
        # refactor; the unit contract test catches that in CI, this line
        # catches it loudly in a live deployment instead of quietly
        # serving a page without server-side prefs.
        logger.error(
            "server-prefs placeholder was not substituted; theme prefs "
            "will not survive fresh origins"
        )
    return rendered


def build_settings_handlers(
    server: HomeAssistantSmartMCPServer | None,
    *,
    is_sidecar: bool = False,
) -> dict[str, Any]:
    """Construct the settings UI route handlers.

    When ``server`` is provided (HTTP modes), the tools list and restart
    handler use the live FastMCP server / Supervisor client. When
    ``server`` is ``None`` (stdio sidecar process, which has no live MCP
    server), the tools list is read from the on-disk metadata cache and
    the restart handler returns 400 (the sidecar is not an add-on).

    ``is_sidecar`` forces the ``settings_info`` handler to report
    ``is_addon=False`` regardless of the inherited ``SUPERVISOR_TOKEN``
    env var. The sidecar process inherits parent env unchanged
    (``subprocess.Popen`` with default ``env=None``), so if the parent
    stdio process happens to run under Supervisor (e.g. an interactive
    debug shell inside the add-on container) the served HTML would
    otherwise show the "Restart Add-on" button that POSTs to a route
    the sidecar doesn't expose, surfacing as a broken UI. The sidecar
    is *by construction* not the add-on entrypoint — pin the flag
    accordingly.

    Returns a dict mapping handler names to async Starlette handlers.
    Both ``register_settings_routes`` (FastMCP mounting) and the stdio
    sidecar's standalone Starlette app consume the same set of handlers
    so the served page is identical regardless of transport.
    """

    async def _root_page(_: Request) -> HTMLResponse:
        return HTMLResponse(_render_settings_html())

    async def _settings_page(_: Request) -> HTMLResponse:
        return HTMLResponse(_render_settings_html())

    handlers: dict[str, Any] = {
        "root_page": _root_page,
        "settings_page": _settings_page,
    }

    # Tool security policies. The main server attaches an
    # ApprovalQueue to the server object once PolicyMiddleware is wired
    # in. Only the main server can serve the live pending/approve/deny
    # endpoints because the queue is in-memory; the sidecar (or a main
    # server without the queue attribute yet) falls back to stub handlers
    # that serve config GET/PUT and return 503 for live approval routes.
    approval_queue = (
        getattr(server, "approval_queue", None) if server is not None else None
    )
    if not is_sidecar and approval_queue is not None:
        from ..policy.handlers import build_policy_handlers

        handlers.update(
            build_policy_handlers(
                data_dir=get_data_dir(),
                queue=approval_queue,
                server=server,
            )
        )
    else:
        handlers.update(_build_stub_policy_handlers(data_dir=get_data_dir()))

    handlers.update(_build_visibility_handlers(data_dir=get_data_dir()))
    handlers.update(build_theme_handlers())
    handlers.update(build_fs_handlers(server))
    handlers.update(build_tools_handlers(server))
    handlers.update(build_backups_handlers(server))
    handlers.update(build_server_handlers(server, is_sidecar=is_sidecar))
    handlers.update(build_advanced_handlers(server))

    return handlers


# Home Assistant proxies every ingress request ("Open Web UI") from the
# Supervisor's fixed network address. Per the add-on ingress contract
# (https://developers.home-assistant.io/docs/add-ons/presentation/#ingress —
# "Only connections from 172.30.32.2 must be allowed") the app must reject
# every other source. This holds under host_network too: ingress proxies to
# http://{app.ip_address}:{ingress_port}/, and for a host-network add-on
# app.ip_address is the hassio bridge gateway 172.30.32.1 — the DESTINATION the
# Supervisor dials (supervisor/docker/app.py ip_address(): host_network ->
# network.gateway). The Supervisor opens that connection from its own container
# address 172.30.32.2, so the transport peer the add-on sees is 172.30.32.2 for
# genuine ingress and some other address (a LAN host, the cloudflared tunnel at
# 172.30.33.x, another add-on) for a direct port-9583 hit. Verified live via
# netstat during an "Open Web UI" click.
SUPERVISOR_INGRESS_IP = "172.30.32.2"

# A settings-UI route handler: async (Request) -> Response.
_SettingsRoute = Callable[[Request], Awaitable[Response]]


def _ingress_only(handler: _SettingsRoute) -> _SettingsRoute:
    """Wrap a root-mounted add-on route so only HA ingress can reach it.

    Add-on root routes carry no MCP secret, so without this guard a direct
    caller on the published port — a LAN peer, a reverse proxy / tunnel
    forwarding the bare root, or a CSRF POST from a LAN browser — could
    rewrite tool config, flip the tool-security-policy, or restart the
    add-on with no authentication. We gate on the *transport* peer
    (``request.client.host``), never ``X-Forwarded-For`` (which a caller can
    forge). The same handlers stay reachable under ``secret_prefix``, where
    the MCP secret path is the auth for direct/remote access.
    """

    @functools.wraps(handler)
    async def _guarded(request: Request) -> Response:
        peer = request.client.host if request.client else None
        if peer != SUPERVISOR_INGRESS_IP:
            logger.warning(
                "Blocked non-ingress request to add-on root route %s from "
                "peer %r (only the Supervisor at %s may reach root routes; "
                "use the MCP secret path for direct/remote access).",
                request.url.path,
                peer,
                SUPERVISOR_INGRESS_IP,
            )
            return JSONResponse(
                {
                    "error": (
                        "This endpoint is only reachable through Home "
                        "Assistant ingress. For direct or remote access, use "
                        "the settings UI under your MCP secret path."
                    )
                },
                status_code=403,
            )
        return await handler(request)

    return _guarded


# Mount prefix the settings UI is served under in long-lived HTTP transports
# (Docker / standalone ha-mcp-web / OAuth / the add-on's secret-path mount).
# Recorded by register_settings_routes so ha_get_overview can point users at
# the settings page in modes that have no stdio sidecar URL file to surface
# (issue #1458). Stays None in pure stdio mode, where the sidecar writes
# ~/.ha-mcp/ui.url instead.
_http_settings_prefix: str | None = None


def get_http_settings_prefix() -> str | None:
    """Return the settings-UI mount prefix for HTTP transports, or None.

    Set by :func:`register_settings_routes` when the page is mounted on a
    long-lived HTTP server. ``ha_get_overview`` reads it to hint at the
    settings page when there is no stdio sidecar URL to hand the user.
    """
    return _http_settings_prefix


def register_settings_routes(
    mcp: FastMCP,
    server: HomeAssistantSmartMCPServer,
    secret_path: str = "",
) -> None:
    """Register the settings UI HTTP routes on the FastMCP Starlette app.

    The routes are mounted under ``secret_path`` so HTTP clients (Docker
    / standalone) need the same secret to reach the UI as they do to
    reach the MCP endpoint itself — there's no native auth on FastMCP
    custom routes (they bypass ``RequireAuthMiddleware``), so this
    matches the auth-by-obscurity model the rest of the server uses for
    those modes. In add-on mode (``SUPERVISOR_TOKEN`` set) the routes
    are *also* mounted at root so HA ingress can proxy to ``localhost:9583/``
    and serve the "Open Web UI" button. Stdio transports use a separate
    side-process sidecar instead — see :mod:`ha_mcp.stdio_settings_sidecar`.

    Args:
        mcp: The FastMCP instance to register routes on.
        server: The HomeAssistantSmartMCPServer wrapping ``mcp``.
        secret_path: The MCP secret path (e.g. ``/private_xxx`` or
            ``/mcp``). Required for non-add-on HTTP modes; if empty in
            non-add-on mode, the function logs a warning and registers
            nothing rather than expose the routes publicly.
    """
    handlers = build_settings_handlers(server)
    secret_prefix = secret_path.rstrip("/") if secret_path else ""
    is_addon = is_running_in_addon()

    if not is_addon and not secret_prefix:
        logger.warning(
            "register_settings_routes: not in add-on mode and no secret_path "
            "provided — settings UI HTTP routes not registered (would otherwise "
            "be publicly reachable). Pass MCP_SECRET_PATH or run as add-on."
        )
        return

    # Every route this function mounts except the add-on-only root mount is defined
    # once in this table and mounted under each active prefix below: at root
    # in add-on mode (so HA ingress can proxy localhost:9583/), and under the
    # secret path when one is set (Docker / standalone direct access). A
    # deployment hits either, both, or — guarded above — neither. Deriving
    # the mounts from one table keeps them from drifting; the frontend uses
    # relative fetches (./api/settings/...) so the handlers work at any prefix.
    routes: list[tuple[str, list[str], str]] = [
        ("/settings", ["GET"], "settings_page"),
        ("/api/settings/tools", ["GET"], "get_tools"),
        ("/api/settings/tools", ["POST"], "save_tools"),
        ("/api/settings/restart", ["POST"], "restart_addon"),
        ("/api/settings/info", ["GET"], "settings_info"),
        ("/api/settings/features", ["GET"], "get_feature_flags"),
        ("/api/settings/features", ["POST"], "save_feature_flags"),
        # Theme / accessibility prefs (#1574 review) — server-side copy so
        # they survive the stdio sidecar's per-spawn origin change
        ("/api/settings/theme", ["GET"], "get_theme_prefs"),
        ("/api/settings/theme", ["POST"], "save_theme_prefs"),
        # Advanced settings endpoints
        ("/api/settings/advanced", ["GET"], "get_advanced_settings"),
        ("/api/settings/advanced", ["POST"], "save_advanced_settings"),
        # Auto-backup endpoints (#1288)
        ("/api/settings/backups", ["GET"], "list_backups"),
        ("/api/settings/backups", ["DELETE"], "delete_backups_bulk"),
        ("/api/settings/backups/{name}", ["GET"], "view_backup"),
        ("/api/settings/backups/{name}/diff", ["GET"], "diff_backup"),
        ("/api/settings/backups/{name}/restore", ["POST"], "restore_backup"),
        ("/api/settings/backups/{name}", ["DELETE"], "delete_backup"),
        ("/api/settings/backup-config", ["GET"], "get_backup_config"),
        ("/api/settings/backup-config", ["POST"], "save_backup_config"),
        # Custom filesystem directories (issue #1567) — component-owned list
        ("/api/settings/fs-custom-paths", ["GET"], "get_fs_custom_paths"),
        ("/api/settings/fs-custom-paths", ["POST"], "save_fs_custom_paths"),
        # Tool security policies endpoints
        ("/api/policy/config", ["GET"], "policy_get_config"),
        ("/api/policy/config", ["PUT"], "policy_put_config"),
        ("/api/policy/pending", ["GET"], "policy_get_pending"),
        ("/api/policy/approve", ["POST"], "policy_post_approve"),
        ("/api/policy/deny", ["POST"], "policy_post_deny"),
        ("/api/policy/tool-schema", ["GET"], "policy_get_tool_schema"),
        ("/api/policy/value-source", ["GET"], "policy_get_value_source"),
        # Entity visibility filter endpoints (issue #1728)
        ("/api/visibility/config", ["GET"], "visibility_get_config"),
        ("/api/visibility/config", ["PUT"], "visibility_put_config"),
    ]

    def _mount(prefix: str, *, guard: bool = False) -> None:
        # guard=True wraps each handler in _ingress_only so the route only
        # answers HA ingress (the Supervisor) — used for the add-on root
        # mount, whose port 9583 is reachable without the MCP secret.
        for path, methods, handler_key in routes:
            handler = handlers[handler_key]
            if guard:
                handler = _ingress_only(handler)
            mcp.custom_route(f"{prefix}{path}", methods=methods)(handler)

    if is_addon:
        # Root mount lets HA ingress proxy localhost:9583/ → the settings UI
        # ("Open Web UI" button). The published port 9583 also makes these
        # routes reachable by direct callers that present no MCP secret, so
        # the root mount is gated with _ingress_only: only the Supervisor
        # (HA ingress, 172.30.32.2) may reach root; every other caller gets
        # 403 and must use the secret-path mount below. The "Open Web UI"
        # button is unaffected — its traffic arrives from the Supervisor.
        mcp.custom_route("/", methods=["GET"])(_ingress_only(handlers["root_page"]))
        _mount("", guard=True)

    if secret_prefix:
        # Mount under the MCP secret path so Docker / standalone clients
        # need the same secret to reach the UI as they do for the MCP
        # endpoint.
        _mount(secret_prefix)
        # Record the mount so ha_get_overview can point users at the settings
        # page in HTTP transports that have no stdio sidecar URL file (#1458).
        global _http_settings_prefix
        _http_settings_prefix = secret_prefix
