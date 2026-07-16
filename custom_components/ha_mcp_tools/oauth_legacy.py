"""Legacy OAuth 2.1 authorization server for the HA-MCP Server webhook.

Ported from the webhook-proxy add-on's ``mcp_proxy/oauth.py`` ``OAuthProvider``
+ ``AuthorizeView`` + ``TokenView`` (the proven ``legacy`` mode). Self-hosted,
single-tenant authorization server with a static client_id/client_secret pair,
for MCP clients that need a credential to paste rather than HA core's native
OAuth (``ha_auth``) — currently just Google Gemini Spark, whose custom
connected apps use the Client ID Metadata Document pattern that HA core's
``/auth/authorize`` does not yet support for cross-origin redirect_uris
(home-assistant/core#176282).

Unlike the add-on, this module holds no reference to ``hass``, the webhook id,
or a public base URL: the discovery-document layer (RFC 8414 / RFC 9728) and
base-URL resolution already live in ``mcp_webhook.py``, shared with the
``ha_auth`` mode, and are extended there to be mode-aware. This module owns
only the OAuth-specific state (tokens, PKCE codes, client auth) and the two
root views the add-on's ``ha_auth`` mode never needed (``ha_auth`` mode has HA
core serve its own ``/auth/authorize`` + ``/auth/token``).

Tokens are opaque, HMAC-SHA256 signed (``body.sig``), and carry enough state
(``{kind, iat, exp, jti, cid}``) to validate without a server-side store, so
the integration survives HA restarts. ``cid`` (the client_id at issuance time)
means rotating the client_id revokes every outstanding token at the restart
that rebinds the root views with the new identity — until then the bound
provider keeps the old client_id and old tokens keep validating (see
:func:`bind_legacy_views`).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import secrets
import time
from collections.abc import Callable
from html import escape
from typing import TYPE_CHECKING, TypedDict
from urllib.parse import unquote_plus, urlparse

from aiohttp import web
from homeassistant.components.http import HomeAssistantView

from .const import WEBHOOK_AUTH_LEGACY

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Registered at the HA ROOT rather than under a component-namespaced path
# because MCP clients (claude.ai, and per the reporter, Google Spark too)
# construct the authorize URL as ``<host>/authorize`` from the resource host
# root — they do not read the ``authorization_endpoint`` field of the
# authorization-server metadata document. Registering at the root is the only
# way to actually catch the redirect. Same paths the add-on's legacy mode
# uses — see the ownership guard below for why that matters.
AUTHORIZE_PATH = "/authorize"
TOKEN_PATH = "/token"

ACCESS_TOKEN_TTL = 60 * 60  # 1 hour
REFRESH_TOKEN_TTL = 30 * 24 * 60 * 60  # 30 days
AUTH_CODE_TTL = 5 * 60  # 5 minutes
TOKEN_KIND_ACCESS = "access"
TOKEN_KIND_REFRESH = "refresh"

# RFC 6749 §5.1: a /token response body carries the access/refresh credentials,
# so it MUST NOT be cached by any intermediary (reverse proxy, Nabu Casa, etc.).
_TOKEN_RESPONSE_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache"}

# RFC 8252 §7.3: native/CLI OAuth clients (e.g. GitHub Copilot CLI) receive the
# authorization code on a loopback redirect, for which the spec explicitly
# permits a plain http scheme. Every non-loopback redirect must still be https.
_LOOPBACK_HOSTNAMES = frozenset({"localhost"})

# RFC 7636 §4.1: code_verifier is 43-128 chars from the unreserved URL set.
PKCE_VERIFIER_MIN = 43
PKCE_VERIFIER_MAX = 128
# SHA-256 → 32 bytes → 43 base64url chars (no padding).
PKCE_S256_CHALLENGE_LEN = 43
_PKCE_VERIFIER_RE = re.compile(r"^[A-Za-z0-9._~-]+$")
_PKCE_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")

# Pending-code dict cap. An attacker spamming /authorize with valid params
# could grow the dict between the prune passes that run on each issuance.
# 1000 codes is well past anything legitimate (5-min TTL, single-tenant).
MAX_PENDING_CODES = 1000

_RESTART_HINT = (
    "If this persists, fully restart Home Assistant (Settings -> System -> "
    "Restart) -- Home Assistant cannot rebind the /authorize and /token "
    "endpoints to new credentials without a full restart."
)

# ---------------------------------------------------------------------------
# Root-route ownership guard
# ---------------------------------------------------------------------------
#
# The webhook-proxy add-on's legacy mode (both the stable and dev flavors)
# ALSO claims the root /authorize + /token routes in the same HA instance —
# aiohttp lets the first-registered path win and silently shadows a later
# duplicate, and HA cannot unregister a bound view until it restarts. These
# two literals are therefore the SAME strings as
# ``mcp_proxy.OAUTH_ROUTE_OWNER_KEY`` / ``mcp_proxy.OAUTH_ROUTE_KEY_FINGERPRINT``
# (deliberately domain-neutral, not namespaced under this component's DOMAIN)
# so whichever integration binds first can be recognized by the other and the
# second registrant fails loud instead of being silently shadowed.
OAUTH_ROUTE_OWNER_KEY = "webhook_proxy_oauth_route_owner"
OAUTH_ROUTE_KEY_FINGERPRINT = "webhook_proxy_oauth_route_key_fingerprint"

# TOP-LEVEL hass.data key (NOT under DOMAIN, so it survives
# async_unload_entry's hass.data.pop(DOMAIN)) holding the LegacyOAuthProvider
# instance bound to the root views for this HA session. A reload reuses it
# when the credentials match; see bind_legacy_views.
_LEGACY_PROVIDER_KEY = "ha_mcp_tools_oauth_legacy_provider"

# TOP-LEVEL hass.data flag recording whether the currently-bound root views were
# registered MID-SESSION (hass already running → the route is not actually live
# until a full HA restart) rather than at boot (live immediately). It cannot be
# cleared without a real process restart, which wipes hass.data — so it stays
# True for the life of a session that late-bound, and is absent/False for a
# session that bound cleanly at boot. Read on every reuse so an unrelated reload
# before that restart does not falsely report "no restart needed" and clear the
# repair (the views are still not live). See bind_legacy_views.
_LEGACY_PENDING_RESTART_KEY = "ha_mcp_tools_oauth_legacy_pending_restart"

# This component's DOMAIN, duplicated here (rather than imported) to avoid a
# module-level dependency on const.DOMAIN for the ownership-marker value —
# the value written IS "ha_mcp_tools", checked against by name below.
_DOMAIN = "ha_mcp_tools"


class LegacyOAuthRouteConflict(RuntimeError):
    """Raised when another integration already owns the root OAuth routes."""


def _oauth_route_fingerprint(
    client_id: str, client_secret: str, signing_key: bytes
) -> str:
    """Stable fingerprint of the OAuth identity bound to the root views."""
    h = hashlib.sha256()
    h.update(client_id.encode())
    h.update(b"\0")
    h.update(client_secret.encode())
    h.update(b"\0")
    h.update(signing_key)
    return h.hexdigest()


def _normalize_signing_key(signing_key: bytes | str) -> bytes:
    """Accept either raw bytes or a hex string (how entry.data stores it,
    since entry.data must be JSON-serializable)."""
    return bytes.fromhex(signing_key) if isinstance(signing_key, str) else signing_key


def bind_legacy_views(
    hass: HomeAssistant,
    client_id: str,
    client_secret: str,
    signing_key: bytes | str,
) -> tuple[LegacyOAuthProvider, bool]:
    """Bind the root ``/authorize`` + ``/token`` views at most once per HA session.

    Returns ``(provider, restart_needed)``. ``provider`` is the identity now
    authoritative for the webhook's bearer gate — on a reload with unchanged
    credentials this REUSES the already-bound provider (aiohttp cannot rebind
    a view, so a fresh provider object would mint tokens the bound views never
    issued and vice versa). ``restart_needed`` is True when:

    * the currently-bound views were registered after HA finished starting (a
      route registered while ``hass.is_running`` is not actually live until a
      restart — this mirrors the add-on's ``oauth_restart_needed =
      hass.is_running``) and that restart has not happened yet — this pending
      state persists across config-entry reloads until a real HA restart wipes
      ``hass.data``, or
    * the credentials changed since the currently-bound views were registered
      (the bound views keep serving the OLD identity until a restart rebinds
      them with the new one).

    Raises :class:`LegacyOAuthRouteConflict` when the webhook-proxy add-on (or
    its dev flavor) already owns the root routes in this HA instance.
    """
    key_bytes = _normalize_signing_key(signing_key)
    fingerprint = _oauth_route_fingerprint(client_id, client_secret, key_bytes)

    owner = hass.data.get(OAUTH_ROUTE_OWNER_KEY)
    if owner is not None and owner != _DOMAIN:
        _LOGGER.error(
            "HA-MCP: cannot enable legacy OAuth mode -- the Webhook Proxy "
            "add-on ('%s') already owns the root /authorize and /token routes "
            "in this Home Assistant instance, and Home Assistant cannot "
            "release them until it restarts. Stop that add-on and restart "
            "Home Assistant, then enable legacy mode again.",
            owner,
        )
        raise LegacyOAuthRouteConflict(owner)

    bound_provider = hass.data.get(_LEGACY_PROVIDER_KEY)
    if owner == _DOMAIN and isinstance(bound_provider, LegacyOAuthProvider):
        bound_fingerprint = hass.data.get(OAUTH_ROUTE_KEY_FINGERPRINT)
        creds_changed = bound_fingerprint != fingerprint
        if creds_changed:
            _LOGGER.warning(
                "HA-MCP: legacy OAuth credentials changed but the bound root "
                "views still use the previous ones -- a Home Assistant "
                "restart is required to activate the new credentials."
            )
        # Still restart-pending if the views were late-bound this session (flag
        # persisted below) OR the credentials just changed. Reading the flag
        # rather than recomputing keeps an unrelated reload from clearing a
        # still-pending restart repair while the routes remain not-live.
        pending = bool(hass.data.get(_LEGACY_PENDING_RESTART_KEY))
        return bound_provider, pending or creds_changed

    # First registration this HA session.
    provider = LegacyOAuthProvider(
        client_id=client_id,
        client_secret=client_secret,
        signing_key=key_bytes,
        active_mode_getter=lambda: _live_auth_mode(hass),
    )
    hass.http.register_view(AuthorizeView(provider))
    hass.http.register_view(TokenView(provider))
    hass.data[OAUTH_ROUTE_OWNER_KEY] = _DOMAIN
    hass.data[OAUTH_ROUTE_KEY_FINGERPRINT] = fingerprint
    hass.data[_LEGACY_PROVIDER_KEY] = provider
    # A first registration happening mid-session isn't live until a full HA
    # restart; flag it. At HA boot (hass.is_running is still False while
    # integrations are being set up) it binds cleanly. Persist the pending
    # state so a later reload before that restart reuses it (see the reuse
    # branch above) rather than recomputing "no restart needed".
    pending_restart = hass.is_running
    hass.data[_LEGACY_PENDING_RESTART_KEY] = pending_restart
    return provider, pending_restart


def legacy_credentials_active(
    hass: HomeAssistant,
    client_id: str,
    client_secret: str,
    signing_key: bytes | str,
) -> bool:
    """Whether the bound root views currently serve exactly these credentials.

    False while a credential rotation is pending a restart (the bound provider
    keeps the previous identity until then — see :func:`bind_legacy_views`),
    when another integration owns the routes, or when legacy OAuth was never
    bound this session. Callers use this to withhold rotated credentials from
    surfaces a still-valid old-identity token can read — the admin startup log
    in particular, which is reachable through the server's own log tools
    (review finding on #1880).
    """
    if hass.data.get(OAUTH_ROUTE_OWNER_KEY) != _DOMAIN:
        return False
    bound = hass.data.get(OAUTH_ROUTE_KEY_FINGERPRINT)
    if not isinstance(bound, str):
        return False
    current = _oauth_route_fingerprint(
        client_id, client_secret, _normalize_signing_key(signing_key)
    )
    return hmac.compare_digest(bound, current)


def legacy_restart_pending(hass: HomeAssistant) -> bool:
    """Whether the root views were bound mid-session and are not live until a
    restart (see :func:`bind_legacy_views`). Distinct from
    :func:`legacy_credentials_active`: at a mid-session FIRST enable the bound
    views serve exactly the current credentials (active is True) yet
    ``/authorize`` is not live until the pending restart — the admin surfaces
    (options hint, startup log) use this to caveat credentials that are
    correct but not yet serving."""
    return bool(hass.data.get(_LEGACY_PENDING_RESTART_KEY))


def _live_auth_mode(hass: HomeAssistant) -> str | None:
    """Read the CURRENTLY configured webhook auth mode from hass.data.

    Deferred import to avoid a module cycle: mcp_webhook imports this module
    for the views/provider it registers, so this module cannot import
    mcp_webhook at load time.
    """
    from .mcp_webhook import active_auth_mode

    return active_auth_mode(hass)


# ---------------------------------------------------------------------------
# Small helpers (ported from the add-on's oauth.py)
# ---------------------------------------------------------------------------


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _is_loopback_host(hostname: str) -> bool:
    """True for the loopback hosts RFC 8252 §7.3/§8.3 allows over plain http."""
    if hostname in _LOOPBACK_HOSTNAMES:
        return True
    try:
        # Covers all of 127.0.0.0/8 and ::1, not just the literal 127.0.0.1.
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _is_valid_redirect_uri(redirect_uri: str) -> bool:
    """Spec-floor validation for OAuth redirect_uri: an https:// URL — or an
    http:// loopback URL (RFC 8252 §7.3, for native/CLI clients) — with a
    non-empty host, a valid port, and no fragment. Single-tenant — no per-client
    allowlist, but reject the obvious bad shapes that would let an attacker
    direct the flow to an empty/malformed URL."""
    if not redirect_uri:
        return False
    try:
        parsed = urlparse(redirect_uri)
        # Accessing .port validates it: urlparse defers the range/format check
        # until access, so a crafted ':999999' or ':abc' port raises ValueError
        # HERE (→ clean 400) instead of later in yarl inside _redirect_with,
        # where it would escape as an uncaught 500 on an unauthenticated view.
        _ = parsed.port
    except ValueError:
        return False
    if not parsed.hostname:
        return False
    if parsed.scheme == "http":
        # Plain http only for loopback callbacks (native-client flow).
        if not _is_loopback_host(parsed.hostname):
            return False
    elif parsed.scheme != "https":
        return False
    # Fragments are not allowed in OAuth redirect URIs (RFC 6749 §3.1.2).
    return not parsed.fragment


def _text_error(
    status: int, message: str, *, restart_hint: bool = False
) -> web.Response:
    """Plain-text error response. ``restart_hint`` appends ``_RESTART_HINT`` —
    set it only for the stale-registration cases a full HA restart actually
    unsticks (invalid client_id), not for client-side request mistakes."""
    text = f"{message}. {_RESTART_HINT}" if restart_hint else message
    return web.Response(status=status, text=text)


def _json_error(
    error: str,
    status: int,
    headers: dict[str, str] | None = None,
    *,
    restart_hint: bool = False,
) -> web.Response:
    """OAuth JSON error response. ``restart_hint`` carries ``_RESTART_HINT`` in
    ``error_description`` — set it only for the stale-registration case
    (``invalid_client``), not client-side protocol errors."""
    body = {"error": error}
    if restart_hint:
        body["error_description"] = _RESTART_HINT
    return web.json_response(body, status=status, headers=headers)


def _json_not_found() -> web.Response:
    """404 for a root view whose route is disabled in the active mode, or
    whose bound provider is stale (see LegacyOAuthProvider.is_active)."""
    return web.json_response({"error": "not_found"}, status=404)


class _PendingCode(TypedDict):
    """Shape of an entry in LegacyOAuthProvider._codes. TypedDict so a typo on
    one of these keys fails type-check rather than silently treating it as
    missing."""

    redirect_uri: str
    code_challenge: str
    expires: float


# ---------------------------------------------------------------------------
# LegacyOAuthProvider
# ---------------------------------------------------------------------------


class LegacyOAuthProvider:
    """Holds legacy-OAuth state: token issue/validate, PKCE codes, client auth.

    Constructed once per bind (see :func:`bind_legacy_views`), not once per
    config-entry reload — see that function's docstring for why. Holds no
    reference to ``hass``; ``active_mode_getter`` is how the bound root views
    learn whether legacy is STILL the live mode on each request (a reload that
    switches away leaves this same instance bound but inactive — see
    :meth:`is_active`).
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        signing_key: bytes | str,
        active_mode_getter: Callable[[], str | None],
    ) -> None:
        if not client_id:
            raise ValueError("client_id must be a non-empty string")
        if not client_secret:
            raise ValueError("client_secret must be a non-empty string")
        key_bytes = _normalize_signing_key(signing_key)
        if len(key_bytes) < 32:
            raise ValueError("signing_key must be at least 32 bytes")
        self._client_id = client_id
        self._client_secret = client_secret
        self._signing_key = key_bytes
        self._active_mode_getter = active_mode_getter
        # In-memory pending authorization codes. Codes are short-lived (5 min)
        # and one-shot; a restart wipes them, which only forces in-flight
        # authorize/token round-trips to retry.
        self._codes: dict[str, _PendingCode] = {}

    @property
    def client_id(self) -> str:
        return self._client_id

    def is_active(self) -> bool:
        """True iff legacy is the CURRENTLY configured + live webhook auth mode."""
        return self._active_mode_getter() == WEBHOOK_AUTH_LEGACY

    # -----------------------------------------------------------------
    # Token issuance / validation
    # -----------------------------------------------------------------

    def _issue_token(self, kind: str, ttl: int) -> str:
        now = int(time.time())
        payload = {
            "kind": kind,
            "iat": now,
            "exp": now + ttl,
            "jti": secrets.token_urlsafe(12),
            "cid": self._client_id,
        }
        body = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
        sig = hmac.new(self._signing_key, body.encode("ascii"), hashlib.sha256).digest()
        return f"{body}.{_b64url_encode(sig)}"

    def _validate_token(self, token: str, expected_kind: str) -> bool:
        try:
            body, sig_part = token.rsplit(".", 1)
        except ValueError:
            return False
        try:
            actual_sig = _b64url_decode(sig_part)
            # body.encode("ascii") is inside the try: a bearer whose
            # pre-signature segment carries a non-ASCII char raises
            # UnicodeEncodeError, which must be caught here (return False)
            # rather than escaping the webhook gate.
            expected_sig = hmac.new(
                self._signing_key, body.encode("ascii"), hashlib.sha256
            ).digest()
        except (ValueError, binascii.Error, UnicodeEncodeError):
            return False
        if not hmac.compare_digest(actual_sig, expected_sig):
            return False
        try:
            payload = json.loads(_b64url_decode(body))
        except (ValueError, json.JSONDecodeError):
            return False
        if not isinstance(payload, dict):
            return False
        if payload.get("kind") != expected_kind:
            return False
        if payload.get("cid") != self._client_id:
            # Token was issued for a previous client_id config — reject so a
            # client_id rotation revokes outstanding tokens once the restart
            # binds a provider carrying the new identity. Pre-restart the
            # bound provider still holds the OLD client_id, so old tokens
            # keep validating until then (see bind_legacy_views).
            return False
        # Valid up to but not including `exp` (RFC 7519 §4.1.4 convention).
        return bool(payload.get("exp", 0) > int(time.time()))

    def issue_access_token(self) -> str:
        return self._issue_token(TOKEN_KIND_ACCESS, ACCESS_TOKEN_TTL)

    def issue_refresh_token(self) -> str:
        return self._issue_token(TOKEN_KIND_REFRESH, REFRESH_TOKEN_TTL)

    def validate_access_token(self, token: str) -> bool:
        return self._validate_token(token, TOKEN_KIND_ACCESS)

    def validate_refresh_token(self, token: str) -> bool:
        return self._validate_token(token, TOKEN_KIND_REFRESH)

    def validate_bearer(self, request: web.Request) -> bool:
        header = request.headers.get("Authorization", "")
        if not header.lower().startswith("bearer "):
            return False
        token = header[7:].strip()
        return self.validate_access_token(token)

    # -----------------------------------------------------------------
    # Authorization codes (PKCE)
    # -----------------------------------------------------------------

    def issue_code(self, redirect_uri: str, code_challenge: str) -> str | None:
        """Issue a one-shot authorization code, or None if the pending-code
        store is at capacity (signals an abuse attempt — see MAX_PENDING_CODES)."""
        now = time.time()
        self._codes = {k: v for k, v in self._codes.items() if v["expires"] > now}
        if len(self._codes) >= MAX_PENDING_CODES:
            _LOGGER.warning(
                "HA-MCP legacy OAuth: pending-code store at cap (%d); refusing "
                "new issuance until existing codes expire or are consumed.",
                MAX_PENDING_CODES,
            )
            return None
        code = secrets.token_urlsafe(32)
        self._codes[code] = {
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "expires": now + AUTH_CODE_TTL,
        }
        return code

    def consume_code(self, code: str, redirect_uri: str, code_verifier: str) -> bool:
        # Validate the verifier shape per RFC 7636 §4.1 before doing any
        # crypto. A confused client passing an empty/short verifier should be
        # rejected explicitly rather than silently hashing junk.
        if not (PKCE_VERIFIER_MIN <= len(code_verifier) <= PKCE_VERIFIER_MAX):
            return False
        if not _PKCE_VERIFIER_RE.match(code_verifier):
            return False
        entry = self._codes.pop(code, None)
        if entry is None:
            return False
        if entry["expires"] < time.time():
            return False
        if entry["redirect_uri"] != redirect_uri:
            return False
        # PKCE S256 verification: SHA-256(verifier) base64url(no pad) == challenge
        derived = _b64url_encode(hashlib.sha256(code_verifier.encode()).digest())
        return hmac.compare_digest(
            derived.encode("ascii"), entry["code_challenge"].encode("ascii")
        )

    # -----------------------------------------------------------------
    # Client authentication
    # -----------------------------------------------------------------

    def authenticate_client(
        self, client_id: str | None, client_secret: str | None
    ) -> bool:
        if not client_id or not client_secret:
            return False
        return hmac.compare_digest(
            client_id.encode(), self._client_id.encode()
        ) and hmac.compare_digest(client_secret.encode(), self._client_secret.encode())


