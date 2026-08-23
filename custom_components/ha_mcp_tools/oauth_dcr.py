"""Stateless RFC 7591 Dynamic Client Registration compat endpoint.

MCP 2026-07-28 deprecates DCR in favor of Client ID Metadata Documents, but
keeps it for backwards compatibility — and current connector brokers still take
the DCR branch when their discovery does not resolve CIMD (the "client
auto-registration isn't supported" failures in #2188/#2209). This module serves
that branch without a registration database: the minted ``client_id`` is an
HMAC-signed blob embedding the registered ``redirect_uris``, so verification is
stateless, restart-safe, and unbounded-growth-free (the operational DCR
problems the MCP maintainers deprecated it over).

Served only in ``none`` and ``ha_auth`` modes: legacy mode's whole purpose is a
pasted static credential, so it advertises no ``registration_endpoint``.

MIRROR: ``homeassistant-addon-webhook-proxy-dev/mcp_proxy_dev/oauth_dcr.py`` is
the near-verbatim twin of this module. Keep behavioural changes on the two
sides in step; that file's header names the pair's intended deltas (identity
rename, the flat ``hass.data[DOMAIN]`` layout in place of this side's
``cfg[DATA_WEBHOOK]`` nesting, the ``oauth_mode == ha_auth`` test in
``_active_grant_types`` where this side checks for a resource server, and the
``_addon_alive`` gate on the register view).
"""

from __future__ import annotations

import binascii
import hashlib
import hmac
import json
import time
from typing import Any, cast
from urllib.parse import urlparse

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import DATA_WEBHOOK, DOMAIN, OAUTH_BASE
from .oauth_legacy import (
    _b64url_decode,
    _b64url_encode,
    _is_loopback_host,
    _is_valid_redirect_uri,
)

# cfg (hass.data[DOMAIN][DATA_WEBHOOK]) key holding the DCR HMAC key as bytes.
# Present only for none/ha_auth registrations — its presence is the per-request
# liveness gate for the register view (mirrors the mode-provider presence keys).
CFG_DCR_SIGNING_KEY = "dcr_signing_key"

_DCR_VIEW_REGISTERED_KEY = "ha_mcp_tools_oauth_dcr_view_registered"

_CLIENT_ID_PREFIX = "hamcp-dcr-"
_REFRESH_TOKEN_PREFIX = "hamcp-refresh-"

# Registration floor: enough for any real client (claude.ai registers one
# callback; CLI clients a couple of loopback variants), small enough that the
# minted client_id stays a reasonable query-string citizen.
# A conforming registration is a few KB; HA's own 16 MiB client_max_size is
# no bound for an anonymous endpoint, so cap the read like the sibling CIMD
# fetch does (#2219 review round 3).
MAX_DCR_BODY_BYTES = 64 * 1024
MAX_REDIRECT_URIS = 10
MAX_REDIRECT_URI_LEN = 512


def mint_client_id(signing_key: bytes, redirect_uris: list[str]) -> str:
    """Mint a stateless client_id embedding ``redirect_uris`` (HMAC-signed)."""
    payload = {"r": redirect_uris, "iat": int(time.time())}
    body = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(signing_key, body.encode("ascii"), hashlib.sha256).digest()
    return f"{_CLIENT_ID_PREFIX}{body}.{_b64url_encode(sig)}"


def client_redirect_uris(signing_key: bytes, client_id: str) -> list[str] | None:
    """Return the redirect_uris a minted client_id embeds, or None if invalid."""
    if not client_id.startswith(_CLIENT_ID_PREFIX):
        return None
    blob = client_id[len(_CLIENT_ID_PREFIX) :]
    body, sep, sig_part = blob.rpartition(".")
    if not sep or not body:
        return None
    try:
        expected = hmac.new(signing_key, body.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64url_decode(sig_part), expected):
            return None
        payload = json.loads(_b64url_decode(body))
    except (ValueError, binascii.Error, UnicodeEncodeError):
        return None
    if not isinstance(payload, dict):
        return None
    uris = payload.get("r")
    if not isinstance(uris, list) or not all(isinstance(u, str) for u in uris):
        return None
    return uris


def mint_refresh_token_envelope(
    signing_key: bytes,
    client_id: str,
    refresh_token: str,
    origin: str,
) -> str:
    """Bind a core refresh token to its authorization-time loopback origin."""
    client_hash = _b64url_encode(hashlib.sha256(client_id.encode()).digest())
    payload = {"v": 1, "c": client_hash, "t": refresh_token, "o": origin}
    body = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    signature = hmac.new(signing_key, body.encode("ascii"), hashlib.sha256).digest()
    return f"{_REFRESH_TOKEN_PREFIX}{body}.{_b64url_encode(signature)}"


