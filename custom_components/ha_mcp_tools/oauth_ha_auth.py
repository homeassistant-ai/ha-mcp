"""ha_auth-mode OAuth indirection: component-owned endpoints in front of core.

Home Assistant core remains the authorization server — the user signs in on
core's own ``/auth/authorize`` page exactly as before. What changes is what the
CLIENT learns and calls: the unified ``{OAUTH_BASE}/authorize`` redirects the
browser into core, and ``{OAUTH_BASE}/token`` forwards the exchange
server-side. Two problems this kills:

* **Cached-endpoint stickiness.** A client that cached our advertised
  endpoints keeps reaching component-owned routes after an auth-mode switch,
  because those routes dispatch per request — it can no longer end up wedged
  on core's ``/auth/*`` (the un-retractable cache this replaces). A switch to a
  mode with different credentials can still require the client to re-authorize.
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
only outbound request: https-only, no redirects, 10 KiB cap, 5 s timeout, and
DNS pinned to pre-validated globally routable addresses (SSRF floor per the MCP
security considerations page).
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import socket
import time
from enum import Enum
from urllib.parse import ParseResult, urlparse, urlunparse

import aiohttp
from homeassistant.core import HomeAssistant

from .oauth_dcr import (
    _refresh_identity_is_reproducible,
    canonical_origin_url,
    client_redirect_uris,
    normalized_origin,
)
from .oauth_legacy import _is_loopback_host, _is_valid_redirect_uri

_LOGGER = logging.getLogger(__name__)

# CIMD fetch limits (mirrors core PR #176286's hardening + the 00-draft rules).
CIMD_MAX_BYTES = 10 * 1024
CIMD_FETCH_TIMEOUT = aiohttp.ClientTimeout(total=5)
CIMD_RESOLVE_TIMEOUT = 5.0
# One deadline over the WHOLE lookup (resolution + every per-address fetch
# attempt): without it, a hostname resolving to many routable-but-unresponsive
# addresses costs resolve + N x fetch timeouts and an anonymous caller can
# park the small CIMD pool for the sum (#2217 review).
CIMD_TOTAL_LOOKUP_TIMEOUT = 12.0
CIMD_CACHE_TTL = 300.0
# Failed lookups cache too (#2213 review round 2) — briefly, so an anonymous
# caller cannot force a fresh resolution+fetch per request, while a transient
# failure still recovers quickly.
CIMD_NEGATIVE_TTL = 60.0
_CIMD_CACHE_MAX = 64
_ALLOWED_SCHEMES = ("https",)
# client_id URL -> (expires_monotonic, redirect_uris). Draft -00 section 4.4.3
# forbids caching error responses and invalid documents; both return with
# reached=True before any cache write. Unreachable-host and resolution outcomes
# are outside section 4.4.3 and are negative-cached for CIMD_NEGATIVE_TTL.
_cimd_cache: dict[str, tuple[float, list[str] | None]] = {}


def _reject_json_constant(constant: str) -> None:
    """Reject NaN/Infinity, which RFC 8259 JSON does not permit."""
    raise ValueError(f"Invalid JSON constant: {constant}")


def _valid_cimd_client_id(client_id: str) -> bool:
    """Return whether ``client_id`` satisfies the -00 URL-shape MUSTs."""
    try:
        parsed = urlparse(client_id)
        _ = parsed.port  # urlparse defers port validation until access.
    except ValueError:
        return False
    return (
        parsed.scheme in _ALLOWED_SCHEMES
        and bool(parsed.hostname)
        and bool(parsed.path)
        and parsed.path != "/"
        and "#" not in client_id
        and parsed.username is None
        and parsed.password is None
        and not any(segment in (".", "..") for segment in parsed.path.split("/"))
    )


async def _resolve_public_addresses(hostname: str, port: int) -> list[str]:
    """Resolve once and return addresses only when every answer is public.

    Rejecting the entire RRset when any answer is special-use prevents a host
    from mixing a public address with a private/loopback target. The returned
    addresses are used directly for the connection, pinning the fetch to this
    validated resolution instead of allowing a second DNS lookup to rebind it.
    """
    try:
        # Bounded resolution (#2213 review round 2): the view is anonymous and
        # CIMD_FETCH_TIMEOUT only starts at session.get, so an unbounded
        # getaddrinfo would let each unique hostname park a worker for the
        # resolver's own timeout.
        infos = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(
                hostname,
                port,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
            ),
            timeout=CIMD_RESOLVE_TIMEOUT,
        )
    except (OSError, ValueError, TimeoutError):
        # ValueError: getaddrinfo raises UnicodeEncodeError (a ValueError) for
        # hostname labels over 63 chars — attacker-reachable on this anonymous
        # view, and NOT an OSError (#2217 review, verified).
        _LOGGER.debug("CIMD lookup: resolution failed for %s", hostname)
        return []
    addresses = {str(sockaddr[0]) for *_, sockaddr in infos}
    if not addresses:
        return []
    try:
        if any(not ipaddress.ip_address(address).is_global for address in addresses):
            return []
    except ValueError:
        return []
    return sorted(addresses)


def _pinned_url(parsed: ParseResult, address: str) -> str:
    """Replace a parsed URL's host with a validated numeric address."""
    host = f"[{address}]" if ":" in address else address
    netloc = f"{host}:{parsed.port}" if parsed.port is not None else host
    return urlunparse(parsed._replace(netloc=netloc))


