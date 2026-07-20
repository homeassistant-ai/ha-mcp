"""OAuth 2.1 provider for the MCP Webhook Proxy.

This module is lazy-imported by `__init__.py` ONLY when the user has
enabled the OAuth toggle. When OAuth is off the import never runs and the
proxy behaves exactly like a vanilla unauthenticated webhook.

Implements the subset of OAuth 2.1 required by the MCP spec:
- Authorization-code grant with PKCE (S256)
- Client authentication via client_secret_basic OR client_secret_post
- Refresh tokens
- RFC 8414 Authorization Server Metadata
- RFC 9728 Protected Resource Metadata
- WWW-Authenticate: Bearer with resource_metadata pointer (so MCP clients
  discover the auth server from a 401 on the webhook URL)

Single-tenant by design: one client_id / client_secret pair, configured in
the addon. The consent screen displays the requesting redirect_uri so the
user can verify they're authorizing the connector they meant to.

Tokens are signed (HMAC-SHA256) with a per-install secret persisted at
/config/.mcp_proxy_dev_oauth_secret. They contain enough state to validate
without a server-side store, so the integration survives restarts.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from html import escape
from pathlib import Path
from typing import Protocol, TypedDict
from urllib.parse import urlparse

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

OAUTH_BASE = "/api/mcp_proxy_dev/oauth"
# Authorize/token endpoints live at the root rather than under OAUTH_BASE for
# the legacy flow's original motivation: an MCP client that builds
# `<host>/authorize` from the resource host root instead of reading the
# `authorization_endpoint` metadata field (observed with Google Gemini Spark).
# claude.ai DOES honor the advertised `authorization_endpoint` — issue #1969's
# none-mode auto-approve server advertises OAUTH_BASE `/authorize` (see
# oauth_autoapprove) and claude.ai calls exactly that, proven live — so root
# registration is for the metadata-ignoring clients, not a claude.ai requirement.
AUTHORIZE_PATH = "/authorize"
TOKEN_PATH = "/token"
SECRET_FILE = Path("/config/.mcp_proxy_dev_oauth_secret")

# Shared hass.data key for the integration (matches __init__.py DOMAIN). The
# mode-aware discovery-document views read the ACTIVE OAuth mode from
# hass.data[DOMAIN] at request time (see _active_oauth_mode) so the SAME
# registered view instances serve whichever mode is live now.
DOMAIN = "mcp_proxy_dev"

# TOP-LEVEL hass.data key (deliberately NOT under DOMAIN, so it survives
# async_unload_entry's hass.data.pop(DOMAIN)) recording that the seven
# discovery-document metadata views are bound to aiohttp for this HA session.
# HA registers views with no route name and leaves the router unfrozen, so a
# duplicate registration does NOT raise — aiohttp lets the first-registered
# path win and silently shadows the later one; HA also can't unregister a bound
# view until it restarts. So register_metadata_views must bind the seven at
# most once: a same-mode ha_auth reload, a legacy->ha_auth switch, or a
# ha_auth->legacy switch all reuse the already-bound views instead of stacking
# silently-shadowed duplicate routes on every reload/switch.
#
# Suffixed with DOMAIN so each add-on flavor gets its OWN top-level flag: the
# metadata-view URLs and names embed the flavor's DOMAIN and therefore never
# collide across flavors, so one flavor's flag must not
# suppress the other flavor's (non-colliding) registration if both run ha_auth
# once this dev code promotes to stable.
_METADATA_VIEWS_REGISTERED_KEY = (
    f"webhook_proxy_oauth_metadata_views_registered_{DOMAIN}"
)

# OAuth mode markers. Mirrored as auth_native.HA_AUTH_MODE and __init__.py's
# OAUTH_MODE_* (a test pins them in agreement). ha_auth = HA core is the
# authorization server; legacy = this module's embedded authorization server;
# none_autoapprove = OAuth off, but we still serve our own corrected discovery +
# an invisible auto-approve authorization server so claude.ai's flaky discovery
# resolves against us instead of HA core's broken root doc (issue #1969, dev-only
# — see oauth_autoapprove).
MODE_HA_AUTH = "ha_auth"
MODE_LEGACY = "legacy"
MODE_NONE_AUTOAPPROVE = "none_autoapprove"

# hass.data[DOMAIN] key holding the live none-mode AutoApproveProvider (issue
# #1969). Stored under its OWN key — NOT "oauth" — so the webhook forwarder's
# bearer gate (which keys off "oauth") stays off in none mode: the secret
# webhook URL is the credential and the auto-approve token is cosmetic.
AUTOAPPROVE_PROVIDER_KEY = "autoapprove"

ACCESS_TOKEN_TTL = 60 * 60  # 1 hour
REFRESH_TOKEN_TTL = 30 * 24 * 60 * 60  # 30 days
AUTH_CODE_TTL = 5 * 60  # 5 minutes
TOKEN_KIND_ACCESS = "access"
TOKEN_KIND_REFRESH = "refresh"

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

# Minimum client_id length — also enforced in start.py before the addon
# writes the proxy config, but duplicated here so the type itself rejects
# misconfiguration if a future caller forgets the up-front check.
MIN_CLIENT_ID_LEN = 16

# Appended ONLY to the stale-OAuth-registration errors (invalid_client /
# invalid client_id) via the _text_error/_json_error restart_hint flag. The OAuth
# provider's HTTP views are bound at register_views() time and HA can't
# re-register / drop them mid-session, so a client-id regenerate / OAuth toggle /
# reinstall only takes effect on a full HA restart — the case this targets
# (issue #1694). Deliberately NOT added to client-side protocol errors
# (invalid_grant / invalid_request / unsupported_grant_type) or the webhook
# 502/500 paths, where a restart is not the fix. (The webhook itself is
# re-registered on reload; only the OAuth views need the restart.)
RESTART_HINT = (
    "If this persists, fully restart Home Assistant "
    "(Settings -> System -> Restart) — not just the add-on or the integration."
)


def _text_error(
    status: int, message: str, *, restart_hint: bool = False
) -> web.Response:
    """Plain-text error response.

    ``restart_hint`` appends ``RESTART_HINT`` — set it only for the
    stale-OAuth-registration cases (``invalid client_id``) a full HA restart
    actually unsticks, not for client-side request mistakes.
    """
    text = f"{message}. {RESTART_HINT}" if restart_hint else message
    return web.Response(status=status, text=text)


def _json_error(
    error: str,
    status: int,
    headers: dict[str, str] | None = None,
    *,
    restart_hint: bool = False,
) -> web.Response:
    """OAuth JSON error response.

    ``restart_hint`` carries ``RESTART_HINT`` in ``error_description`` — set it
    only for the stale-registration case (``invalid_client``), not for
    client-side protocol errors (``invalid_grant`` / ``invalid_request`` /
    ``unsupported_grant_type``) where a restart is not the fix.
    """
    body = {"error": error}
    if restart_hint:
        body["error_description"] = RESTART_HINT
    return web.json_response(body, status=status, headers=headers)


def _json_not_found() -> web.Response:
    """404 for an OAuth view whose integration data is gone (the config entry
    was unloaded but HA can't drop the bound view until a restart) or whose
    route is disabled in the active mode. MCP clients fall through their
    discovery chain on a 404, so this matches the not-registered behavior."""
    return web.json_response({"error": "not_found"}, status=404)


class _PendingCode(TypedDict):
    """Shape of an entry in OAuthProvider._codes. TypedDict so a typo on
    one of these keys (`expires`/`redirect_uri`/`code_challenge`) fails
    type-check rather than silently treating it as missing."""

    redirect_uri: str
    code_challenge: str
    expires: float


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _atomic_write_0600(path: Path, data: bytes) -> bool:
    """Write ``data`` to ``path`` with 0600, atomically and race-free.

    Writes to a sibling temp file created 0600 (the mode is applied by the
    ``open()`` syscall, so there is no chmod-after-write window), then
    ``os.replace()``s it over ``path``. Because the replace is atomic and the
    temp was 0600 from creation, ``path`` is never observable with the new
    bytes at wider-than-0600 permissions — on first create OR on an
    overwrite/regenerate. Returns True on success; returns False if the
    restricted-mode create OR the subsequent write/replace failed — i.e. on
    ANY OSError, whether the filesystem can't honor restricted mode (tmpfs /
    non-POSIX) or a genuine I/O error occurred (full disk, EACCES, read-only
    fs). A False therefore doesn't prove it was only the mode: the caller's
    plain-write fallback also fails on a hard I/O error, so its "wider
    permissions" warning must not be treated as certain.
    """
    tmp = path.with_name(f"{path.name}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                # Best-effort temp cleanup — if unlink fails the stale ".tmp"
                # is harmless and the caller's fallback path still runs.
                pass
            raise
        return True
    except OSError:
        return False


def load_or_create_secret() -> bytes:
    """Persist a 32-byte signing secret across restarts.

    Public (no leading underscore) because callers in `__init__.py` invoke
    it via `hass.async_add_executor_job` — HA forbids blocking filesystem
    I/O on the event loop, so the OAuth provider receives the loaded key
    rather than loading it itself in `__init__`.
    """
    if SECRET_FILE.exists():
        data = SECRET_FILE.read_bytes()
        if len(data) >= 32:
            return data
        # File exists but is short — partial write, truncation, or
        # corruption from a prior run. Regenerating it here invalidates
        # every previously-issued token. Log loudly so a confused user
        # who suddenly sees their MCP client lose access can find the
        # cause in the addon log instead of debugging blind.
        _LOGGER.warning(
            "MCP Proxy OAuth: existing signing key at %s is shorter than "
            "32 bytes (got %d). Regenerating — ALL previously issued "
            "OAuth tokens are now invalid; MCP clients will need to "
            "re-authorize.",
            SECRET_FILE,
            len(data),
        )
    new_secret = secrets.token_bytes(32)
    SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not _atomic_write_0600(SECRET_FILE, new_secret):
        # False = the restricted-mode create OR the write/replace failed (any
        # OSError: tmpfs / non-POSIX that can't honor 0600, or a hard I/O error
        # like a full disk / EACCES / read-only fs). Fall back to a plain
        # write: on a mode-only limitation it succeeds (the secret may end up
        # world-readable in /config — hence the warning); on a hard I/O error
        # it raises and propagates.
        SECRET_FILE.write_bytes(new_secret)
        _LOGGER.warning(
            "MCP Proxy OAuth: could not create the signing key file with "
            "restricted permissions at %s. The key may have wider permissions "
            "than intended.",
            SECRET_FILE,
        )
    return new_secret


def _is_valid_redirect_uri(redirect_uri: str) -> bool:
    """Spec-floor validation for OAuth redirect_uri: must be an https:// URL
    with a non-empty host and no fragment. Single-tenant addon — we don't
    maintain a per-client allowlist, but reject the obvious bad shapes that
    would let an attacker direct the auth flow to an empty/malformed URL."""
    if not redirect_uri:
        return False
    try:
        parsed = urlparse(redirect_uri)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    if not parsed.hostname:
        return False
    # Fragments are not allowed in OAuth redirect URIs (RFC 6749 §3.1.2).
    return not parsed.fragment


def _build_base_url(request: web.Request, public_base_url: str | None = None) -> str:
    """Build the public base URL used in OAuth metadata and redirects.

    When `public_base_url` is provided (the operator-configured
    `remote_url`/Nabu Casa URL written into proxy_config by start.py),
    it wins and per-request headers are ignored. This pins canonical
    URLs to the operator's intent and prevents an attacker who can hit
    the addon via a forged Host header from poisoning the metadata.

    Falls back to X-Forwarded-Proto/Host or request.scheme/Host when
    no public base URL is configured (e.g. cloudflared/custom proxy
    setups where start.py couldn't auto-detect the public URL).
    """
    if public_base_url:
        return public_base_url.rstrip("/")
    host = request.headers.get("X-Forwarded-Host") or request.headers.get("Host", "")
    scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
    return f"{scheme}://{host}"


class MetadataProvider(Protocol):
    """Interface the mode-aware discovery-document views need from a provider.

    Satisfied structurally by both `OAuthProvider` (legacy) and
    `auth_native.ResourceServer` (ha_auth). The views additionally read the
    implementation's `_hass` via ``getattr`` (see `_active_oauth_mode` /
    `_active_provider`), which a Protocol cannot express for a private
    attribute — both implementations carry it.
    """

    @property
    def webhook_id(self) -> str:
        """This install's private webhook id."""

    def resource_url(self, base_url: str) -> str:
        """Absolute URL of the protected webhook resource under ``base_url``."""

    def authorization_server_url(self, base_url: str) -> str:
        """Issuer / authorization-server URL under ``base_url``."""

    def base_url_for(self, request: web.Request) -> str:
        """Public base URL for ``request`` per the provider's policy
        (legacy: pinned to the configured URL; ha_auth: request-host-derived)."""


