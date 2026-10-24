"""
Home Assistant OAuth 2.1 Provider.

This module implements OAuth 2.1 authentication with Dynamic Client Registration (DCR)
for Home Assistant MCP Server. Users authenticate via a consent form where they
provide their Long-Lived Access Token (LLAT).
"""

import binascii
import contextlib
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from pydantic import AnyHttpUrl, Field, ValidationError
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp

from ha_mcp._vendor.fastmcp.server.auth.auth import (
    AccessToken,  # FastMCP version has claims field
    ClientRegistrationOptions,
    OAuthProvider,
    RevocationOptions,
)
from ha_mcp._vendor.mcp.server.auth.provider import (
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from ha_mcp._vendor.mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from ..utils.data_paths import get_data_dir
from .consent_form import create_consent_html, create_error_html
from .iss_stamp import StampIssOnErrorRedirects

logger = logging.getLogger(__name__)

# Expiry windows
PENDING_AUTHORIZATION_EXPIRY_SECONDS = 5 * 60  # window to submit the consent form
AUTH_CODE_EXPIRY_SECONDS = 5 * 60
ACCESS_TOKEN_EXPIRY_SECONDS = 60 * 60
REFRESH_TOKEN_EXPIRY_SECONDS = 7 * 24 * 60 * 60


class HomeAssistantAuthorizationCode(AuthorizationCode):
    """Authorization code carrying the LLAT the consent form bound to it.

    The token travels with the code rather than in a per-client store so that
    two consent submissions for the same client can never hand one submitter's
    credential to the other at the token endpoint.
    """

    # repr=False keeps the secret out of logs and tracebacks that print the code.
    ha_token: str = Field(repr=False, min_length=1)


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
    Tokens are stateless (HMAC-signed base64 JSON) so no per-token state is
    stored server-side.

    DCR client registrations and the HMAC signing secret are persisted to
    ``get_data_dir() / "oauth_clients.json"`` and
    ``get_data_dir() / "oauth_hmac_secret"`` respectively, so registered
    clients survive container restarts.  If the data directory is not
    writable (e.g. Docker without a mounted volume) the server falls back
    to in-memory-only storage and logs a warning; existing clients must
    re-register after each restart in that case.

    Security comes from HTTPS transport and the LLAT itself being the
    authorization boundary — revoking the LLAT in Home Assistant
    immediately invalidates all derived tokens.
    See SECURITY.md § "OAuth Bearer token design" for the full rationale
    and the intended revocation path.
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

        # DCR client registry — persisted to disk so clients survive restarts.
        self.clients: dict[str, OAuthClientInformationFull] = self._load_clients()

        # Short-lived in-memory state (no persistence needed or wanted). Each
        # code carries the LLAT submitted for it, held until exchanged or
        # evicted as expired.
        self.auth_codes: dict[str, HomeAssistantAuthorizationCode] = {}

        # Pending authorization requests (for consent form flow)
        self.pending_authorizations: dict[str, dict[str, Any]] = {}

        # Server-side secret for HMAC-protecting LLATs in refresh tokens.
        # Persisted so existing refresh tokens survive server restarts.
        self._hmac_secret = self._load_hmac_secret()

        logger.info(f"HomeAssistantOAuthProvider initialized with base_url={base_url}")

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _clients_file() -> Path:
        return get_data_dir() / "oauth_clients.json"

    @staticmethod
    def _hmac_secret_file() -> Path:
        return get_data_dir() / "oauth_hmac_secret"

    def _load_clients(self) -> dict[str, OAuthClientInformationFull]:
        """Load persisted DCR client registrations from disk.

        Returns an empty dict if the file is absent, unreadable, or corrupt.
        """
        path = self._clients_file()
        try:
            data: dict[str, Any] = json.loads(path.read_text())
            clients = {
                cid: OAuthClientInformationFull.model_validate(c)
                for cid, c in data.items()
            }
            logger.info("Loaded %d OAuth client(s) from %s", len(clients), path)
            return clients
        except FileNotFoundError:
            logger.debug("No persistent OAuth client registry found at %s", path)
            return {}
        except (json.JSONDecodeError, OSError, ValidationError) as exc:
            logger.warning(
                "Could not load OAuth client registry from %s (%s: %s); "
                "starting with empty registry.",
                path,
                type(exc).__name__,
                exc,
            )
            return {}

    def _save_clients(self) -> None:
        """Persist DCR client registrations to disk.

        Failures are logged as warnings; the server continues with in-memory
        state so existing sessions are not disrupted.
        """
        if not self.clients:
            return
        path = self._clients_file()
        # Serialize before touching the filesystem. A model that won't
        # serialize is a programming error and should surface loudly, not be
        # swallowed as if it were a transient disk fault in the write below.
        data = {
            cid: client.model_dump(mode="json") for cid, client in self.clients.items()
        }
        payload = json.dumps(data, indent=2).encode()
        tmp_path = path.parent / f".{path.name}.tmp"
        try:
            # Write to a temp file first, then atomically rename. Use os.open
            # with mode 0o600 (owner read/write only) because a confidential
            # client's client_secret can be stored in this file.
            fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, payload)
            finally:
                os.close(fd)
            os.replace(str(tmp_path), str(path))
            logger.debug("Persisted %d OAuth client(s) to %s", len(data), path)
        except OSError as exc:
            with contextlib.suppress(OSError):
                tmp_path.unlink()
            logger.warning(
                "Failed to persist OAuth client registry to %s (%s: %s). "
                "Clients will need to re-register after the next restart. "
                "Mount a persistent volume to fix this: "
                "docker run -v ha-mcp-data:/home/mcpuser/.ha-mcp ...",
                path,
                type(exc).__name__,
                exc,
            )

    def _load_hmac_secret(self) -> bytes:
        """Load or generate the HMAC signing secret.

        Persists a newly generated secret so refresh tokens survive restarts.
        Falls back to a fresh (non-persisted) secret if the file is unreadable.
        """
        path = self._hmac_secret_file()
        try:
            hex_secret = path.read_text().strip()
            if not hex_secret:
                raise ValueError("HMAC secret file is empty")
            secret = bytes.fromhex(hex_secret)
            if len(secret) != 32:
                raise ValueError(
                    f"Invalid HMAC secret length: {len(secret)} bytes (expected 32)"
                )
            logger.debug("Loaded HMAC secret from %s", path)
            return secret
        except FileNotFoundError:
            pass  # first start — generate and persist below
        except (OSError, ValueError) as exc:
            logger.warning(
                "Could not load HMAC secret from %s (%s: %s); "
                "generating a new one — existing refresh tokens will be invalidated.",
                path,
                type(exc).__name__,
                exc,
            )

        secret = secrets.token_bytes(32)
        tmp_path = path.parent / f".{path.name}.tmp"
        try:
            # Write to a temp file first, then atomically rename — prevents a
            # partial write from corrupting the secret on an interrupted flush.
            # os.open with mode 0o600 keeps the file owner read/write only.
            fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, secret.hex().encode())
            finally:
                os.close(fd)
            os.replace(str(tmp_path), str(path))
            logger.debug("Persisted new HMAC secret to %s", path)
        except OSError as exc:
            with contextlib.suppress(OSError):
                tmp_path.unlink()
            logger.warning(
                "Failed to persist HMAC secret to %s (%s: %s). "
                "Refresh tokens will be invalidated on the next restart.",
                path,
                type(exc).__name__,
                exc,
            )
        return secret

    # ------------------------------------------------------------------
    # OAuthProvider interface
    # ------------------------------------------------------------------

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

        The signing secret is persisted (see ``_load_hmac_secret``), so signed
        tokens remain valid across server restarts. Clients re-authenticate via
        the consent form only when a token expires or the secret is rotated.
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

            # Reject unsigned tokens — HMAC secret is regenerated on restart,
            # so no legitimate unsigned tokens can exist on a running server.
            return None
        except (binascii.Error, json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.debug(f"Failed to decode token: {e}")
            return None

    def _issuer(self) -> str:
        """The issuer identifier this authorization server advertises.

        Single source for the ``issuer`` field of the metadata document served
        below and for the RFC 9207 ``iss`` authorization-response parameter,
        which RFC 9207 §2 requires to equal the advertised issuer exactly.
        """
        return str(AnyHttpUrl(str(self.base_url).rstrip("/")))

    def _ensure_single_iss(self, url: str) -> str:
        """Return ``url`` carrying the RFC 9207 ``iss`` parameter exactly once.

        A registered redirect URI may itself carry an ``iss`` query parameter;
        blindly appending ours would produce an ambiguous double-``iss``
        response — and common parsers take the FIRST value, which is precisely
        the issuer mix-up RFC 9207 exists to prevent. Any pre-existing ``iss``
        is dropped so the one this server stamps is authoritative.
        """
        parsed = urlparse(url)
        params = [
            (k, v) for k, vs in parse_qs(parsed.query).items() for v in vs if k != "iss"
        ]
        params.append(("iss", self._issuer()))
        return urlunparse(parsed._replace(query=urlencode(params)))

    def _wrap_authorize_with_iss(self, endpoint: ASGIApp) -> ASGIApp:
        """Wrap the SDK's ``/authorize`` app to stamp ``iss`` on its error redirects.

        The SDK's ``AuthorizationHandler`` builds OAuth error authorization
        responses (invalid scope, ``authorize()`` raising after redirect-URI
        validation) itself, without the RFC 9207 ``iss`` parameter — only our
        consent-form success path carries it otherwise. Success on this route
        is a same-origin redirect to the consent form (no ``error``), which is
        not an authorization response and stays untouched.
        """
        return StampIssOnErrorRedirects(endpoint, self._ensure_single_iss)

    def get_routes(self, mcp_path: str | None = None) -> list[Route]:
        """
        Get OAuth routes including custom consent form routes.

        This extends the base OAuth routes with:
        - /authorize - the SDK handler, its error redirects stamped with ``iss``;
          success redirects to /consent
        - GET/POST /consent - the consent form and its submission
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
            from ha_mcp._vendor.mcp.server.auth.routes import build_metadata

            # Get base metadata from MCP SDK
            metadata = build_metadata(
                issuer_url=AnyHttpUrl(self._issuer()),
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

            # RFC 9207 §3: advertise that authorization responses carry the
            # ``iss`` parameter — omission means "not supported" to
            # discovery-driven clients even though redirects now include it.
            metadata_dict["authorization_response_iss_parameter_supported"] = True

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
                from ha_mcp._vendor.mcp.server.auth.routes import cors_middleware

                enhanced_routes.append(
                    Route(
                        path="/.well-known/oauth-authorization-server",
                        endpoint=cors_middleware(
                            enhanced_metadata_handler, ["GET", "OPTIONS"]
                        ),
                        methods=["GET", "OPTIONS"],
                    )
                )
            elif isinstance(route, Route) and route.path == "/authorize":
                # RFC 9207: the SDK handler's own error redirects must carry
                # ``iss`` too — see _wrap_authorize_with_iss.
                enhanced_routes.append(
                    Route(
                        path=route.path,
                        endpoint=self._wrap_authorize_with_iss(route.endpoint),
                        methods=list(route.methods or []),
                    )
                )
            else:
                enhanced_routes.append(route)

        # Add OpenID Configuration endpoint for ChatGPT compatibility
        # ChatGPT expects /.well-known/openid-configuration (OpenID Connect Discovery)
        # in addition to /.well-known/oauth-authorization-server (OAuth 2.1)
        # Per RFC 8414, many servers support both endpoints with identical metadata
        from ha_mcp._vendor.mcp.server.auth.routes import cors_middleware

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
        self._save_clients()

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

        self._evict_expired(time.time())

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

    @staticmethod
    def _pending_expired(pending: dict[str, Any], now: float) -> bool:
        created_at = float(pending["created_at"])
        return now - created_at > PENDING_AUTHORIZATION_EXPIRY_SECONDS

    def _evict_expired(self, now: float) -> None:
        """Drop pending transactions and codes past their expiry.

        A lookup only drops the one expired entry it finds, so an abandoned
        consent form, or an unredeemed code and its LLAT, would otherwise stay
        in memory for the life of the process.
        """
        stale_txns = [
            txn_id
            for txn_id, pending in self.pending_authorizations.items()
            if self._pending_expired(pending, now)
        ]
        for txn_id in stale_txns:
            del self.pending_authorizations[txn_id]
        stale_codes = [
            code for code, auth in self.auth_codes.items() if auth.expires_at < now
        ]
        for code in stale_codes:
            del self.auth_codes[code]
        if stale_txns or stale_codes:
            logger.debug(
                "Evicted %d expired consent transactions and %d expired "
                "authorization codes",
                len(stale_txns),
                len(stale_codes),
            )

    def _reject_expired_pending(self, txn_id: str) -> Response:
        """Drop the expired transaction and render the expired-request page."""
        del self.pending_authorizations[txn_id]
        return HTMLResponse(
            create_error_html(
                "expired_request",
                "Authorization request has expired. Please start over.",
            ),
            status_code=400,
        )

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

        if self._pending_expired(pending, time.time()):
            return self._reject_expired_pending(txn_id)

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
        form = await request.form()

        txn_id = form.get("txn_id")
        ha_token = form.get("ha_token")
        # The txn_id stays out of the log: it is what binds a submitted LLAT
        # to the code the client receives.
        logger.debug(
            "Consent form submitted (has_txn=%s, has_token=%s)",
            txn_id is not None,
            ha_token is not None,
        )

        if not txn_id:
            return HTMLResponse(
                create_error_html(
                    "invalid_request",
                    "Missing transaction ID.",
                ),
                status_code=400,
            )
        txn_id = str(txn_id)

        pending = self.pending_authorizations.get(txn_id)
        if not pending:
            return HTMLResponse(
                create_error_html(
                    "invalid_request",
                    "Authorization request expired or not found.",
                ),
                status_code=400,
            )

        if self._pending_expired(pending, time.time()):
            return self._reject_expired_pending(txn_id)

        # A multipart upload in the token field is not a token.
        if not isinstance(ha_token, str) or not ha_token.strip():
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

        # The token is not validated here; the first API call to the
        # configured HA instance does that. It is bound to this code alone.
        client_id = pending["client_id"]
        auth_code_value = f"ha_auth_code_{secrets.token_hex(16)}"
        expires_at = time.time() + AUTH_CODE_EXPIRY_SECONDS

        scopes_list = pending.get("scopes", [])
        if isinstance(scopes_list, str):
            scopes_list = scopes_list.split()

        auth_code = HomeAssistantAuthorizationCode(
            code=auth_code_value,
            client_id=client_id,
            redirect_uri=AnyHttpUrl(pending["redirect_uri"]),
            redirect_uri_provided_explicitly=pending.get(
                "redirect_uri_provided_explicitly", True
            ),
            scopes=scopes_list,
            expires_at=expires_at,
            code_challenge=pending.get("code_challenge"),  # type: ignore[arg-type]  # None is valid per PKCE spec (RFC 7636 §4.3); empty string would break validation
            ha_token=ha_token.strip(),
        )
        self.auth_codes[auth_code_value] = auth_code

        # Clean up pending authorization
        del self.pending_authorizations[txn_id]

        # Redirect back to client with auth code. RFC 9207: the authorization
        # response carries the issuer that produced it, so a client registered
        # with several authorization servers cannot be fed a code minted by a
        # different one. _ensure_single_iss (rather than an ``iss=`` kwarg
        # here) collapses any ``iss`` baked into the registered redirect URI —
        # construct_redirect_uri appends blindly, and a double ``iss`` is
        # ambiguous to strict clients.
        redirect_uri = self._ensure_single_iss(
            construct_redirect_uri(
                pending["redirect_uri"],
                code=auth_code_value,
                state=pending.get("state"),
            )
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
        # Consume the auth code. The parameter is typed as the SDK's
        # AuthorizationCode; the stored copy is the one carrying the LLAT.
        stored = self.auth_codes.pop(authorization_code.code, None)
        if stored is None:
            raise TokenError(
                "invalid_grant", "Authorization code not found or already used."
            )

        if client.client_id is None:
            raise TokenError("invalid_client", "Client ID is required")

        scopes = stored.scopes
        access_token_expires_at = int(time.time() + ACCESS_TOKEN_EXPIRY_SECONDS)
        refresh_token_expires_at = int(time.time() + REFRESH_TOKEN_EXPIRY_SECONDS)

        # Both tokens are stateless — no server-side storage needed.
        access_token_value = self._encode_token(
            stored.ha_token,
            token_type="access",
            expires_at=access_token_expires_at,
        )
        refresh_token_value = self._encode_token(
            stored.ha_token,
            token_type="refresh",
            client_id=client.client_id,
            scopes=scopes,
            expires_at=refresh_token_expires_at,
        )

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
