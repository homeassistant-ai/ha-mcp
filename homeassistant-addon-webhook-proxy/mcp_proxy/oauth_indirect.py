"""ha_auth-mode OAuth indirection in front of Home Assistant core.

The proxy-owned authorize and token routes keep client endpoint caches stable
while Home Assistant core remains the authorization server. This module also
validates cross-origin Client ID Metadata Documents before translating their
client identities into the same-origin form accepted by core.

MIRROR: this module is the near-verbatim twin of
``custom_components/ha_mcp_tools/oauth_ha_auth.py``. Keep behavioural changes
on the two sides in step. This pair has exactly ONE intended delta and it is
not behavioural: the loopback, redirect-shape, and base64url helpers are
imported from ``oauth.py`` here and from ``oauth_legacy.py`` in the component.
Those definitions are equivalent today, ``_LOOPBACK_HOSTNAMES`` and
``_AUTHORITY_CHARS_RE`` included — named here so it is noticed if that ever
stops being true. Everything else that differs is drift, including the deltas
the sibling ``oauth_dcr.py`` pair legitimately carries (the
``hass.data[DOMAIN]`` layout, the ``_addon_alive`` gate): neither appears in
either file of THIS pair.

CIMD fetches are HTTPS-only, redirect-free, size- and time-bounded, and pinned
to prevalidated globally routable DNS answers. Invalid identities pass through
unchanged so Home Assistant remains the final authority.

Core binds a refresh token to the client_id the code leg presented, and a
redirect_uri-less refresh grant carries nothing that re-derives it. So the
identity is recorded at mint time: every server-side-forwarded 200 has its
``refresh_token`` replaced by a signed envelope naming that client_id, and the
refresh leg unwraps it back into the exact pair (#2248). The client therefore
holds a value core cannot recognise, and core answers a revocation 200 either
way, so the proxy fronts revocation too and unwraps before forwarding.
"""

from __future__ import annotations

import asyncio
import binascii
import hashlib
import hmac
import ipaddress
import json
import logging
import socket
import time
from enum import Enum
from urllib.parse import ParseResult, urlparse, urlunparse

import aiohttp
from homeassistant.core import HomeAssistant

