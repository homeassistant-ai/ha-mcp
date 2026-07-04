"""Admin-only "Open Web UI" access to the in-process server's settings page (#1527).

The in-process ha-mcp server serves its web settings UI on the loopback interface
at ``http://127.0.0.1:<port><secret_path>/settings`` — unreachable from a browser
and guarded only by the secret path. This module gives every install type the
add-on's "Open Web UI" experience: an admin-only sidebar panel ("HA-MCP") that
opens that settings UI through Home Assistant's own HTTP server, so it works over
the Nabu Casa remote URL and never exposes the loopback secret path to the browser.

Auth model — a plain panel / iframe navigation is a browser GET that carries no
``Authorization`` header, so Home Assistant's normal ``requires_auth`` cannot gate
it. HA's signed-path helper (:func:`homeassistant.components.http.async_sign_path`)
is also unusable: a signature binds ONE exact path + query string, but the settings
app issues relative ``./api/settings/*`` fetches that drop the query — each would
land on a different, unsigned path and 401. Instead:

1. A tiny custom panel runs *inside* the authenticated frontend (it receives the
   ``hass`` object). It POSTs the logged-in user's access token to the session
   endpoint below.
2. :class:`_SessionView` (``requires_auth=True``) authenticates that token the
   normal way, refuses non-admins, and returns a short-lived HttpOnly,
   SameSite=Strict session cookie scoped to the proxy path.
3. The panel then embeds ``…/ui/app/settings`` in an iframe. The browser attaches
   the cookie to every same-origin request under the proxy path — including the
   settings app's relative sub-fetches — so the whole app works unchanged.
4. :class:`_ProxyView` (``requires_auth=False`` because the iframe cannot send a
   bearer) validates that cookie against a live admin user on every request and
   forwards to the loopback settings server.

The proxy reuses the ingress webhook's loopback target + aiohttp session
(``hass.data[DOMAIN][DATA_WEBHOOK]``), so it is available exactly while the server
is running and returns 503 otherwise.
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

# Sidebar panel identity. The url_path is the frontend route (…/ha-mcp); the
# webcomponent name is the custom element the module below defines. "HA-MCP"
# (not "MCP Server") avoids confusion with HA's official MCP Server integration.
PANEL_URL_PATH = "ha-mcp"
PANEL_TITLE = "HA-MCP"
PANEL_ICON = "mdi:robot-happy-outline"
PANEL_WEBCOMPONENT = "ha-mcp-server-panel"

# HTTP surface, all under one base so the session cookie can be tightly scoped.
_UI_BASE = "/api/ha_mcp_tools/ui"
_MODULE_URL = f"{_UI_BASE}/panel.js"
_SESSION_URL = f"{_UI_BASE}/session"
_APP_PREFIX = f"{_UI_BASE}/app/"
_PROXY_URL = f"{_UI_BASE}/app/{{path:.*}}"

# Session cookie. HttpOnly so page JS can never read it; SameSite=Strict so it
# rides only same-origin requests (the iframe is same-origin with the frontend);
# path-scoped to the proxy so it is never sent to the module/session endpoints.
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

_CLIENT_TIMEOUT = aiohttp.ClientTimeout(total=300, sock_connect=10, sock_read=300)


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
    if user is None or not getattr(user, "is_admin", False):
        store.pop(token, None)
        return False
    return True


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


class _ModuleView(HomeAssistantView):
    """Serve the custom-panel web component (public glue JS, no secrets).

    ``requires_auth`` is False because the browser loads this as an ES module via
    a plain ``import`` that cannot attach a bearer. The module contains only the
    bootstrap that mints a session and embeds the proxied iframe.
    """

    requires_auth = False
    cors_allowed = False
    url = _MODULE_URL
    name = "ha_mcp_tools:ui:module"

    async def get(self, request: web.Request) -> web.Response:
        """Return the panel module JavaScript."""
        return web.Response(
            body=_PANEL_JS.encode("utf-8"),
            content_type="text/javascript",
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
    """Bind the module / session / proxy views at most once per HA session."""
    if hass.data.get(_VIEWS_REGISTERED_KEY):
        return
    hass.http.register_view(_ModuleView())
    hass.http.register_view(_SessionView())
    hass.http.register_view(_ProxyView())
    hass.data[_VIEWS_REGISTERED_KEY] = True


async def _register_panel(hass: HomeAssistant) -> None:
    """Add the admin-only sidebar panel if it is not already present."""
    from homeassistant.components.frontend import async_panel_exists
    from homeassistant.components.panel_custom import async_register_panel

    if async_panel_exists(hass, PANEL_URL_PATH):
        return
    await async_register_panel(
        hass,
        frontend_url_path=PANEL_URL_PATH,
        webcomponent_name=PANEL_WEBCOMPONENT,
        sidebar_title=PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        module_url=_MODULE_URL,
        embed_iframe=False,
        require_admin=True,
    )


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
    """Swallow any exception from best-effort teardown (logged by the caller)."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if exc_type is not None:
            _LOGGER.debug("HA-MCP: settings-UI panel teardown error", exc_info=exc)
        return exc_type is not None