async def _fetch_pinned_cimd(
    session: aiohttp.ClientSession,
    client_id: str,
    parsed: ParseResult,
    address: str,
) -> tuple[bool, list[str] | None]:
    """Fetch one pinned address; report whether the server was reached.

    False lets a dual-stack caller try another validated address after a
    transport failure. Any HTTP or document-validation response is definitive
    and returns True so a different address cannot override it.
    """
    try:
        async with session.get(
            _pinned_url(parsed, address),
            allow_redirects=False,
            timeout=CIMD_FETCH_TIMEOUT,
            headers={"Host": parsed.netloc},
            # Preserve TLS SNI and certificate verification for the original
            # hostname while connecting to the pinned address.
            server_hostname=parsed.hostname if parsed.scheme == "https" else None,
        ) as resp:
            if resp.status != 200:
                return True, None
            raw = bytearray()
            async for chunk in resp.content.iter_chunked(1024):
                raw.extend(chunk)
                if len(raw) > CIMD_MAX_BYTES:
                    return True, None
            return True, _parse_cimd(bytes(raw), client_id)
    except (TimeoutError, aiohttp.ClientError):
        return False, None


def origin_client_id(redirect_uri: str) -> str:
    """The redirect target's origin, as a URL-shaped client_id core accepts.

    Canonicalized through the shared normalizer (#2213 review by Patch76):
    scheme-default ports are omitted, so ``https://h:443/cb`` and
    ``https://h/cb`` yield the same identity here and in DCR registration
    validation. (Core's own netloc comparison does not normalize — a client
    that literally presents an explicit default port at authorize still fails
    there; strictly narrower than the untranslated failure this replaces.)
    """
    origin = normalized_origin(redirect_uri)
    if origin is None:
        parsed = urlparse(redirect_uri)
        return f"{parsed.scheme}://{parsed.netloc}"
    return canonical_origin_url(origin)


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
            and reg.params == req.params
            and reg.query == req.query
        ):
            return True
    return False


def stable_translation_origin(registered: list[str]) -> str | None:
    """The single origin shared by every non-loopback registered redirect.

    None when there is no such origin (no web redirects, or several distinct
    ones). Loopback redirects are excluded because their runtime origin embeds
    an ephemeral port (RFC 8252) — they are translated from the presented
    redirect on the authorize/code legs, but cannot be re-derived here for the
    redirect_uri-less refresh leg.
    """
    origins: set[str] = set()
    for uri in registered:
        parsed = urlparse(uri)
        if parsed.hostname is None or _is_loopback_host(parsed.hostname):
            continue
        origin = normalized_origin(uri)
        if origin is not None:
            origins.add(canonical_origin_url(origin))
    if len(origins) == 1:
        return origins.pop()
    return None