def unwrap_refresh_token_envelope(
    signing_key: bytes,
    client_id: str,
    envelope: str,
) -> tuple[str, str] | None:
    """Return the core token and loopback origin from a valid bound envelope."""
    if not envelope.startswith(_REFRESH_TOKEN_PREFIX):
        return None
    blob = envelope[len(_REFRESH_TOKEN_PREFIX) :]
    body, separator, signature = blob.rpartition(".")
    if not separator or not body:
        return None
    try:
        expected = hmac.new(signing_key, body.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64url_decode(signature), expected):
            return None
        payload = json.loads(_b64url_decode(body))
    except (ValueError, binascii.Error, UnicodeEncodeError):
        return None
    client_hash = _b64url_encode(hashlib.sha256(client_id.encode()).digest())
    if (
        not isinstance(payload, dict)
        or payload.get("v") != 1
        or payload.get("c") != client_hash
        or not isinstance(payload.get("t"), str)
        or not payload["t"]
        or not isinstance(payload.get("o"), str)
    ):
        return None
    origin = payload["o"]
    parsed = urlparse(origin)
    normalized = normalized_origin(origin)
    if (
        parsed.scheme != "http"
        or parsed.hostname is None
        or not _is_loopback_host(parsed.hostname)
        or parsed.path not in ("", "/")
        or parsed.params
        or parsed.query
        or parsed.fragment
        or normalized is None
        or canonical_origin_url(normalized) != origin.rstrip("/")
    ):
        return None
    return payload["t"], origin.rstrip("/")


def _active_dcr_key(hass: HomeAssistant) -> bytes | None:
    """The live DCR signing key, or None when DCR is not live (legacy mode,
    local-only mode, entry unloaded). Read live from hass.data per request —
    the view is bound once per HA session (aiohttp cannot unbind it)."""
    domain_data = hass.data.get(DOMAIN)
    if not isinstance(domain_data, dict):
        return None
    cfg = domain_data.get(DATA_WEBHOOK)
    if not isinstance(cfg, dict):
        return None
    key = cfg.get(CFG_DCR_SIGNING_KEY)
    return key if isinstance(key, bytes) else None


_DEFAULT_PORTS = {"https": 443, "http": 80}


def normalized_origin(uri: str) -> tuple[str, str, int] | None:
    """(scheme, host, port) origin identity with the scheme default applied.

    The ONE normalizer shared by registration validation and client-id
    translation (#2213 review by Patch76): ``https://h/a`` and
    ``https://h:443/b`` are the same origin everywhere, or nowhere.
    None for unparseable/hostless URIs.
    """
    parsed = urlparse(uri)
    if not parsed.scheme or parsed.hostname is None:
        return None
    port = parsed.port
    if port is None:
        port = _DEFAULT_PORTS.get(parsed.scheme, 0)
    return (parsed.scheme, parsed.hostname, port)


def canonical_origin_url(origin: tuple[str, str, int]) -> str:
    """URL form of a normalized origin, omitting the scheme-default port.

    IPv6 hosts are re-bracketed: ``urlparse().hostname`` strips the brackets,
    and an unbracketed colon-bearing host is not a valid URL authority (the
    translated client_id would be rejected downstream).
    """
    scheme, host, port = origin
    url_host = f"[{host}]" if ":" in host else host
    if _DEFAULT_PORTS.get(scheme) == port:
        return f"{scheme}://{url_host}"
    return f"{scheme}://{url_host}:{port}"


def stable_refresh_origin(redirect_uris: list[str]) -> str | None:
    """Return the one origin that a redirect-less refresh can reproduce.

    Web redirects are stable by origin. Loopback redirects are stable only
    when every callback uses the same explicit, nonzero port; omitted and zero
    ports represent runtime-selected listeners and cannot be reconstructed.
    """
    origins: set[tuple[str, str, int]] = set()
    loopback: bool | None = None
    for uri in redirect_uris:
        try:
            parsed = urlparse(uri)
            port = parsed.port
        except ValueError:
            return None
        if parsed.hostname is None:
            return None
        is_loopback = _is_loopback_host(parsed.hostname)
        if loopback is not None and loopback != is_loopback:
            return None
        loopback = is_loopback
        if is_loopback and port in (None, 0):
            return None
        origin = normalized_origin(uri)
        if origin is None:
            return None
        origins.add(origin)
    if len(origins) != 1:
        return None
    return canonical_origin_url(origins.pop())


def _refresh_identity_is_reproducible(redirect_uris: list[str]) -> bool:
    """Return whether the callback set has one stable refresh origin."""
    return stable_refresh_origin(redirect_uris) is not None


def _redirect_uris_error(value: Any) -> tuple[str, str] | None:
    """Return an RFC 7591 error for invalid redirect metadata, if any."""
    if not isinstance(value, list) or not value:
        return "invalid_redirect_uri", "redirect_uris must be a non-empty array"
    if len(value) > MAX_REDIRECT_URIS:
        return (
            "invalid_redirect_uri",
            f"at most {MAX_REDIRECT_URIS} redirect_uris are accepted",
        )
    if any(
        not isinstance(uri, str)
        or len(uri) > MAX_REDIRECT_URI_LEN
        or not _is_valid_redirect_uri(uri)
        for uri in value
    ):
        return (
            "invalid_redirect_uri",
            "redirect_uris must be https URLs or http loopback URLs "
            "(RFC 8252) without fragments",
        )
    return None


