"""Stateless RFC 7591 Dynamic Client Registration compatibility endpoint.

The proxy keeps no registration database. Each minted ``client_id`` is an
HMAC-signed blob containing the registered redirect URIs, making verification
restart-safe without allowing an anonymous registration endpoint to grow
persistent state. DCR is live only in ha_auth and none-autoapprove modes;
legacy mode continues to use its configured static credential.

MIRROR: this module is the near-verbatim twin of
``custom_components/ha_mcp_tools/oauth_dcr.py``. Keep behavioural changes on
the two sides in step — the identity rename, the flat ``hass.data[DOMAIN]``
layout, the ``oauth_mode == ha_auth`` test in ``_active_grant_types`` where the
component checks for a resource server, and the ``_addon_alive`` gate on the
register view are the intended deltas; anything else is drift.
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

from .oauth import (
    DOMAIN,
    MODE_HA_AUTH,
    OAUTH_BASE,
    _addon_alive,
    _b64url_decode,
    _b64url_encode,
    _is_loopback_host,
    _is_valid_redirect_uri,
)

CFG_DCR_SIGNING_KEY = "dcr_signing_key"
_DCR_VIEW_REGISTERED_KEY = "mcp_proxy_dev_oauth_dcr_view_registered"
_CLIENT_ID_PREFIX = "hamcp-dcr-"

# A conforming registration is a few KB; HA's own 16 MiB client_max_size is
# no bound for an anonymous endpoint, so cap the read like the sibling CIMD
# fetch does (#2219 review round 3).
MAX_DCR_BODY_BYTES = 64 * 1024
MAX_REDIRECT_URIS = 10
MAX_REDIRECT_URI_LEN = 512


def mint_client_id(signing_key: bytes, redirect_uris: list[str]) -> str:
    """Mint a stateless client_id embedding ``redirect_uris``."""
    payload = {"r": redirect_uris, "iat": int(time.time())}
    body = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    signature = hmac.new(signing_key, body.encode("ascii"), hashlib.sha256).digest()
    return f"{_CLIENT_ID_PREFIX}{body}.{_b64url_encode(signature)}"


def client_redirect_uris(signing_key: bytes, client_id: str) -> list[str] | None:
    """Return the redirect URIs embedded in a valid minted client_id."""
    if not client_id.startswith(_CLIENT_ID_PREFIX):
        return None
    blob = client_id[len(_CLIENT_ID_PREFIX) :]
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
    if not isinstance(payload, dict):
        return None
    uris = payload.get("r")
    if not isinstance(uris, list) or not all(isinstance(uri, str) for uri in uris):
        return None
    return uris


def _active_dcr_key(hass: HomeAssistant) -> bytes | None:
    """Return the live signing key, or None when registration is unavailable."""
    domain_data = hass.data.get(DOMAIN)
    if not isinstance(domain_data, dict):
        return None
    key = domain_data.get(CFG_DCR_SIGNING_KEY)
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
    """Render a normalized origin, omitting scheme-default ports."""
    scheme, host, port = origin
    url_host = f"[{host}]" if ":" in host else host
    if _DEFAULT_PORTS.get(scheme) == port:
        return f"{scheme}://{url_host}"
    return f"{scheme}://{url_host}:{port}"


def _non_loopback_origins(redirect_uris: list[str]) -> set[tuple[str, str, int]]:
    """Return the normalized web origins represented by redirects."""
    origins: set[tuple[str, str, int]] = set()
    for uri in redirect_uris:
        parsed = urlparse(uri)
        if parsed.hostname is None or _is_loopback_host(parsed.hostname):
            continue
        origin = normalized_origin(uri)
        if origin is not None:
            origins.add(origin)
    return origins


def _refresh_identity_is_reproducible(redirect_uris: list[str]) -> bool:
    """Return whether every callback maps to one stable non-loopback origin."""
    if len(_non_loopback_origins(redirect_uris)) != 1:
        return False
    return not any(
        (hostname := urlparse(uri).hostname) is None or _is_loopback_host(hostname)
        for uri in redirect_uris
    )


def _redirect_uris_error(value: Any) -> tuple[str, str] | None:
    """Return an RFC 7591 error for invalid redirect metadata."""
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
    """Return only grant types implemented by the active proxy mode."""
    domain_data = hass.data.get(DOMAIN)
    if (
        isinstance(domain_data, dict)
        and domain_data.get("oauth_mode") == MODE_HA_AUTH
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
    """Return an RFC 7591 registration error response."""
    return web.json_response(
        {"error": error, "error_description": description}, status=400
    )


class DcrRegisterView(HomeAssistantView):
    """Mint stateless public-client registrations for the active proxy."""

    requires_auth = False
    cors_allowed = True
    url = f"{OAUTH_BASE}/register"
    name = "mcp_proxy_dev:oauth:dcr-register"

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def post(self, request: web.Request) -> web.Response:
        """Validate redirect metadata and return a signed client_id."""
        if not await _addon_alive(self._hass):
            return web.json_response({"error": "not_found"}, status=404)
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

        response: dict[str, Any] = {
            "client_id": mint_client_id(key, uris),
            "client_id_issued_at": int(time.time()),
            "redirect_uris": uris,
            "token_endpoint_auth_method": "none",
            "grant_types": _active_grant_types(self._hass, uris),
            "response_types": ["code"],
        }
        for field in ("client_name", "application_type", "scope"):
            if isinstance(body.get(field), str):
                response[field] = body[field]
        return web.json_response(response, status=201)


def bind_dcr_view(hass: HomeAssistant) -> None:
    """Bind the per-request-gated registration view once per HA session."""
    if hass.data.get(_DCR_VIEW_REGISTERED_KEY):
        return
    hass.http.register_view(DcrRegisterView(hass))
    hass.data[_DCR_VIEW_REGISTERED_KEY] = True
