"""Admin-only "Open Web UI" access to the in-process server's settings page (#1527).

The in-process ha-mcp server serves its web settings UI on the loopback interface
at ``http://127.0.0.1:<port><secret_path>/settings`` — unreachable from a browser
and guarded only by the secret path. This module gives every install type the
add-on's "Open Web UI" experience: an admin-only sidebar panel ("HA-MCP") that
opens that settings UI through Home Assistant's own HTTP server, so it works over
the Nabu Casa remote URL and never exposes the loopback secret path to the browser.

The sidebar entry is a built-in ``iframe`` panel — the panel type behind
"webpage" dashboards — NOT a custom panel. Home Assistant itself renders the
standard header (with the sidebar menu button) around our page, so navigation
behaves exactly like every other dashboard. The previous custom panel painted a
bare full-height iframe with no chrome, which left iOS companion-app users with
no way back to the HA UI: iOS has no system back button, edge swipes land inside
the iframe where the frontend cannot see them, and the app restores the trapped
route on every relaunch (#1795).

Auth model — an iframe panel navigation is a browser GET that carries no
``Authorization`` header, so Home Assistant's normal ``requires_auth`` cannot gate
it. HA's signed-path helper (:func:`homeassistant.components.http.async_sign_path`)
is also unusable: a signature binds ONE exact path + query string, but the settings
app issues relative ``./api/settings/*`` fetches that drop the query — each would
land on a different, unsigned path and 401. Instead:

1. The panel's iframe loads :class:`_BootView`, a tiny same-origin bootstrap
   page (public glue, no secrets). Being same-origin with the authenticated
   frontend, it reads the logged-in user's access token from the parent frame's
   ``home-assistant`` root element and POSTs it to the session endpoint below.
2. :class:`_SessionView` (``requires_auth=True``) authenticates that token the
   normal way, refuses non-admins, and returns a short-lived HttpOnly,
   SameSite=Strict session cookie scoped to the proxy path.
3. The boot page then embeds ``…/ui/app/settings`` in an inner iframe. The
   browser attaches the cookie to every same-origin request under the proxy
   path — including the settings app's relative sub-fetches — so the whole app
   works unchanged.
4. :class:`_ProxyView` (``requires_auth=False`` because the iframe cannot send a
   bearer) validates that cookie against a live admin user on every request and
   forwards to the loopback settings server.

The proxy reuses the server's loopback forwarding config + aiohttp session
(``hass.data[DOMAIN][DATA_WEBHOOK]`` — stored whenever the server is running,
even when the public webhook endpoint is disabled), so it is available exactly
while the server is running and returns 503 otherwise.
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import TYPE_CHECKING, Any

import aiohttp
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import DATA_WEBHOOK, DOMAIN

if TYPE_CHECKING:
    from homeassistant.helpers.typing import ConfigType

_LOGGER = logging.getLogger(__name__)

# Sidebar panel identity. The url_path is the frontend route (…/ha-mcp).
# "HA-MCP" (not "MCP Server") avoids confusion with HA's official MCP Server
# integration.
PANEL_URL_PATH = "ha-mcp"
PANEL_TITLE = "HA-MCP"
PANEL_ICON = "mdi:robot-happy-outline"

# HTTP surface, all under one base so the session cookie can be tightly scoped.
_UI_BASE = "/api/ha_mcp_tools/ui"
_BOOT_URL = f"{_UI_BASE}/boot"
_SESSION_URL = f"{_UI_BASE}/session"
_APP_PREFIX = f"{_UI_BASE}/app/"
_PROXY_URL = f"{_UI_BASE}/app/{{path:.*}}"

# Session cookie. HttpOnly so page JS can never read it; SameSite=Strict so it
# rides only same-site requests (the iframe is same-origin with the frontend);
# path-scoped to the proxy so it is never sent to the boot/session endpoints.
_COOKIE_NAME = "ha_mcp_tools_ui_session"
_COOKIE_PATH = f"{_UI_BASE}/app"

# Session lifetime. Short by design; the panel re-mints well within it while open.
_SESSION_TTL_SECONDS = 8 * 60 * 60

# Top-level hass.data keys. Both must survive config-entry teardown: aiohttp
# cannot unregister a bound view, so the views (and the sessions they validate)
# outlive a reload / re-enable of the entry.
_VIEWS_REGISTERED_KEY = "ha_mcp_tools_ui_views_registered"
_SESSIONS_KEY = "ha_mcp_tools_ui_sessions"

# Request headers never forwarded to the loopback server. Hop-by-hop plus the
# browser's cookie/authorization (the loopback server has no auth on the secret
# path and must not receive the session cookie or the frontend bearer).
_STRIPPED_REQUEST_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "cookie",
        "authorization",
    }
)

# Response headers recomputed by aiohttp on the way out, or invalid once the body
# has been transparently decompressed by ``resp.read()``. Everything else
# (Content-Type, Cache-Control, …) passes through so the settings app behaves
# exactly as when reached directly.
_STRIPPED_RESPONSE_HEADERS = frozenset(
    {
        "transfer-encoding",
        "connection",
        "content-length",
        "content-encoding",
        "keep-alive",
    }
)


# ---------------------------------------------------------------------------
# Session store (server-side; no secret ever placed in a URL)
# ---------------------------------------------------------------------------


def _sessions(hass: HomeAssistant) -> dict[str, dict[str, Any]]:
    """Return the token → ``{user_id, expires}`` store, creating it once."""
    store = hass.data.get(_SESSIONS_KEY)
    if not isinstance(store, dict):
        store = {}
        hass.data[_SESSIONS_KEY] = store
    return store


def _prune_expired(store: dict[str, dict[str, Any]], now: float) -> None:
    """Drop expired sessions so the store cannot grow without bound."""
    for token in [t for t, s in store.items() if s["expires"] <= now]:
        del store[token]


def _mint_session(hass: HomeAssistant, user_id: str) -> str:
    """Create and store a new session token for ``user_id``; return the token."""
    store = _sessions(hass)
    now = time.monotonic()
    _prune_expired(store, now)
    token = secrets.token_urlsafe(32)
    store[token] = {"user_id": user_id, "expires": now + _SESSION_TTL_SECONDS}
    return token


async def _session_user_is_admin(hass: HomeAssistant, token: str | None) -> bool:
    """Return True iff ``token`` maps to a live, still-admin user session.

    Re-checks the user's admin flag on every request so revoking admin (or the
    user) takes effect immediately, not only when the session expires. A stale or
    demoted session is dropped so it cannot be retried.
    """
    if not token:
        return False
    store = _sessions(hass)
    now = time.monotonic()
    _prune_expired(store, now)
    session = store.get(token)
    if session is None:
        return False
    user = await hass.auth.async_get_user(session["user_id"])
    if (
        user is None
        or getattr(user, "system_generated", False)
        or not getattr(user, "is_active", False)
        or not getattr(user, "is_admin", False)
    ):
        # Same acceptance bar as the ha_auth webhook gate (review finding:
        # the two admin gates must not drift): active, human, administrator.
        store.pop(token, None)
        return False
    return True


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


class _BootView(HomeAssistantView):
    """Serve the bootstrap page the iframe panel embeds (public glue, no secrets).

    ``requires_auth`` is False because the panel's iframe loads this with a plain
    GET that cannot attach a bearer. The page contains only the bootstrap that
    mints a session (with the token it reads from the parent frontend frame) and
    embeds the proxied settings app.
    """

    requires_auth = False
    cors_allowed = False
    url = _BOOT_URL
    name = "ha_mcp_tools:ui:boot"

    async def get(self, request: web.Request) -> web.Response:
        """Return the bootstrap HTML page."""
        return web.Response(
            body=_BOOT_HTML.encode("utf-8"),
            content_type="text/html",
            charset="utf-8",
            headers={"Cache-Control": "no-cache"},
        )


class _SessionView(HomeAssistantView):
    """Mint a short-lived session cookie for an authenticated admin user.

    ``requires_auth`` is True, so Home Assistant validates the frontend's bearer
    before this runs. The extra admin check refuses non-admins (the panel is
    admin-only, and the settings UI can change privileged server settings).
    """

    requires_auth = True
    cors_allowed = False
    url = _SESSION_URL
    name = "ha_mcp_tools:ui:session"

    async def post(self, request: web.Request) -> web.Response:
        """Issue the session cookie, or 403 for a non-admin caller."""
        user = request.get("hass_user")
        if user is None or not getattr(user, "is_admin", False):
            return web.json_response({"error": "admin_required"}, status=403)

        token = _mint_session(request.app["hass"], user.id)
        response = web.json_response({"ttl": _SESSION_TTL_SECONDS})
        response.set_cookie(
            _COOKIE_NAME,
            token,
            max_age=_SESSION_TTL_SECONDS,
            path=_COOKIE_PATH,
            httponly=True,
            samesite="Strict",
            secure=_request_is_https(request),
        )
        return response


class _ProxyView(HomeAssistantView):
    """Forward settings-UI traffic to the loopback server for a valid session.

    ``requires_auth`` is False because the iframe (and its relative sub-fetches)
    cannot send a bearer; the session cookie minted by :class:`_SessionView` is
    the credential and is re-validated against a live admin user on every request.
    Returns 503 while the server is not running and 401 without a valid session.
    """

    requires_auth = False
    cors_allowed = False
    url = _PROXY_URL
    name = "ha_mcp_tools:ui:proxy"

    async def get(self, request: web.Request, path: str) -> web.StreamResponse:
        """Proxy a GET (the settings page and read endpoints)."""
        return await self._forward(request, path)

    async def post(self, request: web.Request, path: str) -> web.StreamResponse:
        """Proxy a POST (save endpoints)."""
        return await self._forward(request, path)

    async def put(self, request: web.Request, path: str) -> web.StreamResponse:
        """Proxy a PUT (policy-config writes)."""
        return await self._forward(request, path)

    async def delete(self, request: web.Request, path: str) -> web.StreamResponse:
        """Proxy a DELETE (backup deletion)."""
        return await self._forward(request, path)

    async def _forward(self, request: web.Request, path: str) -> web.StreamResponse:
        """Validate the session, then forward to the loopback settings server."""
        hass: HomeAssistant = request.app["hass"]

        if not await _session_user_is_admin(hass, request.cookies.get(_COOKIE_NAME)):
            return web.Response(status=401, text="Unauthorized")

        # Defense in depth: never let a crafted path escape the secret-path
        # prefix on the loopback server (the caller is already an admin, so this
        # only blocks confusing requests, but it keeps the target well-formed).
        if any(segment == ".." for segment in path.split("/")):
            return web.Response(status=400, text="Bad request")

        cfg = _webhook_cfg(hass)
        if cfg is None:
            return web.Response(status=503, text="The MCP server is not running")

        target = f"{cfg['target_url']}/{path}"
        if request.query_string:
            target = f"{target}?{request.query_string}"
        session: aiohttp.ClientSession = cfg["session"]

        body = await request.read()
        forward_headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in _STRIPPED_REQUEST_HEADERS
        }

        try:
            async with session.request(
                method=request.method,
                url=target,
                headers=forward_headers,
                data=body if body else None,
            ) as upstream:
                return await _relay_response(request, upstream)
        except aiohttp.ClientError as err:
            _LOGGER.error("HA-MCP settings proxy: upstream request failed: %s", err)
            return web.Response(status=502, text="MCP settings server unavailable")
        except Exception as err:
            _LOGGER.exception("HA-MCP settings proxy: unexpected error: %s", err)
            return web.Response(status=500, text="MCP settings server error")


async def _relay_response(
    request: web.Request, upstream: aiohttp.ClientResponse
) -> web.StreamResponse:
    """Relay the loopback response, streaming when it is an event stream.

    The loopback server is our own trusted process, so — unlike the MCP webhook —
    the Content-Type is passed through unchanged (the settings page is text/html,
    the API endpoints are JSON): coercing it would break the page.
    """
    content_type = upstream.headers.get("Content-Type", "")
    headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() not in _STRIPPED_RESPONSE_HEADERS
    }

    if "text/event-stream" in content_type:
        headers["Cache-Control"] = "no-cache, no-transform"
        headers["X-Accel-Buffering"] = "no"
        response = web.StreamResponse(status=upstream.status, headers=headers)
        await response.prepare(request)
        try:
            async for chunk in upstream.content.iter_any():
                await response.write(chunk)
        except aiohttp.ClientError as err:
            _LOGGER.error("HA-MCP settings proxy: upstream dropped mid-stream: %s", err)
        with _suppress_connection_reset():
            await response.write_eof()
        return response

    return web.Response(
        status=upstream.status, body=await upstream.read(), headers=headers
    )


# ---------------------------------------------------------------------------
# Registration / teardown
# ---------------------------------------------------------------------------


async def async_register_ui_panel(hass: HomeAssistant) -> None:
    """Register the settings-UI proxy views (once) and the sidebar panel.

    Called from the server entry's setup. The views resolve the running server
    from ``hass.data`` per request, so they are bound once per HA session and
    reused across reloads; the panel is (re)added here and removed on unload.
    Any failure is logged and swallowed — a frontend hiccup must never block the
    config entry from loading.
    """
    try:
        _register_views(hass)
        await _register_panel(hass)
    except Exception:
        _LOGGER.exception("HA-MCP: failed to register the settings-UI panel")


def async_unregister_ui_panel(hass: HomeAssistant) -> None:
    """Remove the sidebar panel on entry unload (the views stay bound).

    aiohttp cannot unregister the views; they return 503 once the server is no
    longer running, so removing the sidebar entry is enough to reflect the
    paused/removed state.
    """
    from homeassistant.components.frontend import async_remove_panel

    with _suppress_all():
        async_remove_panel(hass, PANEL_URL_PATH, warn_if_unknown=False)


def _register_views(hass: HomeAssistant) -> None:
    """Bind the boot / session / proxy views at most once per HA session."""
    if hass.data.get(_VIEWS_REGISTERED_KEY):
        return
    hass.http.register_view(_BootView())
    hass.http.register_view(_SessionView())
    hass.http.register_view(_ProxyView())
    hass.data[_VIEWS_REGISTERED_KEY] = True


async def _register_panel(hass: HomeAssistant) -> None:
    """Add the admin-only sidebar panel if it is not already present.

    Registered as a built-in ``iframe`` panel (the "webpage dashboard" panel
    type): Home Assistant renders its standard header around the page, so the
    panel can never trap navigation the way a chrome-less custom panel did on
    iOS (#1795).
    """
    from homeassistant.components.frontend import (
        async_panel_exists,
        async_register_built_in_panel,
    )

    if async_panel_exists(hass, PANEL_URL_PATH):
        return
    cfg = panel_config()
    async_register_built_in_panel(hass, cfg.pop("component_name"), **cfg)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _webhook_cfg(hass: HomeAssistant) -> dict[str, Any] | None:
    """Return the running server's forwarding config, or None when it is down."""
    domain_data = hass.data.get(DOMAIN)
    if not isinstance(domain_data, dict):
        return None
    cfg = domain_data.get(DATA_WEBHOOK)
    return cfg if isinstance(cfg, dict) else None


def _request_is_https(request: web.Request) -> bool:
    """Return True when the request reached HA over HTTPS (honoring the proxy)."""
    forwarded = request.headers.get("X-Forwarded-Proto")
    return bool((forwarded or request.scheme) == "https")


class _suppress_connection_reset:
    """Swallow a ConnectionResetError from a client that closed mid-stream."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return exc_type is not None and issubclass(exc_type, ConnectionResetError)


class _suppress_all:
    """Swallow any Exception from best-effort teardown, logged at WARNING."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if exc_type is None or not issubclass(exc_type, Exception):
            return False  # never swallow KeyboardInterrupt/SystemExit
        _LOGGER.warning("HA-MCP: settings-UI panel teardown error", exc_info=exc)
        return True


# ---------------------------------------------------------------------------
# Bootstrap page (embedded by the built-in iframe panel)
# ---------------------------------------------------------------------------
#
# Plain same-origin page (no Lit / HA-frontend imports) so it never couples to a
# specific frontend build. Home Assistant's own iframe panel draws the standard
# header around it; this page only mints the session and embeds the proxied
# settings app. The script is a separate string so the node syntax test can
# parse it alone. Deliberately NOT registered in _js_harness._PY_RENDERERS
# (importing this module needs Home Assistant installed, which would break the
# harness for every surface); coverage = the node --check syntax test plus the
# Python-side session/proxy tests in test_ui_panel.py.

_BOOT_JS = f"""
const SESSION_URL = {_SESSION_URL!r};
const APP_BASE_URL = {_APP_PREFIX!r} + "settings";
// Re-mint at half the cookie lifetime so an open panel never expires mid-use.
const REFRESH_MS = {_SESSION_TTL_SECONDS // 2} * 1000;
// While the frontend is still booting (a cold start straight into this panel),
// the parent frame has no token yet -- poll gently until it does (local reads,
// no network). After TOKEN_HINT_AFTER misses, surface a hint but keep polling.
const TOKEN_RETRY_MS = 1000;
const TOKEN_HINT_AFTER = 20;
// Transient failures (network blip, server starting/restarting) retry on
// their own. Auth refusals (401/403) never auto-retry: every rejected bearer
// counts as a failed login for http.ban, and a retry loop got users IP-banned
// from their own instance (#1802).
const RETRY_MS = 5000;
const FETCH_TIMEOUT_MS = 15000;

const msg = document.querySelector(".msg");
const frame = document.querySelector("iframe");
let timer = null;
let busy = false;
let tokenMisses = 0;
let authDead = false;

function homeAssistantRoot() {{
  try {{
    if (window.parent === window) return null;
    return window.parent.document.querySelector("home-assistant");
  }} catch (err) {{
    return null;
  }}
}}

function appUrl() {{
  const root = homeAssistantRoot();
  const language = root && root.hass && root.hass.language;
  if (!language) return APP_BASE_URL;
  return APP_BASE_URL + "?ha_lang=" + encodeURIComponent(language);
}}

function showMessage(text, isError) {{
  frame.classList.add("hidden");
  msg.classList.remove("hidden");
  // Failure messages announce assertively (style guide: status regions switch
  // to role=alert on the failure path); benign progress stays polite.
  msg.setAttribute("role", isError ? "alert" : "status");
  msg.setAttribute("aria-live", isError ? "assertive" : "polite");
  msg.textContent = text;
}}

function transientFailure(text) {{
  // Keep an already-working app visible through a transient blip (the iframe
  // holds state); only surface the message while nothing is showing yet.
  if (!timer) {{
    showMessage(text, true);
  }}
  setTimeout(mint, RETRY_MS);
}}

function fetchWithTimeout(url, options) {{
  // A stalled (never-settling) fetch would wedge `busy` and silently stop all
  // future re-mints; a timeout resolves it into the retry path instead.
  const controller = new AbortController();
  const t = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
  return fetch(url, Object.assign({{ signal: controller.signal }}, options)).finally(
    () => clearTimeout(t)
  );
}}

async function token() {{
  // Same-origin parent = the authenticated HA frontend. Its root element owns
  // the live `hass` object the frontend keeps fresh; its auth.accessToken is
  // the same bearer the frontend itself uses. Refresh an expired token before
  // use -- POSTing a stale bearer counts as a failed login for http.ban
  // (#1802). A failed refresh means the sign-in itself is dead: mark it
  // terminal rather than looping.
  try {{
    const root = homeAssistantRoot();
    const auth = root && root.hass && root.hass.auth;
    if (!auth) return null;
    if (auth.expired && typeof auth.refreshAccessToken === "function") {{
      try {{
        await auth.refreshAccessToken();
      }} catch (err) {{
        authDead = true;
        return null;
      }}
    }}
    return auth.accessToken || (auth.data && auth.data.access_token) || null;
  }} catch (err) {{
    return null; // cross-origin parent: not embedded in the HA frontend
  }}
}}

async function mint() {{
  if (busy) return;
  busy = true;
  try {{
    const bearer = await token();
    if (!bearer) {{
      if (authDead) {{
        showMessage(
          "The Home Assistant sign-in has expired. Reload the page to try again.",
          true
        );
        return;
      }}
      if (window.parent === window) {{
        showMessage("Open this page from the HA-MCP entry in the Home Assistant sidebar.");
        return;
      }}
      tokenMisses += 1;
      if (tokenMisses === TOKEN_HINT_AFTER) {{
        showMessage(
          "Still waiting for the Home Assistant sign-in. If this page is not " +
            "inside the Home Assistant frontend, open it from the HA-MCP " +
            "sidebar entry."
        );
      }}
      setTimeout(mint, TOKEN_RETRY_MS);
      return;
    }}
    tokenMisses = 0;
    let resp;
    try {{
      resp = await fetchWithTimeout(SESSION_URL, {{
        method: "POST",
        credentials: "same-origin",
        headers: {{ Authorization: "Bearer " + bearer }},
      }});
    }} catch (err) {{
      transientFailure("Could not reach Home Assistant to open the settings UI.");
      return;
    }}
    if (resp.status === 401) {{
      // Never loop on a rejected bearer -- see the RETRY_MS note (#1802).
      showMessage(
        "Home Assistant rejected the sign-in token. Reload the page to try again.",
        true
      );
      return;
    }}
    if (resp.status === 403) {{
      showMessage("The HA-MCP settings UI is available to administrators only.", true);
      return;
    }}
    if (!resp.ok) {{
      transientFailure("Could not open the settings UI (HTTP " + resp.status + ").");
      return;
    }}
    await showApp();
  }} finally {{
    busy = false;
  }}
}}

async function showApp() {{
  // Probe the proxy so a not-yet-running server shows a friendly message
  // instead of a raw 503 page inside the iframe.
  const targetUrl = appUrl();
  let probe;
  try {{
    probe = await fetchWithTimeout(targetUrl, {{ credentials: "same-origin" }});
  }} catch (err) {{
    transientFailure("Could not reach Home Assistant to load the settings UI.");
    return;
  }}
  if (probe.status === 503) {{
    if (!timer) {{
      showMessage(
        "The in-process MCP server is starting or is not running yet. " +
          "This view will refresh automatically."
      );
    }}
    setTimeout(mint, RETRY_MS);
    return;
  }}
  if (!probe.ok) {{
    transientFailure("The settings UI returned HTTP " + probe.status + ".");
    return;
  }}
  if (frame.getAttribute("src") !== targetUrl) {{
    frame.setAttribute("src", targetUrl);
  }}
  msg.classList.add("hidden");
  frame.classList.remove("hidden");
  if (!timer) {{
    timer = setInterval(mint, REFRESH_MS);
  }}
}}

mint();
"""

_BOOT_HTML = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>HA-MCP settings</title>
<style>
  html, body {{ height: 100%; margin: 0; background: #fafafa; }}
  main {{ height: 100%; outline: none; }}
  iframe {{ width: 100%; height: 100%; border: 0; display: block; }}
  .msg {{
    padding: 24px; max-width: 640px; margin: 0 auto; box-sizing: border-box;
    font-family: Roboto, sans-serif; color: #212121;
  }}
  .hidden {{ display: none; }}
  @media (prefers-color-scheme: dark) {{
    html, body {{ background: #111111; }}
    .msg {{ color: #e1e1e1; }}
  }}
</style>
</head>
<body>
<main id="main-content" tabindex="-1">
<div class="msg" role="status" aria-live="polite">Loading the HA-MCP settings UI…</div>
<iframe class="hidden" title="HA-MCP settings"></iframe>
</main>
<script>
{_BOOT_JS}
</script>
</body>
</html>
"""


def render_boot_script() -> str:
    """Return the boot-page script source (used by the JS-parse tests)."""
    return _BOOT_JS


def render_boot_page() -> str:
    """Return the boot-page HTML (used by the tests)."""
    return _BOOT_HTML


def panel_config() -> ConfigType:
    """Return the sidebar-panel registration parameters (registration + tests)."""
    return {
        "component_name": "iframe",
        "frontend_url_path": PANEL_URL_PATH,
        "sidebar_title": PANEL_TITLE,
        "sidebar_icon": PANEL_ICON,
        "config": {"url": _BOOT_URL},
        "require_admin": True,
    }