# ---------------------------------------------------------------------------
# Frontend module (the sidebar panel web component)
# ---------------------------------------------------------------------------
#
# Vanilla custom element (no Lit / HA-frontend imports) so it never couples to a
# specific frontend build. Home Assistant sets ``hass`` on the element; the
# element mints a session with the logged-in user's token, then embeds the
# proxied settings UI. Kept in sync with the Python surface via the JS-parse
# harness (tests/src/unit/_js_harness.py registers this renderer).

_PANEL_JS = f"""
const SESSION_URL = {_SESSION_URL!r};
const APP_URL = {_APP_PREFIX!r} + "settings";
// Re-mint at half the cookie lifetime so an open panel never expires mid-use.
const REFRESH_MS = {_SESSION_TTL_SECONDS // 2} * 1000;

class HaMcpServerPanel extends HTMLElement {{
  set hass(hass) {{
    this._hass = hass;
    this._start();
  }}

  connectedCallback() {{
    this._render();
    this._start();
  }}

  disconnectedCallback() {{
    if (this._timer) {{
      clearInterval(this._timer);
      this._timer = null;
    }}
  }}

  _render() {{
    if (this._root) return;
    this._root = this.attachShadow({{ mode: "open" }});
    this._root.innerHTML = `
      <style>
        :host {{ display: block; height: 100%; background: var(--primary-background-color, #fafafa); }}
        .frame {{ width: 100%; height: calc(100vh - var(--header-height, 56px)); border: 0; display: block; }}
        .msg {{
          padding: 24px; max-width: 640px; margin: 0 auto;
          font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif);
          color: var(--primary-text-color, #212121);
        }}
        .msg h2 {{ font-weight: 400; }}
        .hidden {{ display: none; }}
        a {{ color: var(--primary-color, #03a9f4); }}
      </style>
      <div class="msg" role="status" aria-live="polite">Loading the HA-MCP settings UI…</div>
      <iframe class="frame hidden" title="HA-MCP settings"></iframe>
    `;
    this._msg = this._root.querySelector(".msg");
    this._frame = this._root.querySelector("iframe");
  }}

  async _start() {{
    if (!this._hass || !this._root || this._busy) return;
    this._busy = true;
    try {{
      await this._mint();
      if (!this._timer) {{
        this._timer = setInterval(() => this._mint(), REFRESH_MS);
      }}
    }} finally {{
      this._busy = false;
    }}
  }}

  _token() {{
    const auth = this._hass && this._hass.auth;
    if (!auth) return null;
    return auth.accessToken || (auth.data && auth.data.access_token) || null;
  }}

  async _mint() {{
    const token = this._token();
    if (!token) {{
      this._showMessage("Not signed in to Home Assistant.");
      return;
    }}
    let resp;
    try {{
      resp = await fetch(SESSION_URL, {{
        method: "POST",
        credentials: "same-origin",
        headers: {{ Authorization: "Bearer " + token }},
      }});
    }} catch (err) {{
      this._showMessage("Could not reach Home Assistant to open the settings UI.");
      return;
    }}
    if (resp.status === 403) {{
      this._showMessage("The HA-MCP settings UI is available to administrators only.");
      return;
    }}
    if (!resp.ok) {{
      this._showMessage("Could not open the settings UI (HTTP " + resp.status + ").");
      return;
    }}
    await this._showApp();
  }}

  async _showApp() {{
    // Probe the proxy so a not-yet-running server shows a friendly message
    // instead of a raw 503 page inside the iframe.
    let probe;
    try {{
      probe = await fetch(APP_URL, {{ credentials: "same-origin" }});
    }} catch (err) {{
      this._showMessage("Could not reach the in-process MCP server.");
      return;
    }}
    if (probe.status === 503) {{
      this._showMessage(
        "The in-process MCP server is starting or is not running yet. " +
          "This view will refresh automatically."
      );
      setTimeout(() => this._mint(), 5000);
      return;
    }}
    if (!probe.ok) {{
      this._showMessage("The settings UI returned HTTP " + probe.status + ".");
      return;
    }}
    if (this._frame.getAttribute("src") !== APP_URL) {{
      this._frame.setAttribute("src", APP_URL);
    }}
    this._msg.classList.add("hidden");
    this._frame.classList.remove("hidden");
  }}

  _showMessage(text) {{
    this._frame.classList.add("hidden");
    this._msg.classList.remove("hidden");
    this._msg.textContent = text;
  }}
}}

if (!customElements.get({PANEL_WEBCOMPONENT!r})) {{
  customElements.define({PANEL_WEBCOMPONENT!r}, HaMcpServerPanel);
}}
"""


def render_panel_module() -> str:
    """Return the panel web-component module source (used by the JS-parse tests)."""
    return _PANEL_JS


def panel_config() -> ConfigType:
    """Return the sidebar-panel registration parameters (for assertions/tests)."""
    return {
        "frontend_url_path": PANEL_URL_PATH,
        "webcomponent_name": PANEL_WEBCOMPONENT,
        "sidebar_title": PANEL_TITLE,
        "sidebar_icon": PANEL_ICON,
        "module_url": _MODULE_URL,
        "require_admin": True,
    }
