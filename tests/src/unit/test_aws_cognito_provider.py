"""Unit tests for AWS Cognito auth wrapper."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from ha_mcp.auth.aws_cognito_provider import ClaudeCompatibleCognitoProvider


def _mock_oidc_config_response():
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "issuer": "https://example.com",
        "authorization_endpoint": "https://example.com/authorize",
        "token_endpoint": "https://example.com/token",
        "jwks_uri": "https://example.com/jwks.json",
        "response_types_supported": ["code"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
    }
    return response


@pytest.fixture
def provider():
    with patch(
        "fastmcp.server.auth.oidc_proxy.httpx.get",
        return_value=_mock_oidc_config_response(),
    ):
        return ClaudeCompatibleCognitoProvider(
            user_pool_id="us-east-1_ABC123",
            aws_region="us-east-1",
            client_id="client-id",
            client_secret="client-secret",
            base_url="http://localhost:8086",
            issuer_url="http://localhost:8086",
            allowed_client_redirect_uris=["https://claude.ai/api/mcp/auth_callback"],
            required_scopes=["openid"],
        )


def test_metadata_includes_claude_compat_fields(provider):
    routes = provider.get_routes(mcp_path="/mcp")
    app = Starlette(routes=routes)
    client = TestClient(app)

    response = client.get("/.well-known/oauth-authorization-server")
    assert response.status_code == 200
    data = response.json()

    assert data["response_modes_supported"] == ["query"]
    assert "none" in data["token_endpoint_auth_methods_supported"]


def test_authorize_autoregisters_unknown_clients(provider):
    # Patch AuthorizationHandler used inside get_routes so we don't depend on upstream logic.
    with patch("ha_mcp.auth.aws_cognito_provider.AuthorizationHandler") as handler_cls:
        handler = handler_cls.return_value

        async def _handle(_request):
            return JSONResponse({"ok": True})

        handler.handle = AsyncMock(side_effect=_handle)

        # Force auto-registration path.
        provider.get_client = AsyncMock(return_value=None)
        provider.register_client = AsyncMock()

        routes = provider.get_routes(mcp_path="/mcp")
        app = Starlette(routes=routes)
        client = TestClient(app)

        response = client.get(
            "/authorize",
            params={
                "client_id": "test-client",
                "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            },
        )
        assert response.status_code == 200
        assert response.json() == {"ok": True}

        provider.register_client.assert_awaited()