from .oauth import (
    _b64url_decode,
    _b64url_encode,
    _is_loopback_host,
    _is_valid_redirect_uri,
)
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
    """Return the one web origin shared by every non-loopback redirect.

    Loopback redirects are excluded: their runtime origin embeds an ephemeral
    port, so the redirect-less refresh leg reads that origin back out of the
    signed envelope instead of deriving it here (#2248).
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


# Refresh-token envelope (#2248). Same shape as the DCR blob — prefix +
# b64url(compact JSON) + "." + b64url(HMAC-SHA256) under the DCR key — but the
# MAC covers the PREFIX too, where the DCR blob signs the bare body. That makes
# the two families cryptographically disjoint: an envelope can never verify as
# a client_id registration, nor a client_id as a refresh token.
_REFRESH_ENVELOPE_PREFIX = "hamcp-rt-"


def _presented_client_hash(client_id: str) -> str:
    """Digest of the client_id an envelope was minted for."""
    return _b64url_encode(hashlib.sha256(client_id.encode("utf-8")).digest())


def wrap_refresh_token(
    signing_key: bytes,
    core_refresh_token: str,
    forward_client_id: str,
    presented_client_id: str,
) -> str:
    """Wrap core's refresh token with the identity core bound it to.

    The presenter digest binds the envelope to the client_id presented
    alongside it, so it is not usable under another client's identity.
    """
    payload = {
        "v": 1,
        "t": core_refresh_token,
        "c": forward_client_id,
        "p": _presented_client_hash(presented_client_id),
    }
    body = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    signature = hmac.new(
        signing_key,
        f"{_REFRESH_ENVELOPE_PREFIX}{body}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{_REFRESH_ENVELOPE_PREFIX}{body}.{_b64url_encode(signature)}"


class EnvelopeState(Enum):
    """Why :func:`unwrap_refresh_token` returned no ``(token, client_id)`` pair.

    ``ABSENT`` — the value carries no ``hamcp-rt-`` prefix: a bare core token
    minted before the envelope shipped (#2248), a DCR blob, or garbage. The
    caller falls through to the legacy derivation.
    ``INVALID`` — our prefix, with nothing redeemable behind it: a bad MAC, a
    presenter mismatch, a malformed body, or an unknown version. Core cannot
    redeem such a value either, so the REFRESH leg answers it locally instead
    of relaying it (the DCR signing key may simply have rotated). Revocation
    is the exception: see :func:`core_token_for_revocation`.
    """

    ABSENT = "absent"
    INVALID = "invalid"


def unwrap_refresh_token(
    signing_key: bytes, token: str, presented_client_id: str | None
) -> tuple[str, str] | EnvelopeState:
    """Recover ``(core refresh token, forward client_id)``, or why it failed.

    ``presented_client_id`` is the identity the envelope must have been minted
    alongside; pass None to skip that binding, which is what RFC 7009
    revocation wants — it authorizes the bearer of the token, not a client.
    Never raises: this runs on an anonymous view, and the two
    :class:`EnvelopeState` members are the whole failure surface. ``json.loads``
    runs only AFTER the MAC verifies, so no caller-chosen nesting reaches it.
    """
    if not token.startswith(_REFRESH_ENVELOPE_PREFIX):
        return EnvelopeState.ABSENT
    blob = token[len(_REFRESH_ENVELOPE_PREFIX) :]
    body, separator, signature = blob.rpartition(".")
    if not separator or not body:
        return EnvelopeState.INVALID
    try:
        expected = hmac.new(
            signing_key,
            f"{_REFRESH_ENVELOPE_PREFIX}{body}".encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(_b64url_decode(signature), expected):
            return EnvelopeState.INVALID
        payload = json.loads(_b64url_decode(body))
    except (ValueError, binascii.Error, UnicodeEncodeError):
        return EnvelopeState.INVALID
    if not isinstance(payload, dict) or payload.get("v") != 1:
        return EnvelopeState.INVALID
    core_refresh_token = payload.get("t")
    forward_client_id = payload.get("c")
    presenter = payload.get("p")
    if not (
        isinstance(core_refresh_token, str)
        and isinstance(forward_client_id, str)
        and isinstance(presenter, str)
    ):
        return EnvelopeState.INVALID
    if presented_client_id is not None and not hmac.compare_digest(
        presenter, _presented_client_hash(presented_client_id)
    ):
        return EnvelopeState.INVALID
    return core_refresh_token, forward_client_id


# Cap on a prefixed value the REVOCATION path will parse WITHOUT a verified
# MAC. Core's own refresh tokens are short, so a real envelope lands far under
# this; the cap keeps an anonymous view from handing an unbounded
# attacker-chosen blob to the base64 decoder and json.loads (the sibling input
# caps in oauth_dcr — MAX_REDIRECT_URI_LEN, MAX_DCR_BODY_BYTES — are the
# precedent).
MAX_REVOKE_ENVELOPE_LEN = 4096


def core_token_for_revocation(signing_key: bytes | None, token: str) -> str | None:
    """Core's own refresh token behind a revocation's ``token``, or None.

    None means "nothing of ours here" — no ``hamcp-rt-`` prefix, no signing
    key, or a prefixed value whose body yields no token — and the caller
    forwards the presented value on to core unchanged.

    A verified envelope resolves through :func:`unwrap_refresh_token`. A
    prefixed value whose MAC does NOT verify still gives up its core token
    here, parsed from the body alone, because revocation is the one leg where
    that is sound: RFC 7009 authorizes the BEARER of a token rather than a
    client, and core's ``/auth/revoke`` is anonymous and idempotent and
    answers 200 whatever it is handed — so forwarding an unverified body
    grants a forger nothing they could not get by POSTing to core directly.
    What it buys is the real case: an envelope minted before the DCR signing
    key rotated (removing and re-adding the integration mints a new one) stays
    revocable, instead of reaching core as a string it cannot resolve and
    answers 200 to while the grant lives out its 90 days (#2248).

    Deliberately revocation-only. The refresh leg still answers an INVALID
    envelope locally and never forwards it: there an unverified body would be
    a credential claim, where here it is only a request to destroy one.

    Never raises: this runs on an anonymous view.
    """
    if signing_key is None or not token.startswith(_REFRESH_ENVELOPE_PREFIX):
        return None
    verified = unwrap_refresh_token(signing_key, token, None)
    if isinstance(verified, tuple):
        core_refresh_token, _forward_id = verified
        return core_refresh_token
    # Only EnvelopeState.INVALID reaches here — the prefix check above ruled
    # ABSENT out. Everything below runs on UNVERIFIED, caller-chosen input.
    if len(token) > MAX_REVOKE_ENVELOPE_LEN:
        return None
    body, sep, _sig = token[len(_REFRESH_ENVELOPE_PREFIX) :].rpartition(".")
    if not sep or not body:
        return None
    try:
        payload = json.loads(_b64url_decode(body))
    except (ValueError, binascii.Error, UnicodeEncodeError, RecursionError):
        # RecursionError: json.loads on a deeply nested body (#2218 review),
        # which only this parse can meet — unwrap_refresh_token reaches
        # json.loads only AFTER the MAC verifies.
        return None
    if not isinstance(payload, dict):
        return None
    unverified_token = payload.get("t")
    if not isinstance(unverified_token, str):
        return None
    _LOGGER.warning(
        "ha_auth revoke: the presented envelope failed verification — the "
        "DCR signing key may have rotated. Forwarding the revocation to core "
        "on the token's own authority (RFC 7009 authorizes the bearer)"
    )
    return unverified_token


def rewrite_token_response_body(
    signing_key: bytes, body: bytes, forward_client_id: str, presented_client_id: str
) -> bytes:
    """Replace a core token response's ``refresh_token`` with an envelope.

    Returned unchanged when there is no string ``refresh_token`` to wrap.
    Applied to EVERY forwarded 200, not just the code leg, so a core that
    starts rotating refresh tokens stays covered.
    """
    try:
        parsed = json.loads(body)
    except (ValueError, RecursionError):
        # RecursionError: json.loads on a deeply nested body (#2218 review).
        if body:
            # A 200 from core's token endpoint is always a JSON object; a
            # non-empty body that is not one means core changed shape (or
            # something else answered), and the relay is flying blind.
            _LOGGER.warning(
                "ha_auth token response: core returned a non-JSON 200 body "
                "(%d bytes); relaying it unwrapped",
                len(body),
            )
        return body
    if not isinstance(parsed, dict):
        _LOGGER.warning(
            "ha_auth token response: core returned JSON %s rather than an "
            "object; relaying it unwrapped",
            type(parsed).__name__,
        )
        return body
    core_refresh_token = parsed.get("refresh_token")
    if not isinstance(core_refresh_token, str):
        # Expected on the refresh leg: core answers access_token/token_type/
        # expires_in with nothing to wrap. Silent by design.
        return body
    parsed["refresh_token"] = wrap_refresh_token(
        signing_key, core_refresh_token, forward_client_id, presented_client_id
    )
    return json.dumps(parsed, separators=(",", ":")).encode()


class RefreshDisposition(Enum):
    """Refresh identities that have no translated origin string.

    Reachable only for pre-envelope refresh tokens (#2248), which one
    re-authorize migrates to an envelope that names its identity outright.
    """

    PASSTHROUGH = "passthrough"
    UNREPRODUCIBLE = "unreproducible"


async def translated_client_id_for_refresh(
    session: aiohttp.ClientSession | None,
    dcr_key: bytes | None,
    client_id: str,
) -> str | RefreshDisposition:
    """Return a PRE-ENVELOPE refresh identity, passthrough, or unreproducible.

    Reached only for :attr:`EnvelopeState.ABSENT` — a bare token minted before
    #2248 — or when no DCR key is configured, so the envelope check is skipped
    entirely. A verified envelope names its identity outright, and an
    :attr:`EnvelopeState.INVALID` one (tampered, replayed under another
    client_id, or signed under a rotated key) is answered locally by the
    caller; neither reaches here.
    """
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
    return _stable_origin_or_unreproducible(registered, client_id)


def _stable_origin_or_unreproducible(
    registered: list[str], client_id: str
) -> str | RefreshDisposition:
    """The one web origin ``registered`` translates to, or ``UNREPRODUCIBLE``.

    ``_refresh_identity_is_reproducible`` already established that exactly one
    exists (canonical_origin_url is one-to-one over normalized origins), so
    None here means those two disagree. This view is ANONYMOUS: a bare
    ``assert`` would turn that into a 500, and ``-O`` strips it outright — so
    the impossible case degrades to the answer an ambiguous registration gets.
    """
    stable = stable_translation_origin(registered)
    if stable is None:
        _LOGGER.warning(
            "ha_auth refresh: %s passed the reproducible-identity check but "
            "names no single web origin; answering unreproducible",
            client_id,
        )
        return RefreshDisposition.UNREPRODUCIBLE
    return stable


def core_token_base_url(hass: HomeAssistant) -> str:
    """Return a trusted base URL for forwarding to core's token and revocation
    endpoints."""
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
