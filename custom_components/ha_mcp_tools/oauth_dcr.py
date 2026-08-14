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
"""

from __future__ import annotations

import binascii
import hashlib
import hmac
import json
import time
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import DATA_WEBHOOK, DOMAIN, OAUTH_BASE
from .oauth_legacy import (
    _b64url_decode,
    _b64url_encode,
    _is_valid_redirect_uri,
)

# cfg (hass.data[DOMAIN][DATA_WEBHOOK]) key holding the DCR HMAC key as bytes.
# Present only for none/ha_auth registrations — its presence is the per-request
# liveness gate for the register view (mirrors the mode-provider presence keys).
CFG_DCR_SIGNING_KEY = "dcr_signing_key"

_DCR_VIEW_REGISTERED_KEY = "ha_mcp_tools_oauth_dcr_view_registered"

_CLIENT_ID_PREFIX = "hamcp-dcr-"

# Registration floor: enough for any real client (claude.ai registers one
# callback; CLI clients a couple of loopback variants), small enough that the
# minted client_id stays a reasonable query-string citizen.
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
        try:
            body: Any = await request.json()
        except ValueError:
            return _dcr_error("invalid_client_metadata", "body must be JSON")
        if not isinstance(body, dict):
            return _dcr_error("invalid_client_metadata", "body must be an object")

        uris = body.get("redirect_uris")
        if not isinstance(uris, list) or not uris:
            return _dcr_error(
                "invalid_redirect_uri", "redirect_uris must be a non-empty array"
            )
        if len(uris) > MAX_REDIRECT_URIS:
            return _dcr_error(
                "invalid_redirect_uri",
                f"at most {MAX_REDIRECT_URIS} redirect_uris are accepted",
            )
        for uri in uris:
            if (
                not isinstance(uri, str)
                or len(uri) > MAX_REDIRECT_URI_LEN
                or not _is_valid_redirect_uri(uri)
            ):
                return _dcr_error(
                    "invalid_redirect_uri",
                    "redirect_uris must be https URLs or http loopback URLs "
                    "(RFC 8252) without fragments",
                )

        client_id = mint_client_id(key, uris)
        response: dict[str, Any] = {
            "client_id": client_id,
            "client_id_issued_at": int(time.time()),
            "redirect_uris": uris,
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
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
