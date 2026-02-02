"""
Home Assistant OAuth 2.1 Provider.

This module implements OAuth 2.1 authentication with Dynamic Client Registration (DCR)
for Home Assistant MCP Server. Users authenticate via a consent form where they
provide their Home Assistant URL and Long-Lived Access Token (LLAT).
"""

import logging
import os
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet
from fastmcp.server.auth.auth import AccessToken  # FastMCP version has claims field
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

from fastmcp.server.auth.auth import (
    ClientRegistrationOptions,
    OAuthProvider,
    RevocationOptions,
)

from .consent_form import create_consent_html, create_error_html

logger = logging.getLogger(__name__)

# Token expiration times
AUTH_CODE_EXPIRY_SECONDS = 5 * 60  # 5 minutes
ACCESS_TOKEN_EXPIRY_SECONDS = 60 * 60  # 1 hour
REFRESH_TOKEN_EXPIRY_SECONDS = 7 * 24 * 60 * 60  # 7 days


class HomeAssistantCredentials:
    """Stores Home Assistant credentials for a client."""

    def __init__(self, ha_url: str, ha_token: str):
        self.ha_url = ha_url.rstrip("/")
        self.ha_token = ha_token
        self.validated_at = time.time()

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for storage."""
        return {
            "ha_url": self.ha_url,
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
    - Token management with refresh token support

    The consent form collects the user's Home Assistant URL and
    Long-Lived Access Token, validates them, and stores them
    securely for subsequent API calls.
    """

    def __init__(
        self,
        base_url: AnyHttpUrl | str | None = None,
        issuer_url: AnyHttpUrl | str | None = None,
        service_documentation_url: AnyHttpUrl | str | None = None,
        client_registration_options: ClientRegistrationOptions | None = None,
        revocation_options: RevocationOptions | None = None,
        required_scopes: list[str] | None = None,
    ):
        """
        Initialize the Home Assistant OAuth provider.

        Args:
            base_url: The public URL of this MCP server (auto-detected if not provided)
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

        # In-memory storage
        self.clients: dict[str, OAuthClientInformationFull] = {}
        self.auth_codes: dict[str, AuthorizationCode] = {}
        self.access_tokens: dict[str, AccessToken] = {}
        self.refresh_tokens: dict[str, RefreshToken] = {}

        # Home Assistant credentials storage (keyed by client_id)
        self.ha_credentials: dict[str, HomeAssistantCredentials] = {}

        # Pending authorization requests (for consent form flow)
        self.pending_authorizations: dict[str, dict[str, Any]] = {}

        # Token mapping for revocation
        self._access_to_refresh_map: dict[str, str] = {}
        self._refresh_to_access_map: dict[str, str] = {}

        # Initialize encryption key for stateless tokens
        self._encryption_key = self._get_or_create_encryption_key()
        self._cipher = Fernet(self._encryption_key)

        # Auto-detected base URL (cached from first request)
        self._detected_base_url: str | None = None

        if base_url:
            logger.info(f"HomeAssistantOAuthProvider initialized with base_url={base_url}")
        else:
            logger.info("HomeAssistantOAuthProvider initialized (base_url will be auto-detected)")

    def _get_or_create_encryption_key(self) -> bytes:
        """
        Get encryption key from environment, file, or generate a new one.

        Priority:
        1. OAUTH_ENCRYPTION_KEY environment variable (for advanced users)
        2. Persistent key file at ~/.ha-mcp/oauth_key (auto-generated)
        3. Generate temporary key (dev/testing only)
        """
        # Check environment variable first (highest priority)
        key_str = os.getenv("OAUTH_ENCRYPTION_KEY")
        if key_str:
            logger.info("Using OAUTH_ENCRYPTION_KEY from environment")
            return key_str.encode()

        # Try to load from persistent file
        from pathlib import Path
        key_file = Path.home() / ".ha-mcp" / "oauth_key"

        if key_file.exists():
            try:
                key_bytes = key_file.read_bytes()
                # Validate it's a proper Fernet key
                Fernet(key_bytes)
                logger.info(f"Using persistent OAuth key from {key_file}")
                return key_bytes
            except Exception as e:
                logger.warning(
                    f"Failed to load OAuth key from {key_file}: {e}. "
                    "Generating new key."
                )

        # Generate new key and persist it
        key = Fernet.generate_key()
        try:
            key_file.parent.mkdir(parents=True, exist_ok=True)
            key_file.write_bytes(key)
            # Set restrictive permissions (owner read/write only)
            os.chmod(key_file, 0o600)
            logger.info(
                f"Generated new OAuth encryption key and saved to {key_file}. "
                "This key will persist across server restarts. "
                "To share across multiple instances, copy this file or set OAUTH_ENCRYPTION_KEY."
            )
        except Exception as e:
            logger.warning(
                f"Failed to persist OAuth key to {key_file}: {e}. "
                "Using temporary key - tokens will be invalid after restart."
            )

        return key

    def _encrypt_credentials(self, ha_url: str, ha_token: str) -> str:
        """Encrypt HA credentials into a token string."""
        credentials_str = f"{ha_url}|{ha_token}"
        encrypted = self._cipher.encrypt(credentials_str.encode())
        return encrypted.decode()

    def _decrypt_credentials(self, token: str) -> tuple[str, str] | None:
        """Decrypt token to get HA credentials. Returns (ha_url, ha_token) or None."""
        try:
            decrypted = self._cipher.decrypt(token.encode())
            credentials_str = decrypted.decode()
            parts = credentials_str.split("|", 1)
            if len(parts) == 2:
                return parts[0], parts[1]
            return None
        except Exception as e:
            logger.debug(f"Failed to decrypt token: {e}")
            return None

    def _get_base_url(self, request: Request | None = None) -> str:
        """
        Get the base URL, auto-detecting from request if not configured.

        Args:
            request: Starlette request object for auto-detection

        Returns:
            Base URL string
        """
        # Use configured base_url if available
        if self.base_url:
            return str(self.base_url).rstrip('/')

        # Use cached detected URL if available
        if self._detected_base_url:
            return self._detected_base_url

        # Auto-detect from request
        if request:
            # Get protocol from X-Forwarded-Proto or request scheme
            proto = request.headers.get(
                "X-Forwarded-Proto",
                "https" if request.url.scheme == "https" else "http"
            )

            # Get host from X-Forwarded-Host or Host header
            host = request.headers.get(
                "X-Forwarded-Host",
                request.headers.get("Host", request.url.netloc)
            )

            # Remove any path components (we only want the origin)
            base = f"{proto}://{host}"

            # Cache the detected URL
            self._detected_base_url = base
            logger.info(f"Auto-detected base URL from request: {base}")

            return base

        # Fallback to localhost (shouldn't happen in production)
        logger.warning(
            "No base_url configured and no request available for auto-detection. "
            "Using http://localhost:8086 as fallback. "
            "Set MCP_BASE_URL environment variable for production."
        )
        return "http://localhost:8086"

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

            # Get base URL (configured or auto-detected)
            base = self._get_base_url(request)

            # Get base metadata from MCP SDK
            metadata = build_metadata(
                issuer_url=AnyHttpUrl(base),
                service_documentation_url=AnyHttpUrl("https://github.com/homeassistant-ai/ha-mcp"),
                client_registration_options=self.client_registration_options or {},  # type: ignore[arg-type]
                revocation_options=self.revocation_options or {},  # type: ignore[arg-type]
            )

            # Convert to dict and enhance with missing fields
            # Use mode='json' to serialize AnyHttpUrl objects to strings
            metadata_dict = metadata.model_dump(mode='json', exclude_none=True)

            # Add response_modes_supported (required by some OAuth clients)
            metadata_dict["response_modes_supported"] = ["query"]

            # Add "none" auth method for public clients with PKCE (used by Claude.ai)
            if "token_endpoint_auth_methods_supported" in metadata_dict:
                if "none" not in metadata_dict["token_endpoint_auth_methods_supported"]:
                    metadata_dict["token_endpoint_auth_methods_supported"].append("none")

            # Also add "none" to revocation endpoint auth methods
            if "revocation_endpoint_auth_methods_supported" in metadata_dict:
                if "none" not in metadata_dict["revocation_endpoint_auth_methods_supported"]:
                    metadata_dict["revocation_endpoint_auth_methods_supported"].append("none")

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
                endpoint=cors_middleware(
                    enhanced_metadata_handler, ["GET", "OPTIONS"]
                ),
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
            logger.info(f"Client registered without scopes, granting all valid scopes: {client_info.scope}")

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
                error="invalid_client",
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
        base = self._get_base_url()
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

        html = create_consent_html(
            client_id=pending["client_id"],
            client_name=pending.get("client_name"),
            redirect_uri=pending["redirect_uri"],
            state=pending.get("state", ""),
            scopes=pending.get("scopes", []),
            error_message=error_message,
        )

        # Add txn_id as hidden field
        html = html.replace(
            '<input type="hidden" name="client_id"',
            f'<input type="hidden" name="txn_id" value="{txn_id}">\n            <input type="hidden" name="client_id"',
        )

        return HTMLResponse(html)

    async def _consent_post(self, request: Request) -> Response:
        """Handle POST request from consent form."""
        logger.info("📝 === CONSENT FORM POST RECEIVED ===")
        form = await request.form()

        txn_id = form.get("txn_id")
        ha_url = form.get("ha_url")
        ha_token = form.get("ha_token")
        logger.info(f"📝 Form data: txn_id={txn_id}, ha_url={ha_url}, has_token={ha_token is not None}")

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

        if not ha_url or not ha_token:
            # Redirect back to form with error
            base = self._get_base_url(request)
            error_params = urlencode(
                {
                    "txn_id": txn_id,
                    "error": "Please provide both Home Assistant URL and access token.",
                }
            )
            return RedirectResponse(
                f"{base}/consent?{error_params}",
                status_code=303,
            )

        # Validate HA credentials
        validation_error = await self._validate_ha_credentials(
            str(ha_url), str(ha_token)
        )
        if validation_error:
            base = self._get_base_url(request)
            error_params = urlencode(
                {
                    "txn_id": txn_id,
                    "error": validation_error,
                }
            )
            return RedirectResponse(
                f"{base}/consent?{error_params}",
                status_code=303,
            )

        # Store validated credentials
        client_id = pending["client_id"]
        self.ha_credentials[client_id] = HomeAssistantCredentials(
            ha_url=str(ha_url),
            ha_token=str(ha_token),
        )
        logger.info(f"✅ Stored HA credentials for client {client_id}: {str(ha_url)}")

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
            code_challenge=pending.get("code_challenge"),
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

    async def _validate_ha_credentials(self, ha_url: str, ha_token: str) -> str | None:
        """
        Validate Home Assistant credentials by making a test API call.

        Args:
            ha_url: Home Assistant URL
            ha_token: Long-Lived Access Token

        Returns:
            Error message if validation failed, None if successful
        """
        try:
            ha_url = ha_url.rstrip("/")

            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    f"{ha_url}/api/config",
                    headers={
                        "Authorization": f"Bearer {ha_token}",
                        "Content-Type": "application/json",
                    },
                )

                if response.status_code == 401:
                    return "Invalid access token. Please check your Long-Lived Access Token."

                if response.status_code == 403:
                    return "Access forbidden. The token may not have sufficient permissions."

                if response.status_code >= 400:
                    return f"Failed to connect to Home Assistant: HTTP {response.status_code}"

                # Verify we got a valid config response
                try:
                    config = response.json()
                    if "location_name" not in config and "version" not in config:
                        return "Invalid response from Home Assistant. Please check the URL."
                except Exception:
                    return "Invalid response format from Home Assistant."

                logger.info(
                    f"Validated HA credentials for {config.get('location_name', 'Unknown')}"
                )
                return None

        except httpx.ConnectError:
            return "Could not connect to Home Assistant. Please check the URL and ensure Home Assistant is accessible."
        except httpx.TimeoutException:
            return "Connection to Home Assistant timed out. Please check the URL."
        except Exception as e:
            logger.error(f"Error validating HA credentials: {e}")
            return f"Failed to validate credentials: {str(e)}"

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
        """Exchange authorization code for access and refresh tokens."""
        if authorization_code.code not in self.auth_codes:
            raise TokenError(
                "invalid_grant", "Authorization code not found or already used."
            )

        # Consume the auth code
        del self.auth_codes[authorization_code.code]

        if client.client_id is None:
            raise TokenError("invalid_client", "Client ID is required")

        # Generate tokens
        access_token_value = f"ha_access_{secrets.token_hex(32)}"
        refresh_token_value = f"ha_refresh_{secrets.token_hex(32)}"

        access_token_expires_at = int(time.time() + ACCESS_TOKEN_EXPIRY_SECONDS)
        refresh_token_expires_at = int(time.time() + REFRESH_TOKEN_EXPIRY_SECONDS)

        # Get HA credentials for this client to encrypt in token
        ha_credentials = self.ha_credentials.get(client.client_id)
        if not ha_credentials:
            raise TokenError(
                "server_error",
                f"No Home Assistant credentials found for client {client.client_id}",
            )

        # STATELESS TOKEN: Encrypt HA credentials directly into the access token
        # No server-side storage needed - token contains everything
        access_token_value = self._encrypt_credentials(
            ha_credentials.ha_url, ha_credentials.ha_token
        )

        # Still use random string for refresh token (less sensitive, can be in memory)
        # Refresh tokens are less frequently used and don't need to carry credentials
        self.refresh_tokens[refresh_token_value] = RefreshToken(
            token=refresh_token_value,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=refresh_token_expires_at,
        )

        # Map for revocation (refresh token only, access token is stateless)
        self._refresh_to_access_map[refresh_token_value] = client.client_id or ""

        # Clean up temporary credentials storage (no longer needed after token issued)
        if client.client_id in self.ha_credentials:
            del self.ha_credentials[client.client_id]

        logger.info(f"Issued encrypted stateless access token for client {client.client_id}")

        return OAuthToken(
            access_token=access_token_value,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_EXPIRY_SECONDS,
            refresh_token=refresh_token_value,
            scope=(
                " ".join(authorization_code.scopes)
                if authorization_code.scopes
                else None
            ),
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        """Load refresh token from storage."""
        token_obj = self.refresh_tokens.get(refresh_token)
        if token_obj:
            if token_obj.client_id != client.client_id:
                return None
            if token_obj.expires_at is not None and token_obj.expires_at < time.time():
                self._revoke_internal(refresh_token_str=token_obj.token)
                return None
            return token_obj
        return None

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        """Exchange refresh token for new access token."""
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

        # Preserve claims from old access token before revoking
        old_access_token_str = self._refresh_to_access_map.get(refresh_token.token)
        old_claims = {}
        if old_access_token_str and old_access_token_str in self.access_tokens:
            old_access_token = self.access_tokens[old_access_token_str]
            old_claims = old_access_token.claims or {}

        # Revoke old tokens
        self._revoke_internal(refresh_token_str=refresh_token.token)

        # Issue new tokens
        new_access_token_value = f"ha_access_{secrets.token_hex(32)}"
        new_refresh_token_value = f"ha_refresh_{secrets.token_hex(32)}"

        access_token_expires_at = int(time.time() + ACCESS_TOKEN_EXPIRY_SECONDS)
        refresh_token_expires_at = int(time.time() + REFRESH_TOKEN_EXPIRY_SECONDS)

        # Preserve HA credentials in new access token claims
        self.access_tokens[new_access_token_value] = AccessToken(
            token=new_access_token_value,
            client_id=client.client_id,
            scopes=scopes,
            expires_at=access_token_expires_at,
            claims=old_claims,  # Preserve HA credentials across token refresh
        )

        self.refresh_tokens[new_refresh_token_value] = RefreshToken(
            token=new_refresh_token_value,
            client_id=client.client_id,
            scopes=scopes,
            expires_at=refresh_token_expires_at,
        )

        self._access_to_refresh_map[new_access_token_value] = new_refresh_token_value
        self._refresh_to_access_map[new_refresh_token_value] = new_access_token_value

        return OAuthToken(
            access_token=new_access_token_value,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_EXPIRY_SECONDS,
            refresh_token=new_refresh_token_value,
            scope=" ".join(scopes) if scopes else None,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        """
        Load and validate access token.

        STATELESS: Decrypts token to extract HA credentials.
        No server-side storage needed - token is self-contained.
        """

        # Decrypt token to get HA credentials
        credentials = self._decrypt_credentials(token)
        if not credentials:
            return None

        ha_url, ha_token = credentials

        # Create AccessToken object with decrypted credentials in claims
        # No expiry check - tokens don't expire (encryption key rotation handles security)
        return AccessToken(
            token=token,
            client_id="decrypted",  # We don't store client_id in token
            scopes=["homeassistant", "mcp"],
            expires_at=None,  # Stateless tokens don't expire
            claims={
                "ha_url": ha_url,
                "ha_token": ha_token,
            },
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        """Verify bearer token and return access info if valid."""
        return await self.load_access_token(token)

    def _revoke_internal(
        self, access_token_str: str | None = None, refresh_token_str: str | None = None
    ) -> None:
        """Internal helper to remove tokens and their associations."""
        if access_token_str:
            if access_token_str in self.access_tokens:
                del self.access_tokens[access_token_str]

            associated_refresh = self._access_to_refresh_map.pop(access_token_str, None)
            if associated_refresh:
                if associated_refresh in self.refresh_tokens:
                    del self.refresh_tokens[associated_refresh]
                self._refresh_to_access_map.pop(associated_refresh, None)

        if refresh_token_str:
            if refresh_token_str in self.refresh_tokens:
                del self.refresh_tokens[refresh_token_str]

            associated_access = self._refresh_to_access_map.pop(refresh_token_str, None)
            if associated_access:
                if associated_access in self.access_tokens:
                    del self.access_tokens[associated_access]
                self._access_to_refresh_map.pop(associated_access, None)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        """Revoke an access or refresh token."""
        if isinstance(token, AccessToken):
            self._revoke_internal(access_token_str=token.token)
        elif isinstance(token, RefreshToken):
            self._revoke_internal(refresh_token_str=token.token)

    def get_ha_credentials(self, client_id: str) -> HomeAssistantCredentials | None:
        """
        Get Home Assistant credentials for a client.

        This is used by the MCP server to get the HA URL and token
        for making API calls on behalf of the authenticated user.

        Args:
            client_id: The OAuth client ID

        Returns:
            HomeAssistantCredentials if found, None otherwise
        """
        return self.ha_credentials.get(client_id)

    def get_ha_credentials_for_token(
        self, access_token: str
    ) -> HomeAssistantCredentials | None:
        """
        Get Home Assistant credentials for an access token.

        Args:
            access_token: The access token string

        Returns:
            HomeAssistantCredentials if found, None otherwise
        """
        token_obj = self.access_tokens.get(access_token)
        if token_obj and token_obj.client_id:
            return self.ha_credentials.get(token_obj.client_id)
        return None
