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

REFRESH IDENTITY (issue #2248): core binds the refresh token to whatever
client_id the code leg presented, and a redirect_uri-less refresh grant carries
nothing that re-derives it — a loopback callback's runtime origin embeds an
ephemeral port (RFC 8252 §7.3), and a multi-origin registration is ambiguous.
So the identity is RECORDED at mint time instead of guessed later: every
server-side-forwarded 200 has its ``refresh_token`` replaced by a signed
envelope (:func:`wrap_refresh_token`) carrying core's real token plus the
client_id core bound it to, and the refresh leg unwraps it back into the exact
pair. :func:`translated_client_id_for_refresh` remains for tokens minted before
the envelope existed. The client therefore holds a value core cannot recognise,
and core answers a revocation 200 either way, so ``{OAUTH_BASE}/revoke`` fronts
revocation as well and unwraps before forwarding.

SECURITY: translation grants nothing new. Core already accepts any
self-asserted ``client_id == redirect-origin`` pair (that is how claude.ai
connects today), so rewriting a VALIDATED cross-origin identity into that shape
authorizes nothing a client could not already claim by presenting the
redirect-origin as its client_id directly. Anything that fails validation is
forwarded UNCHANGED and core's own checks apply. The CIMD fetch itself is the
only outbound request: https-only, no redirects, 10 KiB cap, 5 s timeout, and
DNS pinned to pre-validated globally routable addresses (SSRF floor per the MCP
security considerations page).

MIRROR: ``homeassistant-addon-webhook-proxy-dev/mcp_proxy_dev/oauth_indirect.py``
is the near-verbatim twin of this module. Keep behavioural changes on the two
sides in step; that file's header names the pair's one intended delta (the
loopback, redirect-shape, and base64url helpers come from ``oauth_legacy.py``
here and ``oauth.py`` there).
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

from .oauth_dcr import (
    _refresh_identity_is_reproducible,
    canonical_origin_url,
    client_redirect_uris,
    normalized_origin,
)
from .oauth_legacy import (
    _b64url_decode,
    _b64url_encode,
    _is_loopback_host,
    _is_valid_redirect_uri,
)

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

# Admission for the whole cache-miss lookup (DNS + fetch). Matches the
# dedicated CIMD connector limit so the two bounds cannot disagree.
_CIMD_LOOKUP_SLOTS = asyncio.Semaphore(4)


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
    redirect on the authorize/code legs, and the redirect_uri-less refresh leg
    reads that origin back out of the signed envelope instead of deriving it
    here (#2248).
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
    # indieauth._parse_url lowercases the netloc of BOTH the client_id and the
    # redirect_uri before comparing them, so a pair differing only in host
    # casing is same-origin to core and needs no translation. (Core's REFRESH
    # leg is byte-exact instead — refresh_token.client_id != client_id with no
    # normalization — which is why the refresh derivation below must reproduce
    # what THIS leg forwarded, verbatim.)
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


# Refresh-token envelope (issue #2248). Same shape as the DCR blob — prefix +
# b64url(compact JSON) + "." + b64url(HMAC-SHA256), signed with the DCR key —
# but the MAC covers the PREFIX too, where the DCR blob signs the bare body.
# That one difference is load-bearing: it makes the two blob families
# cryptographically disjoint, so an envelope can never verify as a client_id
# registration and a client_id can never verify as a refresh token.
_REFRESH_ENVELOPE_PREFIX = "hamcp-rt-"


def _presented_client_hash(client_id: str) -> str:
    """Digest of the client_id the envelope was minted for.

    Hashed rather than embedded verbatim: the envelope travels through the
    client, and a CIMD/DCR identity is long enough to bloat every refresh
    request for a value only ever compared for equality.
    """
    return _b64url_encode(hashlib.sha256(client_id.encode("utf-8")).digest())


def wrap_refresh_token(
    signing_key: bytes,
    core_refresh_token: str,
    forward_client_id: str,
    presented_client_id: str,
) -> str:
    """Wrap core's refresh token with the identity core bound it to.

    Recorded at mint time because the refresh leg cannot re-derive it: see the
    module header's REFRESH IDENTITY note. The presenter digest binds the
    envelope to the client_id that will be presented alongside it, so an
    envelope leaked to a different registered client is not usable under that
    client's own identity.
    """
    payload = {
        "v": 1,
        "t": core_refresh_token,
        "c": forward_client_id,
        "p": _presented_client_hash(presented_client_id),
    }
    body = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(
        signing_key,
        f"{_REFRESH_ENVELOPE_PREFIX}{body}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{_REFRESH_ENVELOPE_PREFIX}{body}.{_b64url_encode(sig)}"


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
    body, sep, sig_part = blob.rpartition(".")
    if not sep or not body:
        return EnvelopeState.INVALID
    try:
        expected = hmac.new(
            signing_key,
            f"{_REFRESH_ENVELOPE_PREFIX}{body}".encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(_b64url_decode(sig_part), expected):
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

    Returned unchanged for anything without a string ``refresh_token`` to wrap
    (core's refresh response is access_token/token_type/expires_in only today).
    Applied to EVERY server-side-forwarded 200 rather than just the code leg,
    so a core that starts rotating refresh tokens stays covered without a
    second change here.
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
    """Outcomes of refresh-identity derivation that carry no origin string.

    ``PASSTHROUGH`` — forward the client_id unchanged (unmanaged identity, or
    a same-origin identity the authorize leg also forwarded untranslated).
    ``UNREPRODUCIBLE`` — a VERIFIED registration (DCR blob or fetched CIMD
    document) whose refresh identity cannot be re-derived without the
    redirect_uri; the caller must answer ``invalid_grant`` locally instead of
    relaying a guaranteed core failure into its failed-login accounting
    (#2217 review — previously only DCR blobs got that answer, so CIMD
    identities of the same shape were 307'd into core on every token expiry).
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
    """Refresh-leg identity for a PRE-ENVELOPE token: an origin, or a disposition.

    Reached only for :attr:`EnvelopeState.ABSENT` — a bare token minted before
    #2248 shipped — or when no DCR key is configured, so the envelope check is
    skipped entirely. A verified envelope names its identity outright, and one
    that carries our prefix without verifying (:attr:`EnvelopeState.INVALID`:
    tampered, replayed under another client_id, or signed under a rotated key)
    is answered locally by the caller; neither reaches here.

    Must agree with what the authorize/code legs presented to core, or core
    rejects the refresh (the token is bound to the client_id it was minted
    under). Each case below either reproduces that identity exactly or
    returns ``UNREPRODUCIBLE`` rather than guess:

    * Unmanaged identities (no DCR blob, no fetchable document) →
      ``PASSTHROUGH`` — core stays the authority. A transient CIMD fetch
      failure lands here too (logged by the fetch path); erring toward
      ``UNREPRODUCIBLE`` would force re-auth on working same-origin clients.
    * Identities whose registered redirects ALL share the client_id's own
      origin (claude.ai's hosted surfaces) took the authorize fast path
      untranslated whichever redirect was presented → ``PASSTHROUGH``.
    * Identities where only SOME registered redirects share it cannot be
      resolved without knowing which redirect was presented — the fast path
      keys off the PRESENTED redirect, and the server keeps no record →
      ``UNREPRODUCIBLE`` (a local invalid_grant and a clean re-authorize beat
      forwarding a coin-flip identity into core's failed-login accounting).
    * Cross-origin identities with exactly one web origin and no loopback
      entries were translated to that origin on every leg → return it.
    * Everything else that is VERIFIED — multiple web origins (Gemini
      Spark-class), loopback-only (Claude Code-class), or hybrid — cannot be
      re-derived from a pre-envelope token without the redirect:
      ``UNREPRODUCIBLE``.
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
    #
    #   every registered redirect shares the client_id's origin → whichever
    #     one was presented, the fast path fired → the raw client_id went to
    #     core → PASSTHROUGH;
    #   none of them do → whichever was presented, it was translated to the
    #     one canonical origin → return it;
    #   some but not all → the answer depends on which was presented, and
    #     nothing here records that → UNREPRODUCIBLE.
    #
    # The middle case is real even under _refresh_identity_is_reproducible,
    # which normalizes ports: ["https://h/a", "https://h:443/b"] is ONE
    # canonical origin, yet authorize on /a passes the raw client_id through
    # while /b translates (#2219 review round 3). Casefolded because the
    # authorize fast path is (core lowercases both sides there).
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
    """Base URL for the server-side ``/auth/token`` and ``/auth/revoke``
    forwards — never request-derived.

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