def _active_oauth_mode(provider: object) -> str | None:
    """Return the OAuth mode currently active for this integration.

    Read live from hass.data so the SAME registered view instances serve
    whichever mode is active now — e.g. a legacy-bound view after the operator
    switched the add-on to ha_auth, which Home Assistant cannot rebind without a
    restart. Returns ``MODE_HA_AUTH``, ``MODE_LEGACY``, or
    ``MODE_NONE_AUTOAPPROVE`` (none-mode auto-approve, issue #1969), or ``None``
    when no mode is live — the integration data is gone (the config entry was
    unloaded)
    or present without an ``oauth_mode`` key (reloaded with OAuth turned off) —
    so a stale view can 404 like an unregistered route; HA can't drop the bound
    views until a restart either way. A non-dict ``hass.data[DOMAIN]`` (only
    happens with test doubles) defaults to ``MODE_LEGACY``, the pre-ha_auth
    behavior.
    """
    hass = getattr(provider, "_hass", None)
    domain_data = hass.data.get(DOMAIN) if hass is not None else None
    if domain_data is None:
        return None
    if not isinstance(domain_data, dict):
        return MODE_LEGACY
    return domain_data.get("oauth_mode")


def _active_provider(bound_provider: MetadataProvider) -> MetadataProvider:
    """Return the provider whose base-URL policy is active right now.

    After a live mode switch the still-bound view instances were constructed
    with the PREVIOUS mode's provider (HA can't rebind views mid-session), and
    the two modes build URLs differently: legacy pins the base URL to the
    operator-configured public_base_url (Host-poisoning resistance) while
    ha_auth derives it from the request host (the same install must work via
    any hostname). Resolving ``hass.data[DOMAIN]["oauth"]`` at request time
    applies whichever policy is live; falls back to the bound provider when the
    domain data is absent (test doubles — the unloaded case already 404s in the
    views before any URL is built).
    """
    hass = getattr(bound_provider, "_hass", None)
    domain_data = hass.data.get(DOMAIN) if hass is not None else None
    if isinstance(domain_data, dict):
        # ha_auth/legacy store their provider under "oauth"; none-mode
        # auto-approve (issue #1969) stores its MetadataProvider under
        # AUTOAPPROVE_PROVIDER_KEY (never "oauth", so the bearer gate stays
        # off). The two are mutually exclusive, so this resolves the live one.
        active: MetadataProvider | None = domain_data.get("oauth") or domain_data.get(
            AUTOAPPROVE_PROVIDER_KEY
        )
        if active is not None:
            return active
    return bound_provider