def _translation_for(registered: list[str], client_id: str, redirect_uri: str) -> str:
    """Translate a registered redirect to the URL-shaped identity core accepts.

    One rule (#2217 review — the former web/loopback split collapsed to
    identical arms): a redirect that matches the registered list translates to
    the PRESENTED redirect's origin — for web redirects that keeps multi-origin
    registrations consistent across the authorize and code legs (both carry
    ``redirect_uri``), and for loopback redirects it is the runtime origin
    including the RFC 8252 ephemeral port. Unregistered redirects pass through
    unchanged (core stays the authority).
    """
    if not redirect_matches(registered, redirect_uri):
        return client_id
    return origin_client_id(redirect_uri)


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
    if not _valid_cimd_client_id(client_id):
        return None
    parsed = urlparse(client_id)
    assert parsed.hostname is not None  # established by _valid_cimd_client_id
    # Never fetch loopback or IP-literal client identifiers.
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

    try:
        async with asyncio.timeout(CIMD_TOTAL_LOOKUP_TIMEOUT):
            return await _lookup_cimd(session, client_id, parsed, now)
    except TimeoutError:
        _LOGGER.debug("CIMD lookup: total deadline exceeded for %s", client_id)
        _cache_cimd(client_id, now, None)
        return None


async def _lookup_cimd(
    session: aiohttp.ClientSession,
    client_id: str,
    parsed: ParseResult,
    now: float,
) -> list[str] | None:
    """Resolve and fetch under the caller's total deadline; cache the outcome."""
    addresses = await _resolve_public_addresses(
        parsed.hostname or "", parsed.port or 443
    )
    for address in addresses:
        reached, result = await _fetch_pinned_cimd(session, client_id, parsed, address)
        if not reached:
            # A dual-stack hostname may have one temporarily unreachable
            # address. Try the other address from the same pinned public RRset.
            continue
        if result is None:
            # INVALID document: deliberately NOT cached — a client that fixes
            # its metadata recovers on the next request (pinned by
            # test_invalid_cimd_is_not_negative_cached).
            _LOGGER.debug("CIMD lookup: document at %s failed validation", client_id)
            return None
        _cache_cimd(client_id, now, result)
        return result
    # Resolution failed or no address answered: negative-cache THIS — the view
    # is anonymous, and only-success caching would let each request for a dead
    # hostname pay (and inflict) a fresh resolution (#2213 review round 2).
    _LOGGER.debug("CIMD lookup: no reachable address for %s", client_id)
    _cache_cimd(client_id, now, None)
    return None


def _cache_cimd(client_id: str, now: float, result: list[str] | None) -> None:
    """Cache a lookup outcome, evicting expired entries then the oldest.

    Negative outcomes get the short ``CIMD_NEGATIVE_TTL``. Eviction drops
    expired entries first, then the least-recently-written; re-caching pops
    the key first so a hot client is not evicted from its original insertion
    slot. Anonymous churn can still cycle the 64 slots — that costs 64 unique
    requests per live entry, a rate bound rather than an absolute guarantee
    (#2217 review).
    """
    _cimd_cache.pop(client_id, None)
    if len(_cimd_cache) >= _CIMD_CACHE_MAX:
        for key in [k for k, (exp, _) in _cimd_cache.items() if exp <= now]:
            del _cimd_cache[key]
    while len(_cimd_cache) >= _CIMD_CACHE_MAX:
        del _cimd_cache[next(iter(_cimd_cache))]
    ttl = CIMD_CACHE_TTL if result is not None else CIMD_NEGATIVE_TTL
    _cimd_cache[client_id] = (now + ttl, result)


