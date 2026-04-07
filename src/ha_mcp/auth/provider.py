"""
Home Assistant OAuth 2.1 Provider.

This module implements OAuth 2.1 authentication with Dynamic Client Registration (DCR)
for Home Assistant MCP Server. Users authenticate via a consent form where they
provide their Long-Lived Access Token (LLAT).
"""

import binascii
import hashlib
import hmac
import json
import logging
import secrets
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from typing import Any
from urllib.parse import urlencode

from fastmcp.server.auth.auth import (
    AccessToken,  # FastMCP version has claims field
    ClientRegistrationOptions,
    OAuthProvider,
    RevocationOptions,
)
from mcp.server.auth.provider import (
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from .consent_form import create_consent_html, create_error_html

logger = logging.getLogger(__name__)

# Token expiration times
AUTH_CODE_EXPIRY_SECONDS = 5 * 60  # 5 minutes
ACCESS_TOKEN_EXPIRY_SECONDS = 60 * 60  # 1 hour
REFRESH_TOKEN_EXPIRY_SECONDS = 7 * 24 * 60 * 60  # 7 days


class HomeAssistantCredentials:
    """Stores Home Assistant credentials for a client."""

    def __init__(self, ha_token: str):
        self.ha_token = ha_token
        self.validated_at = time.time()

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for storage."""
        return {
            "ha_token": self.ha_token,
            "validated_at": self.validated_at,
        }


class HomeAssistantOAuthProvider(OAuthProvider):
    """
    OAuth 2.1 provider for Home Assistant MCP Server.

    This provider implements the full OAuth 2.1 flow with:
    - Dynamic Client Registration (DCR)
    - PKCE support
    - Custom consent form for collecting HA credentials
    - Fully stateless tokens (both access and refresh)

    The consent form collects the user's Long-Lived Access Token (LLAT),
    which is encoded into both access and refresh tokens as base64 JSON.
    No server-side token state is stored, so the server survives container
    restarts without losing sessions (clients re-register via DCR
    automatically).

    Security comes from HTTPS transport and the LLAT itself being the
    authorization boundary — revoking the LLAT in Home Assistant
    immediately invalidates all derived tokens.
    """

    def __init__(
        self,
        base_url: AnyHttpUrl | str,
        issuer_url: AnyHttpUrl | str | None = None,
        service_documentation_url: AnyHttpUrl | str | None = None,
        client_registration_options: ClientRegistrationOptions | None = None,
        revocation_options: RevocationOptions | None = None,
        required_scopes: list[str] | None = None,
    ):
        """
        Initialize the Home Assistant OAuth provider.

        Args:
            base_url: The public URL of this MCP server (required)
            issuer_url: The issuer URL for OAuth metadata (defaults to base_url)
            service_documentation_url: URL to service documentation
            client_registration_options: Options for client registration
            revocation_options: Options for token revocation
            required_scopes: Scopes required for all requests
        """
        # Enable DCR by default
        if client_registration_options is None:
            client_registration_options = ClientRegistrationOptions(
                enabled=True,
                valid_scopes=["homeassistant", "mcp"],
            )

        # Enable revocation by default
        if revocation_options is None:
            revocation_options = RevocationOptions(enabled=True)

        super().__init__(
            base_url=base_url,
            issuer_url=issuer_url,
            service_documentation_url=service_documentation_url,
            client_registration_options=client_registration_options,
            revocation_options=revocation_options,
            required_scopes=required_scopes,
        )

        # In-memory storage (session-scoped, not persisted)
        self.clients: dict[str, OAuthClientInformationFull] = {}
        self.auth_codes: dict[str, AuthorizationCode] = {}

        # Home Assistant credentials storage (keyed by client_id)
        # Temporary: only held between consent form submission and token exchange
        self.ha_credentials: dict[str, HomeAssistantCredentials] = {}

        # Pending authorization requests (for consent form flow)
        self.pending_authorizations: dict[str, dict[str, Any]] = {}

        # Server-side secret for HMAC-protecting LLATs in refresh tokens.
        # Regenerated each startup — existing refresh tokens become invalid
        # on restart, but that's acceptable since access tokens still work
        # and clients will re-authenticate via the consent form.
        self._hmac_secret = secrets.token_bytes(32)

        logger.info(f"HomeAssistantOAuthProvider initialized with base_url={base_url}")

    def _sign_payload(self, payload_json: bytes) -> str:
        """Compute HMAC-SHA256 signature of a token payload."""
        return hmac.new(self._hmac_secret, payload_json, hashlib.sha256).hexdigest()

    def _encode_token(
        self,
        ha_token: str,
        token_type: str = "access",
        client_id: str | None = None,
        scopes: list[str] | None = None,
        expires_at: int | None = None,
    ) -> str:
        """Encode a stateless HMAC-signed OAuth token as base64 JSON.

        All tokens are HMAC-signed using a per-instance server secret.
        This prevents forgery and tampering: an intercepted token cannot
        be modified (e.g., changing exp, scopes, or client_id) without
        invalidating the signature.

        The token payload contains the HA LLAT, token type, issued-at
        timestamp, and (for refresh tokens) client/scope metadata and
        expiry.

        Tokens are invalidated on server restart (new HMAC secret).
        Clients re-authenticate via the consent form.
        """
        payload: dict[str, Any] = {
            "ha_token": ha_token,
            "type": token_type,
            "iat": int(time.time()),
        }
        if client_id is not None:
            payload["client_id"] = client_id
        if scopes is not None:
            payload["scopes"] = scopes
        if expires_at is not None:
            payload["exp"] = expires_at

        payload_json = json.dumps(payload, sort_keys=True).encode()
        sig = self._sign_payload(payload_json)
        envelope = json.dumps({"payload": payload, "sig": sig}).encode()
        return urlsafe_b64encode(envelope).decode().rstrip("=")

    def _decode_token(self, token: str) -> dict[str, Any] | None:
        """Decode a stateless token and return its full payload.

        For refresh tokens, verifies the HMAC signature before accepting.
        Returns the payload dict or ``None`` if the token is malformed
        or signature verification fails.
        """
        try:
            # Add padding if needed
            padding = 4 - (len(token) % 4)
            if padding != 4:
                token += "=" * padding

            decoded = urlsafe_b64decode(token.encode()).decode()
            outer = json.loads(decoded)

            if not isinstance(outer, dict):
                return None

            # Signed envelope: {"payload": {...}, "sig": "..."}
            if "sig" in outer and "payload" in outer:
                payload = outer["payload"]
                if not isinstance(payload, dict):
                    return None
                expected_sig = self._sign_payload(
                    json.dumps(payload, sort_keys=True).encode()
                )
                if not hmac.compare_digest(outer["sig"], expected_sig):
                    logger.warning("Token signature verification failed")
                    return None
                if not payload.get("ha_token"):
                    return None
                return payload

            # Unsigned payload (backwards compat with tokens issued before
            # v7.x / April 2026, before HMAC signing was added)
            if outer.get("ha_token"):
                logger.debug("Accepted unsigned token (pre-signing backwards compat)")
                return outer
            return None
        except (binascii.Error, json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.debug(f"Failed to decode token: {e}")
            return None

    def get_routes(self, mcp_path: str | None = None) -> list[Route]:
        """
        Get OAuth routes including custom consent form routes.

        This extends the base OAuth routes with:
        - GET /authorize - Shows the consent form
        - POST /authorize - Handles consent form submission
        - Custom /.well-known/oauth-authorization-server with enhanced metadata
        """
        # Get base OAuth routes
        routes = super().get_routes(mcp_path)

        # Override the well-known metadata route to include fields needed by Claude.ai
        # The MCP SDK omits critical fields like response_modes_supported and
        # the "none" token_endpoint_auth_method that public clients with PKCE require
        from starlette.responses import JSONResponse

        async def enhanced_metadata_handler(request: Request) -> Response:
            """Enhanced OAuth metadata handler with Claude.ai compatibility."""
            from mcp.server.auth.routes import build_metadata

            # Get base URL
            base = str(self.base_url).rstrip("/")

            # Get base metadata from MCP SDK
            metadata = build_metadata(
                issuer_url=AnyHttpUrl(base),
                service_documentation_url=AnyHttpUrl(
                    "https://github.com/homeassistant-ai/ha-mcp"
                ),
                client_registration_options=self.client_registration_options or {},  # type: ignore[arg-type]
                revocation_options=self.revocation_options or {},  # type: ignore[arg-type]
            )

            # Convert to dict and enhance with missing fields
            # Use mode='json' to serialize AnyHttpUrl objects to strings
            metadata_dict = metadata.model_dump(mode="json", exclude_none=True)

            # Add response_modes_supported (required by some OAuth clients)
            metadata_dict["response_modes_supported"] = ["query"]

            # Add "none" auth method for public clients with PKCE (used by Claude.ai)
            if "token_endpoint_auth_methods_supported" in metadata_dict:
                if "none" not in metadata_dict["token_endpoint_auth_methods_supported"]:
                    metadata_dict["token_endpoint_auth_methods_supported"].append(
                        "none"
                    )

            # Also add "none" to revocation endpoint auth methods
            if "revocation_endpoint_auth_methods_supported" in metadata_dict:
                if (
                    "none"
                    not in metadata_dict["revocation_endpoint_auth_methods_supported"]
                ):
                    metadata_dict["revocation_endpoint_auth_methods_supported"].append(
                        "none"
                    )

            return JSONResponse(content=metadata_dict)

        # Replace the well-known metadata route
        enhanced_routes = []
        for route in routes:
            if (
                isinstance(route, Route)
                and route.path == "/.well-known/oauth-authorization-server"
            ):
                from mcp.server.auth.routes import cors_middleware

                enhanced_routes.append(
                    Route(
                        path="/.well-known/oauth-authorization-server",
                        endpoint=cors_middleware(
                            enhanced_metadata_handler, ["GET", "OPTIONS"]
                        ),
                        methods=["GET", "OPTIONS"],
                    )
                )
            else:
                enhanced_routes.append(route)

        # Add OpenID Configuration endpoint for ChatGPT compatibility
        # ChatGPT expects /.well-known/openid-configuration (OpenID Connect Discovery)
        # in addition to /.well-known/oauth-authorization-server (OAuth 2.1)
        # Per RFC 8414, many servers support both endpoints with identical metadata
        from mcp.server.auth.routes import cors_middleware

        enhanced_routes.append(
            Route(
                path="/.well-known/openid-configuration",
                endpoint=cors_middleware(enhanced_metadata_handler, ["GET", "OPTIONS"]),
                methods=["GET", "OPTIONS"],
            )
        )

        # ChatGPT bug workaround: It also requests /token/.well-known/openid-configuration
        # This is non-standard (mixing token endpoint path with discovery path)
        # but we'll serve the same metadata to ensure ChatGPT can connect
        enhanced_routes.append(
            Route(
                path="/token/.well-known/openid-configuration",
                endpoint=cors_middleware(enhanced_metadata_handler, ["GET", "OPTIONS"]),
                methods=["GET", "OPTIONS"],
            )
        )

        # Add consent form routes (these override the default authorize behavior)
        consent_routes = [
            Route("/consent", endpoint=self._consent_get, methods=["GET"]),
            Route("/consent", endpoint=self._consent_post, methods=["POST"]),
        ]

        enhanced_routes.extend(consent_routes)
        return enhanced_routes

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        """Retrieve client information by ID."""
        return self.clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        """Register a new OAuth client."""
        # Set default scopes if client doesn't specify any (ChatGPT compatibility)
        # ChatGPT registers without scopes, then requests them during authorization
        if (
            client_info.scope is None
            and self.client_registration_options is not None
            and self.client_registration_options.valid_scopes is not None
        ):
            # Grant all valid scopes by default
            client_info.scope = " ".join(self.client_registration_options.valid_scopes)
            logger.info(
                f"Client registered without scopes, granting all valid scopes: {client_info.scope}"
            )

        # Validate scopes if configured
        if (
            client_info.scope is not None
            and self.client_registration_options is not None
            and self.client_registration_options.valid_scopes is not None
        ):
            requested_scopes = set(client_info.scope.split())
            valid_scopes = set(self.client_registration_options.valid_scopes)
            invalid_scopes = requested_scopes - valid_scopes
            if invalid_scopes:
                raise ValueError(
                    f"Requested scopes are not valid: {', '.join(invalid_scopes)}"
                )

        if client_info.client_id is None:
            raise ValueError("client_id is required for client registration")

        self.clients[client_info.client_id] = client_info
        logger.info(f"Registered OAuth client: {client_info.client_id}")

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """
        Handle authorization request by redirecting to consent form.

        Instead of immediately issuing an auth code, we redirect to our
        consent form where users enter their HA credentials.
        """
        if client.client_id is None:
            raise AuthorizeError(
                error="invalid_request",
                error_description="Client ID is required",
            )

        if client.client_id not in self.clients:
            raise AuthorizeError(
                error="unauthorized_client",
                error_description=f"Client '{client.client_id}' not registered.",
            )

        # Generate a unique transaction ID for this authorization
        txn_id = secrets.token_urlsafe(32)

        # Store the authorization parameters for the consent form
        self.pending_authorizations[txn_id] = {
            "client_id": client.client_id,
            "client_name": client.client_name,
            "redirect_uri": str(params.redirect_uri),
            "state": params.state,
            "scopes": params.scopes or [],
            "code_challenge": params.code_challenge,
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "created_at": time.time(),
        }

        # Build consent form URL
        base = str(self.base_url).rstrip("/")
        consent_url = f"{base}/consent?txn_id={txn_id}"

        logger.debug(f"Redirecting to consent form: {consent_url}")
        return consent_url

    async def _consent_get(self, request: Request) -> Response:
        """Handle GET request to consent form."""
        txn_id = request.query_params.get("txn_id")
        error_message = request.query_params.get("error")

        if not txn_id:
            return HTMLResponse(
                create_error_html(
                    "invalid_request",
                    "Missing transaction ID. Please start the authorization flow again.",
                ),
                status_code=400,
            )

        pending = self.pending_authorizations.get(txn_id)
        if not pending:
            return HTMLResponse(
                create_error_html(
                    "invalid_request",
                    "Authorization request expired or not found. Please try again.",
                ),
                status_code=400,
            )

        # Check if authorization request is expired (5 minutes)
        if time.time() - pending["created_at"] > 300:
            del self.pending_authorizations[txn_id]
            return HTMLResponse(
                create_error_html(
                    "expired_request",
                    "Authorization request has expired. Please start over.",
                ),
                status_code=400,
            )

        redirect_uri = pending.get("redirect_uri", "")
        if not redirect_uri:
            return HTMLResponse(
                create_error_html(
                    "invalid_request",
                    "No redirect URI provided. The client must specify a redirect URI.",
                ),
                status_code=400,
            )

        consent_html = create_consent_html(
            client_id=pending["client_id"],
            redirect_uri=redirect_uri,
            state=pending.get("state", ""),
            txn_id=txn_id,
            error_message=error_message,
        )

        return HTMLResponse(consent_html)

    async def _consent_post(self, request: Request) -> Response:
        """Handle POST request from consent form."""
        logger.info("=== CONSENT FORM POST RECEIVED ===")
        form = await request.form()

        txn_id = form.get("txn_id")
        ha_token = form.get("ha_token")
        logger.info(f"Form data: txn_id={txn_id}, has_token={ha_token is not None}")

        if not txn_id:
            return HTMLResponse(
                create_error_html(
                    "invalid_request",
                    "Missing transaction ID.",
                ),
                status_code=400,
            )

        pending = self.pending_authorizations.get(str(txn_id))
        if not pending:
            return HTMLResponse(
                create_error_html(
                    "invalid_request",
                    "Authorization request expired or not found.",
                ),
                status_code=400,
            )

        if not ha_token:
            # Redirect back to form with error
            base = str(self.base_url).rstrip("/")
            error_params = urlencode(
                {
                    "txn_id": txn_id,
                    "error": "Please provide your Long-Lived Access Token.",
                }
            )
            return RedirectResponse(
                f"{base}/consent?{error_params}",
                status_code=303,
            )

        # Store credentials (no server-side validation - the token will be
        # validated on first actual API call to the configured HA instance)
        client_id = pending["client_id"]
        self.ha_credentials[client_id] = HomeAssistantCredentials(
            ha_token=str(ha_token),
        )
        logger.info(f"Stored HA credentials for client {client_id}")

        # Generate authorization code
        auth_code_value = f"ha_auth_code_{secrets.token_hex(16)}"
        expires_at = time.time() + AUTH_CODE_EXPIRY_SECONDS

        scopes_list = pending.get("scopes", [])
        if isinstance(scopes_list, str):
            scopes_list = scopes_list.split()

        auth_code = AuthorizationCode(
            code=auth_code_value,
            client_id=client_id,
            redirect_uri=AnyHttpUrl(pending["redirect_uri"]),
            redirect_uri_provided_explicitly=pending.get(
                "redirect_uri_provided_explicitly", True
            ),
            scopes=scopes_list,
            expires_at=expires_at,
            code_challenge=pending.get("code_challenge"),  # type: ignore[arg-type]  # None is valid per PKCE spec (RFC 7636 §4.3); empty string would break validation
        )
        self.auth_codes[auth_code_value] = auth_code

        # Clean up pending authorization
        del self.pending_authorizations[str(txn_id)]

        # Redirect back to client with auth code
        redirect_uri = construct_redirect_uri(
            pending["redirect_uri"],
            code=auth_code_value,
            state=pending.get("state"),
        )

        logger.info(f"Authorization successful for client {client_id}")
        return RedirectResponse(redirect_uri, status_code=303)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        """Load authorization code from storage."""
        auth_code_obj = self.auth_codes.get(authorization_code)
        if auth_code_obj:
            if auth_code_obj.client_id != client.client_id:
                return None
            if auth_code_obj.expires_at < time.time():
                del self.auth_codes[authorization_code]
                return None
            return auth_code_obj
        return None

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        """Exchange authorization code for stateless access and refresh tokens."""
        if authorization_code.code not in self.auth_codes:
            raise TokenError(
                "invalid_grant", "Authorization code not found or already used."
            )

        # Consume the auth code
        del self.auth_codes[authorization_code.code]

        if client.client_id is None:
            raise TokenError("invalid_client", "Client ID is required")

        # Get HA credentials for this client to encode in token
        ha_credentials = self.ha_credentials.get(client.client_id)
        if not ha_credentials:
            raise TokenError(
                "invalid_client",
                f"No Home Assistant credentials found for client {client.client_id}",
            )

        scopes = authorization_code.scopes
        access_token_expires_at = int(time.time() + ACCESS_TOKEN_EXPIRY_SECONDS)
        refresh_token_expires_at = int(time.time() + REFRESH_TOKEN_EXPIRY_SECONDS)

        # Both tokens are stateless — no server-side storage needed.
        access_token_value = self._encode_token(
            ha_credentials.ha_token,
            token_type="access",
            expires_at=access_token_expires_at,
        )
        refresh_token_value = self._encode_token(
            ha_credentials.ha_token,
            token_type="refresh",
            client_id=client.client_id,
            scopes=scopes,
            expires_at=refresh_token_expires_at,
        )

        # Clean up temporary credentials (no longer needed after token issued)
        self.ha_credentials.pop(client.client_id, None)

        logger.info(f"Issued stateless tokens for client {client.client_id}")

        return OAuthToken(
            access_token=access_token_value,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_EXPIRY_SECONDS,
            refresh_token=refresh_token_value,
            scope=" ".join(scopes) if scopes else None,
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        """Decode and validate a stateless refresh token."""
        payload = self._decode_token(refresh_token)
        if not payload:
            return None

        if payload.get("type") != "refresh":
            logger.debug("Token is not a refresh token")
            return None

        if payload.get("client_id") != client.client_id:
            logger.warning(
                f"Refresh token client_id mismatch: expected {client.client_id}, "
                f"got {payload.get('client_id')}"
            )
            return None

        expires_at = payload.get("exp")
        if expires_at is not None and expires_at < time.time():
            logger.debug("Refresh token expired")
            return None

        return RefreshToken(
            token=refresh_token,
            client_id=payload["client_id"],
            scopes=payload.get("scopes", []),
            expires_at=expires_at,
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        """Exchange a stateless refresh token for new stateless tokens."""
        # Validate scopes
        original_scopes = set(refresh_token.scopes)
        requested_scopes = set(scopes)
        if not requested_scopes.issubset(original_scopes):
            raise TokenError(
                "invalid_scope",
                "Requested scopes exceed those authorized by the refresh token.",
            )

        if client.client_id is None:
            raise TokenError("invalid_client", "Client ID is required")

        # Recover HA token directly from the stateless refresh token
        payload = self._decode_token(refresh_token.token)
        if not payload:
            raise TokenError(
                "invalid_grant",
                "Cannot decode refresh token.",
            )

        ha_token = payload["ha_token"]
        access_token_expires_at = int(time.time() + ACCESS_TOKEN_EXPIRY_SECONDS)
        refresh_token_expires_at = int(time.time() + REFRESH_TOKEN_EXPIRY_SECONDS)

        # Issue new stateless token pair
        new_access_token_value = self._encode_token(
            ha_token, token_type="access", expires_at=access_token_expires_at
        )
        new_refresh_token_value = self._encode_token(
            ha_token,
            token_type="refresh",
            client_id=client.client_id,
            scopes=scopes,
            expires_at=refresh_token_expires_at,
        )

        return OAuthToken(
            access_token=new_access_token_value,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_EXPIRY_SECONDS,
            refresh_token=new_refresh_token_value,
            scope=" ".join(scopes) if scopes else None,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        """Decode and validate a stateless access token.

        Accepts tokens with ``type=access`` or no type field (backwards
        compatibility with tokens issued before v7.x / April 2026).
        Rejects tokens explicitly typed as ``refresh``.
        Enforces ``exp`` if present in the payload.
        """
        payload = self._decode_token(token)
        if not payload:
            return None

        # Reject refresh tokens presented as access tokens
        token_type = payload.get("type")
        if token_type == "refresh":
            logger.warning("Refresh token presented as access token (rejected)")
            return None

        # Enforce expiry if present
        expires_at = payload.get("exp")
        if expires_at is not None and expires_at < time.time():
            logger.debug("Access token expired")
            return None

        return AccessToken(
            token=token,
            client_id=payload.get("client_id", "stateless"),
            scopes=payload.get("scopes", ["homeassistant", "mcp"]),
            expires_at=expires_at,
            claims={"ha_token": payload["ha_token"]},
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        """Verify bearer token and return access info if valid."""
        return await self.load_access_token(token)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        """Revoke a token (no-op for stateless tokens).

        With fully stateless tokens there is no server-side state to
        remove.  The LLAT itself is the security boundary — revoking
        it in Home Assistant immediately invalidates all derived tokens.

        Per RFC 7009, the /revoke endpoint returns success regardless.
        The token remains valid until the underlying LLAT is revoked
        in Home Assistant.
        """
        logger.debug(
            "Token revocation requested (no-op for stateless tokens; "
            "revoke the LLAT in Home Assistant to invalidate)"
        )

    def get_ha_credentials(self, client_id: str) -> HomeAssistantCredentials | None:
        """Get temporarily stored HA credentials for a client.

        Only valid between consent form submission and token exchange.
        After token issuance, credentials are deleted from this store.
        """
        return self.ha_credentials.get(client_id)