class PKCECodeStore:
    """In-memory PKCE (S256) authorization-code store.

    Shared by `OAuthProvider` (legacy) and the none-mode auto-approve server
    (`oauth_autoapprove`, issue #1969) so the one-shot code lifecycle — issue at
    `/authorize`, verify + consume at `/token` — has a single implementation
    instead of two copies. Codes are short-lived (`AUTH_CODE_TTL`), one-shot,
    bound to the `redirect_uri` + `code_challenge` presented at issuance, and
    capped (`MAX_PENDING_CODES`) with an expiry prune on each issue. A restart
    wipes the store, which only forces in-flight authorize/token round-trips to
    retry.
    """

    def __init__(self) -> None:
        self._codes: dict[str, _PendingCode] = {}

    def issue_code(self, redirect_uri: str, code_challenge: str) -> str | None:
        """Issue a one-shot authorization code, or return None if the
        pending-code store is at capacity (which signals an abuse attempt
        — see MAX_PENDING_CODES)."""
        # Prune expired entries — bounds dict size to O(active codes)
        # between abusive bursts; the cap below handles the burst case.
        now = time.time()
        self._codes = {k: v for k, v in self._codes.items() if v["expires"] > now}
        if len(self._codes) >= MAX_PENDING_CODES:
            _LOGGER.warning(
                "MCP Proxy OAuth: pending-code store at cap (%d); refusing "
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
        """One-shot consume ``code``, verifying its PKCE S256 challenge.

        Returns True only for a live, unexpired code whose stored
        ``redirect_uri`` matches and whose ``code_challenge`` equals
        ``base64url(SHA-256(code_verifier))``. The code is popped (one-shot)
        once the verifier clears the cheap RFC 7636 shape guard below, so every
        well-formed attempt burns it — a wrong verifier, expired code, or
        redirect mismatch still consumes the code. A malformed verifier is
        rejected before the pop and does NOT burn the code, so a client that
        sends a syntactically broken verifier can retry.
        """
        # Validate the verifier shape per RFC 7636 §4.1 before doing any
        # crypto. A confused client passing an empty/short verifier should
        # be rejected explicitly rather than silently hashing junk.
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
            derived.encode("ascii"),
            entry["code_challenge"].encode("ascii"),
        )