def _parse_cimd(raw: bytes, client_id: str) -> list[str] | None:
    """Strict-parse a CIMD body; None unless every MUST holds."""
    try:
        doc = json.loads(raw.decode("utf-8"), parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        # RecursionError: json.loads on ~5000 nested arrays fits inside the
        # 10 KiB cap and is a RuntimeError, not ValueError (#2217, verified).
        return None
    if (
        not isinstance(doc, dict)
        or doc.get("client_id") != client_id
        or not isinstance(doc.get("client_name"), str)
        or not doc["client_name"].strip()
        or "client_secret" in doc
        or "client_secret_expires_at" in doc
        or doc.get("token_endpoint_auth_method")
        in ("client_secret_basic", "client_secret_jwt", "client_secret_post")
    ):
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
            return _translation_for(registered, client_id, redirect_uri)

    if parsed_client.scheme == "https" and session is not None:
        registered = await fetch_cimd_redirects(session, client_id)
        if registered is not None:
            return _translation_for(registered, client_id, redirect_uri)
    return client_id


class RefreshDisposition(Enum):
    """Outcomes of refresh-identity derivation that carry no origin string.

    ``PASSTHROUGH`` — forward the client_id unchanged (unmanaged identity, or
    a same-origin identity the authorize leg also forwarded untranslated).
    ``UNREPRODUCIBLE`` — a VERIFIED registration (DCR blob or fetched CIMD
    document) whose refresh identity cannot be re-derived without the
    redirect_uri; the caller must answer ``invalid_grant`` locally instead of
    relaying a guaranteed core failure into its failed-login accounting
    (#2217 review — previously only DCR blobs got that answer, so CIMD
    identities of the same shape were 307'd into core on every token expiry).
    """

    PASSTHROUGH = "passthrough"
    UNREPRODUCIBLE = "unreproducible"


async def translated_client_id_for_refresh(
    session: aiohttp.ClientSession | None,
    dcr_key: bytes | None,
    client_id: str,
) -> str | RefreshDisposition:
    """Refresh-leg identity: a translated origin, or a disposition.

    Must agree with what the authorize/code legs presented to core, or core
    rejects the refresh (the token is bound to the client_id it was minted
    under). The legs agree by construction:

    * Unmanaged identities (no DCR blob, no fetchable document) →
      ``PASSTHROUGH`` — core stays the authority. A transient CIMD fetch
      failure lands here too (logged by the fetch path); erring toward
      ``UNREPRODUCIBLE`` would force re-auth on working same-origin clients.
    * Same-origin identities (client_id origin == stable origin — claude.ai's
      hosted surfaces) took the authorize fast path untranslated →
      ``PASSTHROUGH``, compared through the shared canonical origin form.
    * Cross-origin identities with exactly one web origin and no loopback
      entries were translated to that origin on every leg → return it.
    * Everything else that is VERIFIED — multiple web origins (Gemini
      Spark-class), loopback-only (Claude Code-class), or hybrid — cannot be
      re-derived without the redirect: ``UNREPRODUCIBLE``.
    """
    registered: list[str] | None = None
    if dcr_key is not None:
        registered = client_redirect_uris(dcr_key, client_id)
    if registered is None:
        parsed = urlparse(client_id)
        if parsed.scheme == "https" and session is not None:
            registered = await fetch_cimd_redirects(session, client_id)
    if not registered:
        return RefreshDisposition.PASSTHROUGH
    if not _refresh_identity_is_reproducible(registered):
        return RefreshDisposition.UNREPRODUCIBLE
    # Reproducible ⇒ exactly one web origin ⇒ stable_translation_origin cannot
    # return None (canonical_origin_url is one-to-one over normalized origins).
    stable = stable_translation_origin(registered)
    assert stable is not None
    # Canonical comparison (#2217 review): the raw-netloc form diverged from
    # the fast path whenever a registered redirect carried an explicit
    # scheme-default port.
    if origin_client_id(client_id) == stable:
        return RefreshDisposition.PASSTHROUGH
    return stable


def core_token_base_url(hass: HomeAssistant) -> str:
    """Base URL for the server-side ``/auth/token`` forward — never
    request-derived.

    Loopback when core serves plain http (no TLS mismatch possible); otherwise
    the operator-configured URL via ``homeassistant.helpers.network.get_url``.
    A forwarded-header-derived base would let an anonymous caller steer this
    server-side POST to a host of their choosing and read the relayed
    response (#2213 review) — request headers are deliberately not consulted.
    """
    api = getattr(hass.config, "api", None)
    if api is not None and not getattr(api, "use_ssl", False):
        return f"http://127.0.0.1:{api.port}"
    from homeassistant.helpers.network import NoURLAvailableError, get_url

    try:
        # str() wrapper: hass typing stubs leave get_url as Any in this
        # environment (mypy no-any-return).
        return str(
            get_url(
                hass,
                prefer_external=False,
                allow_cloud=False,
                require_ssl=True,
            )
        ).rstrip("/")
    except NoURLAvailableError:
        # Preserve the listener's TLS scheme. Certificate verification may
        # still reject a loopback hostname, but that fails loudly (503) rather
        # than leaking the token request in clear text or trusting caller
        # supplied headers.
        return f"https://127.0.0.1:{getattr(api, 'port', 8123)}"
