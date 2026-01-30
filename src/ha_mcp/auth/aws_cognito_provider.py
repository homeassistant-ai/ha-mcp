"""AWS Cognito auth helpers for ha-mcp deployments.

This module provides a thin wrapper around FastMCP's AWSCognitoProvider with
OAuth metadata and optional auto-registration behavior tuned for MCP clients
like Claude connectors.
"""

from __future__ import annotations

import logging

from fastmcp.server.auth.providers.aws import AWSCognitoProvider
from fastmcp.server.auth.redirect_validation import validate_redirect_uri
from fastmcp.server.auth.handlers.authorize import AuthorizationHandler
from mcp.server.auth.handlers.metadata import MetadataHandler
from mcp.server.auth.routes import build_metadata, cors_middleware
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

logger = logging.getLogger(__name__)

# Default allowed OAuth client redirect URIs for Claude MCP connectors.
DEFAULT_ALLOWED_CLIENT_REDIRECT_URIS: list[str] = [
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    "http://localhost:*",
    "http://127.0.0.1:*",
]


class ClaudeCompatibleCognitoProvider(AWSCognitoProvider):
    """AWS Cognito provider with Claude-friendly OAuth metadata.

    Adds:
    - `response_modes_supported=["query"]`
    - `token_endpoint_auth_methods_supported` includes `"none"` for public PKCE clients
    - Optional auto-registration on `/authorize` (for clients that skip DCR)
    """

    def get_routes(self, mcp_path: str | None = None) -> list[Route]:
        routes = super().get_routes(mcp_path)
        patched: list[Route] = []

        assert self.base_url is not None  # required by provider

        client_registration_options = self.client_registration_options or (
            ClientRegistrationOptions()
        )
        revocation_options = self.revocation_options or RevocationOptions()

        metadata = build_metadata(
            self.base_url,
            self.service_documentation_url,
            client_registration_options,
            revocation_options,
        )

        metadata.response_modes_supported = ["query"]
        metadata.token_endpoint_auth_methods_supported = [
            "none",
            "client_secret_post",
            "client_secret_basic",
        ]
        if metadata.revocation_endpoint:
            metadata.revocation_endpoint_auth_methods_supported = [
                "none",
                "client_secret_post",
                "client_secret_basic",
            ]

        authorize_handler = AuthorizationHandler(provider=self, base_url=self.base_url)

        async def _authorize_with_autoreg(request: Request) -> Response:
            client_id: str | None = None
            redirect_uri_raw: str | None = None

            if request.method == "GET":
                client_id = request.query_params.get("client_id")
                redirect_uri_raw = request.query_params.get("redirect_uri")
            else:
                form = await request.form()
                client_id_value = form.get("client_id")
                if isinstance(client_id_value, str):
                    client_id = client_id_value
                redirect_uri_value = form.get("redirect_uri")
                if isinstance(redirect_uri_value, str):
                    redirect_uri_raw = redirect_uri_value

            if client_id and redirect_uri_raw:
                existing = await self.get_client(client_id)
                if existing is None:
                    allowed_patterns = getattr(
                        self, "_allowed_client_redirect_uris", None
                    )
                    if validate_redirect_uri(redirect_uri_raw, allowed_patterns):
                        try:
                            client_info = OAuthClientInformationFull(
                                client_id=client_id,
                                redirect_uris=[AnyUrl(redirect_uri_raw)],
                                token_endpoint_auth_method="none",
                                grant_types=["authorization_code", "refresh_token"],
                                response_types=["code"],
                                scope=" ".join(self.required_scopes or []),
                            )
                            await self.register_client(client_info)
                        except Exception:
                            # Fall through to handler; it will return a helpful error page
                            logger.debug(
                                "Auto-registration failed for client_id=%s",
                                client_id,
                                exc_info=True,
                            )

            return await authorize_handler.handle(request)

        for route in routes:
            if (
                isinstance(route, Route)
                and route.path == "/.well-known/oauth-authorization-server"
                and route.methods is not None
                and ("GET" in route.methods or "OPTIONS" in route.methods)
            ):
                patched.append(
                    Route(
                        "/.well-known/oauth-authorization-server",
                        endpoint=cors_middleware(
                            MetadataHandler(metadata).handle, ["GET", "OPTIONS"]
                        ),
                        methods=["GET", "OPTIONS"],
                    )
                )
                continue

            if (
                isinstance(route, Route)
                and route.path == "/authorize"
                and route.methods is not None
                and ("GET" in route.methods or "POST" in route.methods)
            ):
                patched.append(
                    Route(
                        "/authorize",
                        endpoint=_authorize_with_autoreg,
                        methods=["GET", "POST"],
                    )
                )
                continue

            patched.append(route)

        return patched
