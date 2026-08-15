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
import socket
import time
from urllib.parse import ParseResult, urlparse, urlunparse

import aiohttp
from homeassistant.core import HomeAssistant

from .oauth_dcr import canonical_origin_url, client_redirect_uris, normalized_origin
from .oauth_legacy import _is_loopback_host, _is_valid_redirect_uri

# CIMD fetch limits (mirrors core PR #176286's hardening + the 00-draft rules).
CIMD_MAX_BYTES = 10 * 1024
CIMD_FETCH_TIMEOUT = aiohttp.ClientTimeout(total=5)
CIMD_CACHE_TTL = 300.0
# Failed lookups cache too (#2213 review round 2) — briefly, so an anonymous
# caller cannot force a fresh resolution+fetch per request, while a transient
# failure still recovers quickly.
CIMD_NEGATIVE_TTL = 60.0
_CIMD_CACHE_MAX = 64
_ALLOWED_SCHEMES = ("https",)
# client_id URL -> (expires_monotonic, redirect_uris). The -00 draft forbids
# caching fetch errors and invalid documents, so negative entries never live
# here.
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
            timeout=5.0,
        )
    except (OSError, TimeoutError):
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

    Web redirects use the one stable origin the refresh leg can reproduce.
    Loopback redirects use the runtime origin including its ephemeral port.
    Anything else passes through unchanged (core stays the authority).
    """
    if not redirect_matches(registered, redirect_uri):
        return client_id
    redirect_origin = origin_client_id(redirect_uri)
    parsed_redirect = urlparse(redirect_uri)
    if parsed_redirect.hostname and _is_loopback_host(parsed_redirect.hostname):
        # A signed DCR blob is not URL-shaped, so core rejects it before login.
        # The runtime loopback origin is URL-shaped and exactly matches the
        # presented redirect (including its RFC 8252 ephemeral port).
        return redirect_origin
    stable = stable_translation_origin(registered)
    if stable is not None and redirect_origin == stable:
        return stable
    return client_id


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

    addresses = await _resolve_public_addresses(parsed.hostname, parsed.port or 443)
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
            return None
        _cache_cimd(client_id, now, result)
        return result
    # Resolution failed or no address answered: negative-cache THIS — the view
    # is anonymous, and only-success caching would let each request for a dead
    # hostname pay (and inflict) a fresh resolution (#2213 review round 2).
    _cache_cimd(client_id, now, None)
    return None


def _cache_cimd(client_id: str, now: float, result: list[str] | None) -> None:
    """Cache a lookup outcome, evicting expired entries then the oldest.

    Negative outcomes get the short ``CIMD_NEGATIVE_TTL``. Eviction never
    wholesale-clears: dropping every live client's entry because one more
    unique client_id arrived would hand an anonymous caller a lever over other
    clients' cache hits (#2213 review round 2).
    """
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
    except (UnicodeDecodeError, ValueError):
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


async def translated_client_id_for_refresh(
    session: aiohttp.ClientSession | None,
    dcr_key: bytes | None,
    client_id: str,
) -> str | None:
    """Translated client_id for the redirect_uri-less refresh grant, or None.

    Must agree with what the authorize/code legs presented to core, or core
    rejects the refresh (the token is bound to the client_id it was minted
    under). The legs agree by construction:

    * Same-origin identities (client_id origin == stable origin — claude.ai's
      hosted surfaces) took the fast path untranslated → None here.
    * Cross-origin identities with one stable web origin (Gemini Spark-class)
      were translated to that origin on every leg → return it here.
    * Loopback identities use their runtime origin (including the ephemeral
      port) for authorization/code exchange. That value cannot be reproduced
      on the redirect-less refresh leg, so None here makes core reject refresh
      and the client re-authorize rather than forwarding a mismatched identity.
    * Identities with several web origins were never translated → None here.

    Known caveat, documented not hidden: an identity whose registration mixes
    ONE stable web origin with loopback entries and that authorized via a
    loopback redirect used the runtime loopback origin on the code leg but
    translates to the web origin here — that refresh fails and the client
    re-authorizes. No observed client has that shape; fixing it would require
    remembering which redirect each token used, i.e. server-side state this
    design deliberately avoids.
    """
    registered: list[str] | None = None
    if dcr_key is not None:
        registered = client_redirect_uris(dcr_key, client_id)
    if registered is None:
        parsed = urlparse(client_id)
        if parsed.scheme == "https" and session is not None:
            registered = await fetch_cimd_redirects(session, client_id)
    if not registered:
        return None
    stable = stable_translation_origin(registered)
    if stable is None:
        return None
    parsed = urlparse(client_id)
    if f"{parsed.scheme}://{parsed.netloc}" == stable:
        return None
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
