"""ha_auth-mode OAuth indirection in front of Home Assistant core.

The proxy-owned authorize and token routes keep client endpoint caches stable
while Home Assistant core remains the authorization server. This module also
validates cross-origin Client ID Metadata Documents before translating their
client identities into the same-origin form accepted by core.

MIRROR: this module is the near-verbatim twin of
``custom_components/ha_mcp_tools/oauth_ha_auth.py``. Keep behavioural changes
on the two sides in step — the identity rename, the flat ``hass.data[DOMAIN]``
layout instead of the component's ``cfg[DATA_WEBHOOK]`` nesting, and the
``_addon_alive`` gate are the intended deltas; anything else is drift.

CIMD fetches are HTTPS-only, redirect-free, size- and time-bounded, and pinned
to prevalidated globally routable DNS answers. Invalid identities pass through
unchanged so Home Assistant remains the final authority.
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

from .oauth import _is_loopback_host, _is_valid_redirect_uri
from .oauth_dcr import (
    _refresh_identity_is_reproducible,
    canonical_origin_url,
    client_redirect_uris,
    normalized_origin,
)

_LOGGER = logging.getLogger(__name__)

CIMD_MAX_BYTES = 10 * 1024
CIMD_FETCH_TIMEOUT = aiohttp.ClientTimeout(total=5)
CIMD_RESOLVE_TIMEOUT = 5.0
CIMD_TOTAL_LOOKUP_TIMEOUT = 12.0
CIMD_CACHE_TTL = 300.0
CIMD_NEGATIVE_TTL = 60.0
_CIMD_CACHE_MAX = 64
_ALLOWED_SCHEMES = ("https",)
# client_id URL -> (expires_monotonic, redirect_uris). Draft -00 section 4.4.3
# forbids caching error responses and invalid documents; both return with
# reached=True before any cache write. Unreachable-host and resolution outcomes
# are outside section 4.4.3 and are negative-cached for CIMD_NEGATIVE_TTL.
_cimd_cache: dict[str, tuple[float, list[str] | None]] = {}

# Admission for the whole cache-miss lookup (DNS + fetch). Matches the
# dedicated CIMD connector limit so the two bounds cannot disagree.
_CIMD_LOOKUP_SLOTS = asyncio.Semaphore(4)


def _reject_json_constant(constant: str) -> None:
    """Reject NaN and Infinity, which RFC 8259 JSON does not permit."""
    raise ValueError(f"Invalid JSON constant: {constant}")


def _valid_cimd_client_id(client_id: str) -> bool:
    """Return whether a client_id satisfies the CIMD URL-shape requirements."""
    try:
        parsed = urlparse(client_id)
        _ = parsed.port
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
    """Resolve once and accept the result only when every address is public."""
    try:
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
    """Replace a parsed URL host with a validated numeric address."""
    host = f"[{address}]" if ":" in address else address
    netloc = f"{host}:{parsed.port}" if parsed.port is not None else host
    return urlunparse(parsed._replace(netloc=netloc))


async def _fetch_pinned_cimd(
    session: aiohttp.ClientSession,
    client_id: str,
    parsed: ParseResult,
    address: str,
) -> tuple[bool, list[str] | None]:
    """Fetch one pinned address and report whether the server was reached."""
    try:
        async with session.get(
            _pinned_url(parsed, address),
            allow_redirects=False,
            timeout=CIMD_FETCH_TIMEOUT,
            headers={"Host": parsed.netloc},
            server_hostname=parsed.hostname if parsed.scheme == "https" else None,
        ) as response:
            if response.status != 200:
                return True, None
            raw = bytearray()
            async for chunk in response.content.iter_chunked(1024):
                raw.extend(chunk)
                if len(raw) > CIMD_MAX_BYTES:
                    return True, None
            return True, _parse_cimd(bytes(raw), client_id)
    except (TimeoutError, aiohttp.ClientError):
        return False, None


def origin_client_id(redirect_uri: str) -> str:
    """Return the callback origin in the URL-shaped form accepted by core."""
    origin = normalized_origin(redirect_uri)
    if origin is None:
        parsed = urlparse(redirect_uri)
        return f"{parsed.scheme}://{parsed.netloc}"
    return canonical_origin_url(origin)


def redirect_matches(registered: list[str], redirect_uri: str) -> bool:
    """Apply exact matching plus RFC 8252 port-agnostic loopback matching."""
    if redirect_uri in registered:
        return True
    requested = urlparse(redirect_uri)
    if requested.hostname is None or not _is_loopback_host(requested.hostname):
        return False
    for entry in registered:
        candidate = urlparse(entry)
        if (
            candidate.scheme == requested.scheme
            and candidate.hostname is not None
            and _is_loopback_host(candidate.hostname)
            and candidate.hostname == requested.hostname
            and candidate.path == requested.path
            and candidate.params == requested.params
            and candidate.query == requested.query
        ):
            return True
    return False


def stable_translation_origin(registered: list[str]) -> str | None:
    """Return the one web origin shared by every non-loopback redirect."""
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
    """Translate a verified registration using the presented redirect origin."""
    if not redirect_matches(registered, redirect_uri):
        return client_id
    return origin_client_id(redirect_uri)


async def fetch_cimd_redirects(
    session: aiohttp.ClientSession, client_id: str
) -> list[str] | None:
    """Fetch and validate a Client ID Metadata Document."""
    if not _valid_cimd_client_id(client_id):
        return None
    parsed = urlparse(client_id)
    assert parsed.hostname is not None  # established by _valid_cimd_client_id
    if _is_loopback_host(parsed.hostname):
        return None
    try:
        ipaddress.ip_address(parsed.hostname)
        return None
    except ValueError:
        pass

    now = time.monotonic()
    cached = _cimd_cache.get(client_id)
    if cached is not None and cached[0] > now:
        return cached[1]

    try:
        async with asyncio.timeout(CIMD_TOTAL_LOOKUP_TIMEOUT):
            # The dedicated connector caps concurrent HTTP, but DNS runs in
            # the executor BEFORE any connection is taken, so unique
            # attacker-chosen client_ids could pile getaddrinfo() calls onto
            # the shared pool (#2219 codex review). Admission covers the whole
            # cache-miss path and waits inside the deadline above, so a
            # legitimate lookup queues rather than failing.
            async with _CIMD_LOOKUP_SLOTS:
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
    """Resolve and fetch under the total deadline, then cache the outcome."""
    addresses = await _resolve_public_addresses(
        parsed.hostname or "", parsed.port or 443
    )
    for address in addresses:
        reached, result = await _fetch_pinned_cimd(session, client_id, parsed, address)
        if not reached:
            continue
        if result is None:
            # INVALID document: deliberately NOT cached — a client that fixes
            # its metadata recovers on the next request (pinned by
            # test_invalid_cimd_document_is_not_negative_cached; the component
            # twin pins the same policy as
            # test_invalid_cimd_is_not_negative_cached).
            _LOGGER.debug("CIMD lookup: document at %s failed validation", client_id)
            return None
        _cache_cimd(client_id, now, result)
        return result
    _LOGGER.debug("CIMD lookup: no reachable address for %s", client_id)
    _cache_cimd(client_id, now, None)
    return None


def _cache_cimd(client_id: str, now: float, result: list[str] | None) -> None:
    """Cache positive and unreachable-host outcomes with bounded storage."""
    _cimd_cache.pop(client_id, None)
    if len(_cimd_cache) >= _CIMD_CACHE_MAX:
        for key in [key for key, (exp, _) in _cimd_cache.items() if exp <= now]:
            del _cimd_cache[key]
    while len(_cimd_cache) >= _CIMD_CACHE_MAX:
        del _cimd_cache[next(iter(_cimd_cache))]
    ttl = CIMD_CACHE_TTL if result is not None else CIMD_NEGATIVE_TTL
    _cimd_cache[client_id] = (now + ttl, result)


def _parse_cimd(raw: bytes, client_id: str) -> list[str] | None:
    """Strictly parse a CIMD body and return its validated redirect URIs."""
    try:
        document = json.loads(raw.decode("utf-8"), parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if (
        not isinstance(document, dict)
        or document.get("client_id") != client_id
        or not isinstance(document.get("client_name"), str)
        or not document["client_name"].strip()
        or "client_secret" in document
        or "client_secret_expires_at" in document
        or document.get("token_endpoint_auth_method")
        in ("client_secret_basic", "client_secret_jwt", "client_secret_post")
    ):
        return None
    uris = document.get("redirect_uris")
    if not isinstance(uris, list) or not uris:
        return None
    if not all(isinstance(uri, str) and _is_valid_redirect_uri(uri) for uri in uris):
        return None
    return uris


async def resolve_forward_client_id(
    session: aiohttp.ClientSession | None,
    dcr_key: bytes | None,
    client_id: str,
    redirect_uri: str,
) -> str:
    """Return the validated translated identity to present to core."""
    if not client_id or not _is_valid_redirect_uri(redirect_uri):
        return client_id
    # Raw-netloc comparison exactly like the component (#2218 review): the
    # client_id is unvalidated here, and normalized_origin() reads
    # parsed.port, which raises ValueError on a malformed port — the fast
    # path must pass such identities through for core to reject, not 500.
    # urlparse defers some validation until access and raises outright on
    # shapes like "https://[" (unterminated IPv6). These views are ANONYMOUS,
    # so a malformed client_id must pass through for core to reject rather
    # than traceback (#2219 codex review) — the same contract the redirect
    # validator states for its own .port access.
    try:
        parsed_client = urlparse(client_id)
        parsed_redirect = urlparse(redirect_uri)
    except ValueError:
        return client_id
    # Case-insensitive netloc equality, matching core's own authorize rule:
    # indieauth._parse_url lowercases the netloc of BOTH the client_id and
    # the redirect_uri before comparing. (Core's REFRESH leg is byte-exact
    # instead, which is why the derivation below must reproduce what THIS
    # leg forwarded, verbatim.)
    if parsed_client.scheme in ("http", "https") and (
        (parsed_client.scheme, parsed_client.netloc.lower())
        == (parsed_redirect.scheme, parsed_redirect.netloc.lower())
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
    """Refresh identities that have no translated origin string."""

    PASSTHROUGH = "passthrough"
    UNREPRODUCIBLE = "unreproducible"


async def translated_client_id_for_refresh(
    session: aiohttp.ClientSession | None,
    dcr_key: bytes | None,
    client_id: str,
) -> str | RefreshDisposition:
    """Return a refresh identity, passthrough, or unreproducible disposition."""
    registered: list[str] | None = None
    if dcr_key is not None:
        registered = client_redirect_uris(dcr_key, client_id)
    if registered is None:
        try:
            parsed = urlparse(client_id)
        except ValueError:
            # Malformed identity: core stays the authority (see the authorize
            # leg's note); never a traceback on an anonymous view.
            return RefreshDisposition.PASSTHROUGH
        if parsed.scheme == "https" and session is not None:
            registered = await fetch_cimd_redirects(session, client_id)
    if not registered:
        return RefreshDisposition.PASSTHROUGH
    if not _refresh_identity_is_reproducible(registered):
        return RefreshDisposition.UNREPRODUCIBLE
    stable = stable_translation_origin(registered)
    assert stable is not None
    # Reproduce what the authorize leg forwarded, or admit we cannot. That
    # leg's fast path keys off the PRESENTED redirect, which the redirect-less
    # refresh grant does not carry, so the registered list is all we have:
    # every registered redirect shares the client_id's origin → the fast path
    # fired whichever was presented → PASSTHROUGH; none do → it was
    # translated to the one canonical origin; some but not all → the answer
    # depends on which was presented and nothing records that →
    # UNREPRODUCIBLE. The split case is real even under
    # _refresh_identity_is_reproducible, which normalizes ports:
    # ["https://h/a", "https://h:443/b"] is ONE canonical origin, yet
    # authorize on /a passes through while /b translates (#2219 round 3).
    try:
        parsed = urlparse(client_id)
    except ValueError:
        return RefreshDisposition.PASSTHROUGH
    client_origin = (parsed.scheme, parsed.netloc.lower())
    matched = [
        (urlparse(uri).scheme, urlparse(uri).netloc.lower()) == client_origin
        for uri in registered
    ]
    if all(matched):
        return RefreshDisposition.PASSTHROUGH
    if any(matched):
        return RefreshDisposition.UNREPRODUCIBLE
    return stable


def core_token_base_url(hass: HomeAssistant) -> str:
    """Return a trusted base URL for forwarding to core's token endpoint."""
    api = getattr(hass.config, "api", None)
    if api is not None and not getattr(api, "use_ssl", False):
        return f"http://127.0.0.1:{api.port}"
    from homeassistant.helpers.network import NoURLAvailableError, get_url

    try:
        return str(
            get_url(
                hass,
                prefer_external=False,
                allow_cloud=False,
                require_ssl=True,
            )
        ).rstrip("/")
    except NoURLAvailableError:
        return f"https://127.0.0.1:{getattr(api, 'port', 8123)}"
