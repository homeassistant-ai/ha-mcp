"""ha_auth-mode OAuth indirection: component-owned endpoints in front of core.

Home Assistant core remains the authorization server — the user signs in on
core's own ``/auth/authorize`` page exactly as before. What changes is what the
CLIENT learns and calls: the unified ``{OAUTH_BASE}/authorize`` redirects the
browser into core, and ``{OAUTH_BASE}/token`` forwards the exchange
server-side. Two problems this kills:

* **Cached-endpoint stickiness.** A client that cached our advertised
  endpoints keeps working after ANY auth-mode switch, because the endpoints it
  cached are ours and dispatch per request — it can no longer end up wedged on
  core's ``/auth/*`` (the un-retractable cache this replaces).
* **Cross-origin CIMD clients.** Core advertises CIMD but never fetches the
  document (core issue #176282), so clients whose redirect is not same-origin
  with their URL client_id die with "Invalid redirect URI". We validate the
  document HERE — the AS-side MUSTs from MCP 2026-07-28 client-registration —
  and hand core a translated client_id shaped to pass its long-stable
  same-origin IndieAuth rule.

SECURITY: translation grants nothing new. Core already accepts any
self-asserted ``client_id == redirect-origin`` pair (that is how claude.ai
connects today), so rewriting a VALIDATED cross-origin identity into that shape
authorizes nothing a client could not already claim by presenting the
redirect-origin as its client_id directly. Anything that fails validation is
forwarded UNCHANGED and core's own checks apply. The CIMD fetch itself is the
only outbound request: https-only, no redirects, 10 KiB cap, 5 s timeout,
loopback/IP-literal hosts refused (SSRF floor per the MCP security
considerations page).
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import time
from urllib.parse import urlparse

import aiohttp
from aiohttp import web
from homeassistant.core import HomeAssistant

from .oauth_dcr import client_redirect_uris
from .oauth_legacy import _is_loopback_host, _is_valid_redirect_uri

# CIMD fetch limits (mirrors core PR #176286's hardening + the 00-draft rules).
CIMD_MAX_BYTES = 10 * 1024
CIMD_FETCH_TIMEOUT = aiohttp.ClientTimeout(total=5)
CIMD_CACHE_TTL = 300.0
_CIMD_CACHE_MAX = 64
_ALLOWED_SCHEMES = ("https",)
# client_id URL -> (expires_monotonic, redirect_uris | None)
_cimd_cache: dict[str, tuple[float, list[str] | None]] = {}


def origin_client_id(redirect_uri: str) -> str:
    """The redirect target's origin, as a URL-shaped client_id core accepts."""
    parsed = urlparse(redirect_uri)
    return f"{parsed.scheme}://{parsed.netloc}"


def redirect_matches(registered: list[str], redirect_uri: str) -> bool:
    """RFC 6749 exact match, plus RFC 8252 §7.3 port-agnostic loopback match.

    Claude Code's Client ID Metadata Document registers
    ``http://localhost/callback`` / ``http://127.0.0.1/callback`` without a
    port while the runtime request carries an ephemeral one — the spec requires
    ignoring the port for loopback redirects.
    """
    if redirect_uri in registered:
        return True
    req = urlparse(redirect_uri)
    if req.hostname is None or not _is_loopback_host(req.hostname):
        return False
    for entry in registered:
        reg = urlparse(entry)
        if (
            reg.scheme == req.scheme
            and reg.hostname is not None
            and _is_loopback_host(reg.hostname)
            and reg.hostname == req.hostname
            and reg.path == req.path
        ):
            return True
    return False


async def fetch_cimd_redirects(
    session: aiohttp.ClientSession, client_id: str
) -> list[str] | None:
    """Fetch + validate a Client ID Metadata Document; return its redirect_uris.

    Returns None on ANY validation failure (the caller then passes the request
    through untranslated). Rules per draft-ietf-oauth-client-id-metadata-document-00
    and MCP 2026-07-28: https scheme with a path component and no fragment,
    direct 200 (no redirects followed), body fully read under the cap, strict
    UTF-8 JSON object, document ``client_id`` must round-trip exactly, and
    ``redirect_uris`` must be a list of strings.
    """
    parsed = urlparse(client_id)
    if (
        parsed.scheme not in _ALLOWED_SCHEMES
        or not parsed.hostname
        or parsed.fragment
        or not parsed.path
        or parsed.path == "/"
    ):
        return None
    # SSRF floor: never fetch loopback or IP-literal hosts. (DNS-rebinding is
    # outside SECURITY.md's local-network trust model.)
    if _is_loopback_host(parsed.hostname):
        return None
    try:
        ipaddress.ip_address(parsed.hostname)
        return None  # IP literal — refuse
    except ValueError:
        pass

    now = time.monotonic()
    cached = _cimd_cache.get(client_id)
    if cached is not None and cached[0] > now:
        return cached[1]

    result: list[str] | None = None
    try:
        async with session.get(
            client_id, allow_redirects=False, timeout=CIMD_FETCH_TIMEOUT
        ) as resp:
            if resp.status == 200:
                raw = await resp.content.read(CIMD_MAX_BYTES + 1)
                if len(raw) <= CIMD_MAX_BYTES:
                    result = _parse_cimd(raw, client_id)
    except (aiohttp.ClientError, asyncio.TimeoutError, UnicodeDecodeError):
        result = None

    if len(_cimd_cache) >= _CIMD_CACHE_MAX:
        _cimd_cache.clear()
    _cimd_cache[client_id] = (now + CIMD_CACHE_TTL, result)
    return result