class OAuthProvider:
    """Holds OAuth state and registers HA HTTP views.

    Only constructed when the addon's enable_oauth toggle is on AND
    client_id/client_secret are non-empty. When neither is true,
    `__init__.py` never imports this module — keeping the OFF code path
    behaviorally identical to the original unauthenticated proxy.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        client_id: str,
        client_secret: str,
        webhook_id: str,
        signing_key: bytes,
        public_base_url: str | None = None,
    ) -> None:
        if not client_id or len(client_id) < MIN_CLIENT_ID_LEN:
            raise ValueError(
                f"client_id must be a non-empty string at least "
                f"{MIN_CLIENT_ID_LEN} characters long"
            )
        if not client_secret:
            raise ValueError("client_secret must be a non-empty string")
        if len(signing_key) < 32:
            raise ValueError("signing_key must be at least 32 bytes")
        self._hass = hass
        self._client_id = client_id
        self._client_secret = client_secret
        self._webhook_id = webhook_id
        self._public_base_url = public_base_url
        # Loaded by the caller via `hass.async_add_executor_job` — HA's
        # event loop must not be blocked with sync filesystem I/O during
        # integration setup.
        self._signing_key = signing_key
        # PKCE authorization codes live in the shared store (also used by the
        # none-mode auto-approve server) — see PKCECodeStore.
        self._code_store = PKCECodeStore()

    @property
    def client_id(self) -> str:
        return self._client_id

    @property
    def webhook_id(self) -> str:
        return self._webhook_id

    def client_id_masked(self) -> str:
        if len(self._client_id) <= 4:
            return "***"
        return self._client_id[:3] + "..." + self._client_id[-2:]

    def resource_url(self, base_url: str) -> str:
        return f"{base_url}/api/webhook/{self._webhook_id}"

    def authorization_server_url(self, base_url: str) -> str:
        return f"{base_url}{OAUTH_BASE}"

    def base_url_for(self, request: web.Request) -> str:
        return _build_base_url(request, self._public_base_url)

    # -----------------------------------------------------------------
    # View registration
    # -----------------------------------------------------------------

    def register_views(self) -> None:
        """Register the legacy OAuth endpoints with HA's HTTP layer.

        Delegates the seven discovery-document views to the shared,
        flag-guarded `register_metadata_views` (which binds them at most once
        per HA session — see `_METADATA_VIEWS_REGISTERED_KEY`), then registers
        ONLY the two root views (`AuthorizeView` + `TokenView`). ha_auth mode
        registers just the seven metadata views (HA core is the authorization
        server, serving its own `/auth/authorize` + `/auth/token`, so the
        add-on binds no root views); the bare `/authorize` + `/token` here are
        this add-on's OWN legacy root views. Routing both modes through the same
        registrar means a legacy<->ha_auth switch or a same-mode reload reuses
        the already-bound seven instead of stacking silently-shadowed duplicate
        routes (a duplicate registration does not raise — aiohttp lets the
        first-registered path win — and HA can't unregister a bound view until
        it restarts). The two root views stay gated by the caller's route-owner
        / fingerprint logic in __init__.py.
        """
        register_metadata_views(self._hass, self)
        for view in (AuthorizeView(self), TokenView(self)):
            self._hass.http.register_view(view)

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
            # rather than escaping the webhook gate — HA core's
            # async_handle_webhook would swallow the exception into a 200 OK
            # and never emit the 401 discovery challenge.
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
            # Token was issued for a previous client_id config — reject so
            # rotating client_id revokes outstanding tokens.
            return False
        # Token is valid up to but not including `exp` — at the boundary
        # `now == exp`, the token has expired (matches RFC 7519 §4.1.4
        # convention used by mainstream JWT implementations).
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
        """Issue a one-shot PKCE-bound authorization code (see PKCECodeStore)."""
        return self._code_store.issue_code(redirect_uri, code_challenge)

    def consume_code(self, code: str, redirect_uri: str, code_verifier: str) -> bool:
        """Verify PKCE S256 + one-shot consume a code (see PKCECodeStore)."""
        return self._code_store.consume_code(code, redirect_uri, code_verifier)

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
# Views
# ---------------------------------------------------------------------------


class ProtectedResourceMetadataView(HomeAssistantView):
    """RFC 9728 Protected Resource Metadata (fixed, guessable path)."""

    requires_auth = False
    cors_allowed = True
    url = f"{OAUTH_BASE}/protected-resource"
    name = "mcp_proxy_dev:oauth:protected-resource"

    # SECURITY (#1976 review): this fixed path exposes ``resource:
    # <base>/api/webhook/<id>``. In none-autoapprove mode the webhook id is the
    # SOLE credential, so this ANONYMOUS, guessable path must NOT serve it there.
    # The path-scoped subclass flips this True — its URL already embeds the id,
    # so its caller must already know it (no leak).
    _serves_in_none_mode = False

    def __init__(self, provider: MetadataProvider) -> None:
        self._provider = provider

    async def get(self, request: web.Request) -> web.Response:
        # The protected-resource document has the same shape in every OAuth
        # mode; 404 when no mode is live (entry unloaded / OAuth off) so a
        # stale-bound view acts like an unregistered route. URLs are built via
        # the ACTIVE mode's provider, not the instance this view was bound with,
        # so a live mode switch also switches the base-URL policy (legacy:
        # pinned; ha_auth: host-derived).
        mode = _active_oauth_mode(self._provider)
        if mode is None:
            return _json_not_found()
        # In none-autoapprove mode only the path-scoped subclass may serve (see
        # _serves_in_none_mode above); the fixed-path view 404s to avoid leaking
        # the credential-bearing webhook id anonymously (#1976 review).
        if mode == MODE_NONE_AUTOAPPROVE and not self._serves_in_none_mode:
            return _json_not_found()
        provider = _active_provider(self._provider)
        base = provider.base_url_for(request)
        return web.json_response(
            {
                "resource": provider.resource_url(base),
                "authorization_servers": [provider.authorization_server_url(base)],
                "bearer_methods_supported": ["header"],
                "resource_documentation": (
                    "https://github.com/homeassistant-ai/ha-mcp"
                ),
            }
        )


class AuthorizationServerMetadataView(HomeAssistantView):
    """RFC 8414 Authorization Server Metadata."""

    requires_auth = False
    cors_allowed = True
    url = f"{OAUTH_BASE}/authorization-server"
    name = "mcp_proxy_dev:oauth:authorization-server"

    def __init__(self, provider: MetadataProvider) -> None:
        self._provider = provider

    async def get(self, request: web.Request) -> web.Response:
        mode = _active_oauth_mode(self._provider)
        if mode is None:
            # No mode is live (entry unloaded / OAuth off) but HA can't drop
            # this bound view until a restart — behave like an unregistered
            # route.
            return _json_not_found()
        # Build URLs via the ACTIVE mode's provider (see _active_provider): a
        # legacy-bound view serving the ha_auth document must use ha_auth's
        # host-derived base URL, and vice versa the legacy pinned one.
        provider = _active_provider(self._provider)
        base = provider.base_url_for(request)
        if mode == MODE_HA_AUTH:
            # HA core is the authorization server: advertise its /auth/* endpoints
            # + CIMD. Built in auth_native, imported lazily to avoid an import
            # cycle (auth_native imports this module at load).
            from .auth_native import authorization_server_document

            return web.json_response(authorization_server_document(base))
        if mode == MODE_NONE_AUTOAPPROVE:
            # OAuth off, but we advertise OUR OWN auto-approve endpoints under
            # OAUTH_BASE (public PKCE client + CIMD) so claude.ai's flaky
            # discovery resolves against us, not HA core's broken root doc
            # (issue #1969). Lazy import mirrors the ha_auth dispatch above.
            from .oauth_autoapprove import authorization_server_document

            return web.json_response(authorization_server_document(base))
        as_url = provider.authorization_server_url(base)
        return web.json_response(
            {
                "issuer": as_url,
                "authorization_endpoint": f"{base}{AUTHORIZE_PATH}",
                "token_endpoint": f"{base}{TOKEN_PATH}",
                "response_types_supported": ["code"],
                "grant_types_supported": [
                    "authorization_code",
                    "refresh_token",
                ],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": [
                    "client_secret_basic",
                    "client_secret_post",
                ],
            }
        )


class WellKnownProtectedResourceView(ProtectedResourceMetadataView):
    """RFC 9728 §3.1 path-scoped Protected Resource Metadata.

    Same document as `ProtectedResourceMetadataView`, served at the
    well-known location derived from the webhook resource URL
    (`/.well-known/oauth-protected-resource/api/webhook/<id>`). Captured
    live in issue #1714: when the 401's `WWW-Authenticate`
    `resource_metadata` pointer is missing (stripped by a proxy in front,
    or a transient setup window), this path is claude.ai's FIRST fallback
    probe — and when it 404s the client falls through to the HOST-ROOT
    `/.well-known/oauth-protected-resource`, which HA core itself serves
    whenever it can resolve an external URL, steering the flow into
    HA-core native OAuth (`/auth/authorize`) where the proxy's client_id
    can never work. Serving this view keeps discovery on the proxy even
    without the pointer.
    """

    name = "mcp_proxy_dev:oauth:wellknown-protected-resource"

    # Path-scoped: the URL already embeds the webhook id, so the caller must
    # already know it — serving in none-autoapprove mode leaks nothing (#1976).
    # This is the doc claude.ai's none-mode discovery actually uses.
    _serves_in_none_mode = True

    def __init__(self, provider: MetadataProvider) -> None:
        super().__init__(provider)
        # Instance-level URL: the well-known path embeds this install's
        # webhook id, which is only known at runtime.
        self.url = (
            f"/.well-known/oauth-protected-resource/api/webhook/{provider.webhook_id}"
        )


class WellKnownAuthorizationServerMetadataView(AuthorizationServerMetadataView):
    """RFC 8414 / OIDC-discovery locations for the AS metadata document.

    Same document as `AuthorizationServerMetadataView`, registered at the
    well-known URLs MCP clients actually probe for the issuer
    `<base>/api/mcp_proxy_dev/oauth` (request sequence captured live in
    issue #1714). Two findings make these load-bearing:

    * claude.ai caches a per-URL authorization config; when discovery ran
      once against a URL while the pointer was missing, the cached (wrong,
      HA-core) config survives connector delete/re-create and overrides
      every later pointer-based re-discovery — UNLESS the AS metadata
      resolves at these locations, in which case the fresh document
      overrides the cache and the connector heals with no client action.
    * A fresh URL survives these 404ing only via the client's
      origin-default `/authorize`+`/token` fallback; serving the real
      document removes that fragility (and gives PKCE-capable clients the
      `code_challenge_methods_supported` they otherwise never see).
    """

    def __init__(self, provider: MetadataProvider, url: str, name: str) -> None:
        super().__init__(provider)
        self.url = url
        self.name = name


class AuthorizeView(HomeAssistantView):
    """OAuth /authorize endpoint with a minimal consent page."""

    requires_auth = False
    url = AUTHORIZE_PATH
    name = "mcp_proxy_dev:oauth:authorize"

    def __init__(self, provider: OAuthProvider) -> None:
        self._provider = provider

    @staticmethod
    def _redirect_with(redirect_uri: str, **params: str) -> web.Response:
        # yarl ships with aiohttp and handles existing-query-string merging
        # plus parameter encoding correctly — safer than hand-rolling.
        import yarl

        url = yarl.URL(redirect_uri).update_query(params)
        return web.Response(
            status=302,
            headers={"Location": str(url)},
        )

    async def get(self, request: web.Request) -> web.Response:
        if _active_oauth_mode(self._provider) != MODE_LEGACY:
            # Serve ONLY when legacy is the live mode. Both ha_auth (HA core is
            # the authorization server on its own /auth/authorize; this bare
            # /authorize is the add-on's own legacy view) and None (entry
            # unloaded / OAuth off) mean this stale-bound root view must not
            # serve: HA can't rebind or drop root views without a restart, so a
            # legacy->ha_auth switch OR an unload leaves it bound. Refuse it.
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
        # verify the flow goes back to a domain they recognize (claude.ai etc).
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
  <h1>Authorize MCP Webhook Proxy</h1>
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
        if _active_oauth_mode(self._provider) != MODE_LEGACY:
            # Serve ONLY when legacy is live (see the GET defense above): ha_auth
            # (HA core is the authorization server on its own /auth/authorize;
            # this bare /authorize is the add-on's own legacy view) and the
            # unloaded/None case both 404.
            return _text_error(404, "not found")
        data = await request.post()
        action = str(data.get("action", ""))
        client_id = str(data.get("client_id", ""))
        redirect_uri = str(data.get("redirect_uri", ""))
        state = str(data.get("state", ""))
        code_challenge = str(data.get("code_challenge", ""))

        # Re-validate everything from the form — never trust hidden fields.
        # response_type/method aren't carried on the POST so we hard-code
        # the spec values here; the validator still applies all the same
        # rules to the user-influenceable fields.
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
            # Pending-code store at cap → signal back to the client per
            # RFC 6749 §4.1.2.1 instead of silently failing.
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
        """Return a 400 web.Response if any /authorize param is invalid,
        or None if all checks pass. Centralized so GET and POST share
        identical validation — the POST path explicitly re-validates the
        hidden form fields rather than trusting them."""
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
            return _text_error(400, "redirect_uri must be an https:// URL with a host")
        return None


class TokenView(HomeAssistantView):
    """OAuth /token endpoint: authorization_code + refresh_token grants."""

    requires_auth = False
    cors_allowed = True
    url = TOKEN_PATH
    name = "mcp_proxy_dev:oauth:token"

    def __init__(self, provider: OAuthProvider) -> None:
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
                return cid, sec
            return None, None
        return form.get("client_id"), form.get("client_secret")

    async def post(self, request: web.Request) -> web.Response:
        if _active_oauth_mode(self._provider) != MODE_LEGACY:
            # Serve ONLY when legacy is live. Both ha_auth (HA core is the
            # authorization server on its own /auth/token; this bare /token is
            # the add-on's own legacy view) and the unloaded/None case must not
            # mint tokens from this stale-bound view (see AuthorizeView for the
            # switch scenario).
            return _json_not_found()
        form = dict(await request.post())
        client_id, client_secret = self._extract_client_creds(request, form)
        if not self._provider.authenticate_client(client_id, client_secret):
            return _json_error(
                "invalid_client",
                401,
                headers={"WWW-Authenticate": 'Basic realm="MCP Proxy OAuth"'},
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
            }
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
            }
        )


# ---------------------------------------------------------------------------
# Metadata-view registration (shared by legacy register_views + ha_auth)
# ---------------------------------------------------------------------------


def _metadata_views(provider: MetadataProvider) -> list[HomeAssistantView]:
    """Build the seven discovery-document views bound to ``provider``.

    The canonical protected-resource + authorization-server documents, the
    path-scoped protected-resource document, and the four RFC 8414 / OIDC
    well-known authorization-server locations (issue #1714). These serve the
    same content in both OAuth modes (the AS view dispatches per-request); the
    root `AuthorizeView` + `TokenView` are legacy-only and added by
    `OAuthProvider.register_views`, never here.
    """
    views: list[HomeAssistantView] = [
        ProtectedResourceMetadataView(provider),
        AuthorizationServerMetadataView(provider),
        WellKnownProtectedResourceView(provider),
    ]
    for url, name in (
        (
            f"/.well-known/oauth-authorization-server{OAUTH_BASE}",
            "mcp_proxy_dev:oauth:wellknown-as-rfc8414",
        ),
        (
            f"/.well-known/openid-configuration{OAUTH_BASE}",
            "mcp_proxy_dev:oauth:wellknown-oidc-prefixed",
        ),
        (
            f"{OAUTH_BASE}/.well-known/openid-configuration",
            "mcp_proxy_dev:oauth:wellknown-oidc-suffixed",
        ),
        (
            f"{OAUTH_BASE}/.well-known/oauth-authorization-server",
            "mcp_proxy_dev:oauth:wellknown-as-suffixed",
        ),
    ):
        views.append(
            WellKnownAuthorizationServerMetadataView(provider, url=url, name=name)
        )
    return views


def register_metadata_views(hass: HomeAssistant, provider: MetadataProvider) -> None:
    """Register ONLY the seven discovery-document views (ha_auth mode).

    ha_auth serves just these — Home Assistant core is the authorization server,
    serving its own `/auth/authorize` + `/auth/token`, so the add-on binds no
    root views (the bare `/authorize` + `/token` are the legacy flavor's own
    root views) and no HA restart is ever needed to enable or disable the mode.
    ``provider`` is an `auth_native.ResourceServer` (ha_auth) or an
    `OAuthProvider` (legacy, which routes its seven views through here); the
    views read its `base_url_for`, `resource_url`, `authorization_server_url`,
    `webhook_id`, and `_hass` (read by `_active_oauth_mode` AND
    `_active_provider` for per-request mode/provider dispatch).

    Idempotent per HA session: the seven views bind at most once — a same-mode
    reload or a legacy<->ha_auth switch reuses the already-bound views rather
    than stacking silently-shadowed duplicate routes (a duplicate registration
    does not raise — aiohttp lets the first-registered path win — and HA can't
    drop a bound view until it restarts). The guard flag lives at a top-level
    hass.data key so it survives async_unload_entry's pop(DOMAIN).
    """
    if hass.data.get(_METADATA_VIEWS_REGISTERED_KEY):
        return
    for view in _metadata_views(provider):
        hass.http.register_view(view)
    hass.data[_METADATA_VIEWS_REGISTERED_KEY] = True


# ---------------------------------------------------------------------------
# Helper used by the webhook handler to build the 401 challenge response
# ---------------------------------------------------------------------------


def build_unauthorized_response(
    request: web.Request, provider: MetadataProvider
) -> web.Response:
    """Build the 401 + WWW-Authenticate response that MCP clients use to
    discover the OAuth endpoints.

    Per RFC 9728 §5.1 / MCP 2025-06-18 spec: WWW-Authenticate's
    resource_metadata parameter points to the protected-resource metadata
    URL, where the client finds the authorization server URL. We use the
    provider's configured public base URL (when set) so the metadata URL
    isn't built from attacker-supplied Host headers.
    """
    base = provider.base_url_for(request)
    metadata_url = f"{base}{OAUTH_BASE}/protected-resource"
    return web.Response(
        status=401,
        text="Unauthorized",
        headers={
            "WWW-Authenticate": (
                f'Bearer realm="MCP Proxy", resource_metadata="{metadata_url}"'
            )
        },
    )
