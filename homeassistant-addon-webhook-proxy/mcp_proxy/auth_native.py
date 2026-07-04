"""HA-native OAuth ("ha_auth" mode) resource-server support for the MCP Webhook Proxy.

In this mode Home Assistant core is the OAuth authorization server and this
add-on is a pure resource server. The add-on's only OAuth responsibility is
serving the two discovery documents (host-agnostically, exactly as it already
does) and validating bearer tokens via `hass.auth`; every protocol step —
`/auth/authorize`, `/auth/token`, PKCE, refresh — is Home Assistant core's own
maintained OAuth. That is what makes this mode "standardized": there is no
bespoke authorization-server code here to review or keep in step with the spec,
only the discovery documents plus a bearer check.

Validated end-to-end against live claude.ai (a follow-up to issue #1714). Because
the authorization-server document advertises Client ID Metadata Documents, it
also unblocks ChatGPT (issue #1725): clients present a URL-shaped `client_id`
(CIMD) with a same-origin redirect, which Home Assistant core's long-standing
IndieAuth handling accepts, so the user never has to paste any add-on credential.
home-assistant/core#153820 is field evidence that claude.ai and ChatGPT custom
connectors work against this flow — the add-on has no code dependency on it and
no minimum HA version is required.

It works on ANY hostname regardless of Home Assistant's `external_url` — the whole
reason the add-on serves the documents itself rather than relying on HA's own
metadata, which degrades on hostnames HA does not recognize. And because ha_auth
never binds any root HTTP view, enabling or disabling it never requires a Home
Assistant restart (contrast the legacy embedded authorization server in `oauth.py`,
whose `/authorize` + `/token` root views only bind cleanly at HA boot).

This module is lazy-imported by `__init__.py` ONLY on the ha_auth code path, so
the OAuth-off path stays behaviorally identical to the unauthenticated proxy and
the legacy path never loads it. It reuses `oauth.py`'s base-URL helper and
`OAUTH_BASE` rather than duplicating them.
"""

from __future__ import annotations

import inspect
import logging

from aiohttp import web
from homeassistant.core import HomeAssistant

from .oauth import OAUTH_BASE, _build_base_url

_LOGGER = logging.getLogger(__name__)

# Value of the add-on's `oauth.mode` selector that activates HA-native auth.
# Mirrored as the literal `"ha_auth"` in `oauth.py` (view mode dispatch) and
# `__init__.py` (config dispatch); a test pins the three in agreement.
HA_AUTH_MODE = "ha_auth"

# Sanity marker asserted once by the ha_auth literal-agreement test. The test
# suite feature-detects whether a webhook-proxy flavor ships ha_auth support by
# the presence of this module file, not by this symbol (the stable flavor skips
# the ha_auth tests until this module is promoted).
AUTH_V2 = True


def authorization_server_document(base: str) -> dict:
    """Return the RFC 8414 authorization-server metadata for ha_auth mode.

    Points MCP clients at Home Assistant core's own OAuth endpoints
    (`/auth/authorize` + `/auth/token`) while keeping the issuer on the add-on's
    host-agnostic `OAUTH_BASE`. `token_endpoint_auth_methods_supported` is
    `["none"]` (a public client — HA ignores `client_secret`) and
    `client_id_metadata_document_supported` is advertised so clients such as
    claude.ai and ChatGPT present a URL-shaped `client_id` (CIMD). The flag is
    advertisement-only: HA never fetches a CIMD document — a same-origin
    URL-shaped `client_id` plus redirect is long-standing IndieAuth behavior
    (home-assistant/core#153820 is field evidence of claude.ai and ChatGPT
    working against it, not a dependency). No `registration_endpoint`:
    dynamic client registration would hit Home Assistant, which offers none —
    CIMD replaces it. The field contents were validated live against claude.ai.
    """
    return {
        "issuer": f"{base}{OAUTH_BASE}",
        "authorization_endpoint": f"{base}/auth/authorize",
        "token_endpoint": f"{base}/auth/token",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "client_id_metadata_document_supported": True,
    }


class ResourceServer:
    """Resource-server half of ha_auth mode.

    Holds the per-install identity the discovery-document views need (the webhook
    id plus the operator-pinned public base URL, if any) and validates inbound
    bearer tokens against `hass.auth`. Unlike `oauth.OAuthProvider` it owns NO
    signing key, NO client credentials, and registers NO root views — Home
    Assistant core is the authorization server, so there is nothing here that a
    restart would need to rebind.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        webhook_id: str,
        public_base_url: str | None = None,
    ) -> None:
        self._hass = hass
        self._webhook_id = webhook_id
        self._public_base_url = public_base_url

    @property
    def webhook_id(self) -> str:
        return self._webhook_id

    def resource_url(self, base_url: str) -> str:
        return f"{base_url}/api/webhook/{self._webhook_id}"

    def authorization_server_url(self, base_url: str) -> str:
        """The issuer / authorization-server URL embedded in the metadata
        documents. Named to match what the shared discovery-document views in
        `oauth.py` call (so ha_auth reuses them unchanged)."""
        return f"{base_url}{OAUTH_BASE}"

    def base_url_for(self, request: web.Request) -> str:
        return _build_base_url(request, self._public_base_url)

    async def validate_request(self, request: web.Request) -> bool:
        """Return True iff the request carries a Bearer token Home Assistant accepts.

        Thin wrapper over `validate_request_detailed` — same contract, without
        the rejection-reason string.
        """
        authorized, _ = await self.validate_request_detailed(request)
        return authorized

    async def validate_request_detailed(self, request: web.Request) -> tuple[bool, str]:
        """Return ``(authorized, reason)`` for the request's Bearer token.

        A missing or malformed `Authorization` header is rejected WITHOUT
        touching the validator. Otherwise the token is handed to
        `hass.auth.async_validate_access_token`, which returns the backing
        `RefreshToken` (truthy) or `None` — but it is not exception-proof (a
        crafted unsigned token can make it raise outside `jwt.InvalidTokenError`),
        so any raise here is treated as unauthorized and the caller emits its
        normal 401 challenge instead of a 500. That method is a synchronous
        `@callback` in HA core (`homeassistant/auth/__init__.py`); we still await
        defensively iff a future Home Assistant turns it into a coroutine.

        The reason string is diagnostic telemetry for the add-on's inbound
        debug log (never the token itself): it distinguishes a request that
        carried no usable bearer from one whose bearer HA's validator rejected
        outright vs. one where the validator raised — the discrimination needed
        to debug provider-specific rejections (issue #1714's OIDC leg) from a
        user's add-on log alone.
        """
        header = request.headers.get("Authorization", "")
        if not header.lower().startswith("bearer "):
            return False, "no bearer header"
        token = header[7:].strip()
        if not token:
            return False, "empty bearer token"
        try:
            result = self._hass.auth.async_validate_access_token(token)
            if inspect.isawaitable(result):
                result = await result
        except Exception as err:
            _LOGGER.debug(
                "ha_auth: bearer validation raised; treating as unauthorized",
                exc_info=True,
            )
            return False, f"validator raised {type(err).__name__}"
        if result is None:
            return False, "token rejected by hass.auth (returned None)"
        return True, "valid"