def _active_grant_types(hass: HomeAssistant, redirect_uris: list[str]) -> list[str]:
    """Grant types the ACTIVE mode actually implements (RFC 7591 honesty).

    none mode's auto-approve token endpoint rejects refresh grants and its AS
    document advertises only ``authorization_code`` — the registration response
    must not promise more. ha_auth forwards to core, but refresh is advertised
    only when every callback maps to one reproducible origin. Multiple web
    origins and omitted/ephemeral loopback ports cannot be reconstructed for a
    redirect_uri-less refresh grant without server state.
    """
    domain_data = hass.data.get(DOMAIN)
    cfg = domain_data.get(DATA_WEBHOOK) if isinstance(domain_data, dict) else None
    if (
        isinstance(cfg, dict)
        and cfg.get("resource_server") is not None
        and _refresh_identity_is_reproducible(redirect_uris)
    ):
        return ["authorization_code", "refresh_token"]
    return ["authorization_code"]


async def _read_capped_body(request: web.Request) -> bytes | None:
    """The request body, or None when it exceeds ``MAX_DCR_BODY_BYTES``.

    Reads to EOF rather than taking one ``StreamReader.read(n)``: that call
    may return a short chunk before EOF on a fragmented body, which would
    parse a truncated document (#2219 review round 3).
    """
    chunks: list[bytes] = []
    remaining = MAX_DCR_BODY_BYTES + 1
    while remaining > 0:
        chunk = await request.content.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    raw = b"".join(chunks)
    return None if len(raw) > MAX_DCR_BODY_BYTES else raw


def _dcr_error(error: str, description: str) -> web.Response:
    """RFC 7591 §3.2.2 registration error response."""
    return web.json_response(
        {"error": error, "error_description": description}, status=400
    )


class DcrRegisterView(HomeAssistantView):
    """RFC 7591 registration endpoint minting stateless public-client ids.

    Anonymous by design (DCR has no authentication for open registration) and
    write-free: nothing is stored, so the classic open-/register DoS concern
    (unbounded database growth) does not apply — the "registry" lives inside
    the signed client_id itself.
    """

    requires_auth = False
    cors_allowed = True
    url = f"{OAUTH_BASE}/register"
    name = "ha_mcp_tools:oauth:dcr-register"

    def __init__(self, hass: HomeAssistant) -> None:
        """Bind the view to the HA instance; liveness is resolved per request."""
        self._hass = hass

    async def post(self, request: web.Request) -> web.Response:
        """Register a client: validate redirect_uris, mint a signed client_id."""
        key = _active_dcr_key(self._hass)
        if key is None:
            return web.json_response({"error": "not_found"}, status=404)
        raw = await _read_capped_body(request)
        if raw is None:
            return _dcr_error("invalid_client_metadata", "body is too large")
        try:
            body: Any = json.loads(raw)
        except (ValueError, RecursionError):
            # RecursionError: json.loads on a deeply nested body (#2218
            # review) — malformed metadata, not a server error. Reading the
            # bytes ourselves also sidesteps request.json()'s charset lookup,
            # which raises LookupError on a bogus Content-Type charset
            # (#2219 review round 3); JSON is UTF-8 by RFC 8259 anyway.
            return _dcr_error("invalid_client_metadata", "body must be JSON")
        if not isinstance(body, dict):
            return _dcr_error("invalid_client_metadata", "body must be an object")

        raw_uris = body.get("redirect_uris")
        if error := _redirect_uris_error(raw_uris):
            return _dcr_error(*error)
        uris = cast(list[str], raw_uris)

        client_id = mint_client_id(key, uris)
        response: dict[str, Any] = {
            "client_id": client_id,
            "client_id_issued_at": int(time.time()),
            "redirect_uris": uris,
            "token_endpoint_auth_method": "none",
            "grant_types": _active_grant_types(self._hass, uris),
            "response_types": ["code"],
        }
        # Echo benign metadata the client sent (RFC 7591 §3.2.1 lets the AS
        # return the registered metadata; application_type is SEP-837's OIDC
        # nicety — we accept native and web alike, so echoing it is honest).
        for field in ("client_name", "application_type", "scope"):
            if isinstance(body.get(field), str):
                response[field] = body[field]
        return web.json_response(response, status=201)


def bind_dcr_view(hass: HomeAssistant) -> None:
    """Bind the register view at most once per HA session (per-request gated)."""
    if hass.data.get(_DCR_VIEW_REGISTERED_KEY):
        return
    hass.http.register_view(DcrRegisterView(hass))
    hass.data[_DCR_VIEW_REGISTERED_KEY] = True