# ---------------------------------------------------------------------------
# Views (root /authorize + /token)
# ---------------------------------------------------------------------------


class AuthorizeView(HomeAssistantView):
    """OAuth /authorize endpoint with a minimal consent page."""

    requires_auth = False
    url = AUTHORIZE_PATH
    name = "ha_mcp_tools:oauth:authorize"

    def __init__(self, provider: LegacyOAuthProvider) -> None:
        self._provider = provider

    @staticmethod
    def _redirect_with(redirect_uri: str, **params: str) -> web.Response:
        # yarl ships with aiohttp and handles existing-query-string merging
        # plus parameter encoding correctly — safer than hand-rolling.
        import yarl

        url = yarl.URL(redirect_uri).update_query(params)
        return web.Response(status=302, headers={"Location": str(url)})

    async def get(self, request: web.Request) -> web.Response:
        if not self._provider.is_active():
            # Serve ONLY while legacy is the live mode. Both ha_auth/none (HA
            # core or the secret URL is the authority) and a not-yet-live
            # entry mean this root view must not serve — HA can't rebind or
            # drop it without a restart, so a mode switch away leaves it
            # bound. Refuse it.
            return _text_error(404, "not found")
        params = request.query
        client_id = params.get("client_id", "")
        redirect_uri = params.get("redirect_uri", "")
        state = params.get("state", "")
        code_challenge = params.get("code_challenge", "")
        code_challenge_method = params.get("code_challenge_method", "")
        response_type = params.get("response_type", "")

        err = self._validate_authorize_params(
            response_type=response_type,
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
        )
        if err is not None:
            return err

        # Render minimal consent page. Showing the redirect_uri lets the user
        # verify the flow goes back to a domain they recognize.
        html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Authorize MCP Connector</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 36rem; margin: 4rem auto; padding: 0 1rem; }}
    code {{ background: #f4f4f4; padding: 2px 6px; border-radius: 3px; word-break: break-all; }}
    button {{ padding: 0.5rem 1rem; font-size: 1rem; margin-right: 0.5rem; }}
    .approve {{ background: #2563eb; color: white; border: none; }}
    .deny {{ background: #e5e7eb; color: #111; border: none; }}
  </style>
</head>
<body>
  <h1>Authorize MCP Connector</h1>
  <p>An MCP client is requesting access to your Home Assistant MCP server.</p>
  <p>It will redirect to:<br><code>{escape(redirect_uri)}</code></p>
  <p>Only allow this if you started this connection yourself.</p>
  <form method="POST" action="{AUTHORIZE_PATH}">
    <input type="hidden" name="client_id" value="{escape(client_id)}">
    <input type="hidden" name="redirect_uri" value="{escape(redirect_uri)}">
    <input type="hidden" name="state" value="{escape(state)}">
    <input type="hidden" name="code_challenge" value="{escape(code_challenge)}">
    <button class="approve" type="submit" name="action" value="approve">Allow</button>
    <button class="deny" type="submit" name="action" value="deny">Deny</button>
  </form>
</body>
</html>"""
        return web.Response(text=html, content_type="text/html")

    async def post(self, request: web.Request) -> web.Response:
        if not self._provider.is_active():
            return _text_error(404, "not found")
        data = await request.post()
        action = str(data.get("action", ""))
        client_id = str(data.get("client_id", ""))
        redirect_uri = str(data.get("redirect_uri", ""))
        state = str(data.get("state", ""))
        code_challenge = str(data.get("code_challenge", ""))

        # Re-validate everything from the form — never trust hidden fields.
        # response_type/method aren't carried on the POST so we hard-code the
        # spec values here; the validator still applies all the same rules to
        # the user-influenceable fields.
        err = self._validate_authorize_params(
            response_type="code",
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            code_challenge_method="S256",
        )
        if err is not None:
            return err

        if action == "deny":
            return self._redirect_with(redirect_uri, error="access_denied", state=state)
        if action != "approve":
            return _text_error(400, "invalid action")

        code = self._provider.issue_code(redirect_uri, code_challenge)
        if code is None:
            # Pending-code store at cap → signal back per RFC 6749 §4.1.2.1
            # instead of silently failing.
            return self._redirect_with(
                redirect_uri, error="temporarily_unavailable", state=state
            )
        return self._redirect_with(redirect_uri, code=code, state=state)

    def _validate_authorize_params(
        self,
        *,
        response_type: str,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        code_challenge_method: str,
    ) -> web.Response | None:
        """Return a 400 web.Response if any /authorize param is invalid, or
        None if all checks pass. Centralized so GET and POST share identical
        validation — the POST path explicitly re-validates the hidden form
        fields rather than trusting them."""
        if response_type != "code":
            return _text_error(400, "unsupported_response_type")
        if code_challenge_method != "S256":
            return _text_error(400, "invalid code_challenge_method (S256 required)")
        if not _PKCE_CHALLENGE_RE.match(code_challenge):
            return _text_error(
                400, "invalid code_challenge (must be 43-char base64url)"
            )
        if client_id != self._provider.client_id:
            return _text_error(400, "invalid client_id", restart_hint=True)
        if not _is_valid_redirect_uri(redirect_uri):
            return _text_error(
                400,
                "redirect_uri must be an https:// URL (or an http:// loopback "
                "URL) with a valid host and port",
            )
        return None


class TokenView(HomeAssistantView):
    """OAuth /token endpoint: authorization_code + refresh_token grants."""

    requires_auth = False
    cors_allowed = True
    url = TOKEN_PATH
    name = "ha_mcp_tools:oauth:token"

    def __init__(self, provider: LegacyOAuthProvider) -> None:
        self._provider = provider

    @staticmethod
    def _extract_client_creds(
        request: web.Request, form: dict
    ) -> tuple[str | None, str | None]:
        """Pull client_id/secret from Basic auth header OR form body."""
        header = request.headers.get("Authorization", "")
        if header.lower().startswith("basic "):
            try:
                decoded = base64.b64decode(header[6:].strip(), validate=True).decode(
                    "utf-8"
                )
            except (ValueError, UnicodeDecodeError, binascii.Error):
                return None, None
            if ":" in decoded:
                cid, _, sec = decoded.partition(":")
                # RFC 6749 §2.3.1: client_secret_basic values are
                # application/x-www-form-urlencoded before base64, so decode
                # them back with unquote_PLUS ("+" means space in that
                # encoding — plain unquote would leave it literal). A no-op for
                # the generated credentials (URL-safe alphabets, nothing to
                # decode) but required for custom overrides containing reserved
                # characters — matches the form-body path below, which aiohttp
                # also form-decodes ("+" → space).
                return unquote_plus(cid), unquote_plus(sec)
            return None, None
        return form.get("client_id"), form.get("client_secret")

    async def post(self, request: web.Request) -> web.Response:
        if not self._provider.is_active():
            return _json_not_found()
        form = dict(await request.post())
        client_id, client_secret = self._extract_client_creds(request, form)
        if not self._provider.authenticate_client(client_id, client_secret):
            return _json_error(
                "invalid_client",
                401,
                headers={"WWW-Authenticate": 'Basic realm="HA-MCP OAuth"'},
                restart_hint=True,
            )

        grant_type = form.get("grant_type", "")
        if grant_type == "authorization_code":
            return await self._handle_authorization_code(form)
        if grant_type == "refresh_token":
            return await self._handle_refresh(form)
        return _json_error("unsupported_grant_type", 400)

    async def _handle_authorization_code(self, form: dict) -> web.Response:
        code = str(form.get("code", ""))
        redirect_uri = str(form.get("redirect_uri", ""))
        code_verifier = str(form.get("code_verifier", ""))
        if not (code and redirect_uri and code_verifier):
            return _json_error("invalid_request", 400)
        if not self._provider.consume_code(code, redirect_uri, code_verifier):
            return _json_error("invalid_grant", 400)
        return web.json_response(
            {
                "access_token": self._provider.issue_access_token(),
                "token_type": "Bearer",
                "expires_in": ACCESS_TOKEN_TTL,
                "refresh_token": self._provider.issue_refresh_token(),
            },
            headers=_TOKEN_RESPONSE_HEADERS,
        )

    async def _handle_refresh(self, form: dict) -> web.Response:
        refresh = str(form.get("refresh_token", ""))
        if not refresh or not self._provider.validate_refresh_token(refresh):
            return _json_error("invalid_grant", 400)
        return web.json_response(
            {
                "access_token": self._provider.issue_access_token(),
                "token_type": "Bearer",
                "expires_in": ACCESS_TOKEN_TTL,
                "refresh_token": self._provider.issue_refresh_token(),
            },
            headers=_TOKEN_RESPONSE_HEADERS,
        )