def _parse_cimd(raw: bytes, client_id: str) -> list[str] | None:
    """Strict-parse a CIMD body; None unless every MUST holds."""
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(doc, dict) or doc.get("client_id") != client_id:
        return None
    uris = doc.get("redirect_uris")
    if not isinstance(uris, list) or not uris:
        return None
    if not all(isinstance(u, str) and _is_valid_redirect_uri(u) for u in uris):
        return None
    return uris


async def resolve_forward_client_id(
    session: aiohttp.ClientSession | None,
    dcr_key: bytes | None,
    client_id: str,
    redirect_uri: str,
) -> str:
    """The client_id to present to core: translated when validated, else as-is.

    Order: same-origin fast path (no fetch — today's claude.ai behavior,
    forwarded untouched), then our own stateless DCR blobs, then a cross-origin
    CIMD fetch. Every branch that cannot POSITIVELY validate the
    (client_id, redirect_uri) pair returns the original client_id so core's own
    validation remains the authority.
    """
    if not client_id or not _is_valid_redirect_uri(redirect_uri):
        return client_id
    parsed_client = urlparse(client_id)
    parsed_redirect = urlparse(redirect_uri)
    if parsed_client.scheme in ("http", "https") and (
        (parsed_client.scheme, parsed_client.netloc)
        == (parsed_redirect.scheme, parsed_redirect.netloc)
    ):
        return client_id

    if dcr_key is not None:
        registered = client_redirect_uris(dcr_key, client_id)
        if registered is not None:
            if redirect_matches(registered, redirect_uri) and origin_client_id(
                redirect_uri
            ) == origin_client_id(registered[0]):
                return origin_client_id(redirect_uri)
            return client_id

    if parsed_client.scheme == "https" and session is not None:
        registered = await fetch_cimd_redirects(session, client_id)
        if (
            registered is not None
            and redirect_matches(registered, redirect_uri)
            and origin_client_id(redirect_uri) == origin_client_id(registered[0])
        ):
            return origin_client_id(redirect_uri)
    return client_id


async def translated_client_id_for_refresh(
    session: aiohttp.ClientSession | None,
    dcr_key: bytes | None,
    client_id: str,
) -> str | None:
    """Translated client_id for the redirect_uri-less refresh grant, or None.

    The authorize/code legs translate to ``origin(presented redirect)``; the
    refresh leg has no redirect on the wire, so derive from the registered
    list's first entry — identical for any client whose registered redirects
    share one origin (every observed real client). Multi-origin lists were
    never translated on the authorize leg either (passthrough), so the legs
    stay consistent.
    """
    if dcr_key is not None:
        registered = client_redirect_uris(dcr_key, client_id)
        if registered:
            return origin_client_id(registered[0])
    parsed = urlparse(client_id)
    if parsed.scheme == "https" and session is not None:
        registered = await fetch_cimd_redirects(session, client_id)
        if registered:
            return origin_client_id(registered[0])
    return None


def core_token_base_url(hass: HomeAssistant, request: web.Request) -> str:
    """Base URL for the server-side ``/auth/token`` forward.

    Loopback when core serves plain http (no TLS mismatch possible); otherwise
    the public origin THIS request arrived on — guaranteed reachable with a
    certificate the local client stack trusts, because the request just
    traversed it.
    """
    api = getattr(hass.config, "api", None)
    if api is not None and not getattr(api, "use_ssl", False):
        return f"http://127.0.0.1:{api.port}"
    # Deferred to avoid a cycle: mcp_webhook imports this module.
    from .mcp_webhook import _build_base_url

    return _build_base_url(request)
