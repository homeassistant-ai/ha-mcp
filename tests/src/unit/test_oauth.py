"""Unit tests for OAuth 2.1 authentication."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import time

from ha_mcp.auth.provider import (
    HomeAssistantOAuthProvider,
    HomeAssistantCredentials,
    ACCESS_TOKEN_EXPIRY_SECONDS,
)
from ha_mcp.auth.consent_form import create_consent_html, create_error_html


class TestHomeAssistantCredentials:
    """Tests for HomeAssistantCredentials class."""

    def test_credentials_creation(self):
        """Test creating credentials stores values correctly."""
        creds = HomeAssistantCredentials(
            ha_url="http://homeassistant.local:8123/",
            ha_token="test_token_123",
        )

        # URL should have trailing slash stripped
        assert creds.ha_url == "http://homeassistant.local:8123"
        assert creds.ha_token == "test_token_123"
        assert creds.validated_at > 0

    def test_credentials_to_dict(self):
        """Test converting credentials to dictionary."""
        creds = HomeAssistantCredentials(
            ha_url="http://ha.local:8123",
            ha_token="token",
        )

        result = creds.to_dict()

        assert result["ha_url"] == "http://ha.local:8123"
        assert result["ha_token"] == "token"
        assert "validated_at" in result


class TestConsentForm:
    """Tests for consent form HTML generation."""

    def test_create_consent_html_basic(self):
        """Test basic consent HTML generation."""
        html = create_consent_html(
            client_id="test-client",
            client_name="Claude AI",
            redirect_uri="http://localhost:8080/callback",
            state="test-state",
            scopes=["homeassistant", "mcp"],
        )

        # Verify essential elements are present
        assert "<form" in html
        assert "Claude AI" in html
        assert "test-client" in html
        assert "homeassistant, mcp" in html
        assert 'name="ha_url"' in html
        assert 'name="ha_token"' in html
        assert "Authorize" in html

    def test_create_consent_html_with_error(self):
        """Test consent HTML includes error message when provided."""
        html = create_consent_html(
            client_id="test-client",
            client_name=None,
            redirect_uri="http://localhost/cb",
            state="state",
            scopes=[],
            error_message="Invalid credentials",
        )

        assert "Invalid credentials" in html
        assert "error-message" in html

    def test_create_consent_html_without_client_name(self):
        """Test consent HTML uses client_id when no name provided."""
        html = create_consent_html(
            client_id="my-client-id",
            client_name=None,
            redirect_uri="http://localhost/cb",
            state="state",
            scopes=["homeassistant"],
        )

        assert "my-client-id" in html

    def test_create_error_html(self):
        """Test error HTML generation."""
        html = create_error_html(
            error="invalid_request",
            error_description="The request was malformed",
        )

        assert "invalid_request" in html
        assert "The request was malformed" in html
        assert "Authentication Error" in html


class TestHomeAssistantOAuthProvider:
    """Tests for HomeAssistantOAuthProvider."""

    @pytest.fixture
    def provider(self, tmp_path, monkeypatch):
        """Create a provider instance for testing."""
        # Use temporary directory for key file in tests
        monkeypatch.setenv("HOME", str(tmp_path))
        return HomeAssistantOAuthProvider(
            base_url="http://localhost:8086",
        )

    def test_provider_initialization(self, provider):
        """Test provider initializes with correct defaults."""
        assert str(provider.base_url) == "http://localhost:8086/"
        assert provider.client_registration_options is not None
        assert provider.client_registration_options.enabled is True
        assert provider.revocation_options is not None
        assert provider.revocation_options.enabled is True

    @pytest.mark.asyncio
    async def test_register_client(self, provider):
        """Test client registration."""
        from mcp.shared.auth import OAuthClientInformationFull

        client_info = OAuthClientInformationFull(
            client_id="test-client-123",
            client_name="Test Client",
            redirect_uris=["http://localhost:8080/callback"],
            scope="homeassistant mcp",
        )

        await provider.register_client(client_info)

        # Verify client was stored
        stored = await provider.get_client("test-client-123")
        assert stored is not None
        assert stored.client_name == "Test Client"

    @pytest.mark.asyncio
    async def test_register_client_validates_scopes(self, provider):
        """Test client registration validates scopes against valid_scopes."""
        from mcp.shared.auth import OAuthClientInformationFull

        client_info = OAuthClientInformationFull(
            client_id="test-client",
            redirect_uris=["http://localhost/cb"],
            scope="invalid_scope homeassistant",
        )

        with pytest.raises(ValueError, match="not valid"):
            await provider.register_client(client_info)

    @pytest.mark.asyncio
    async def test_register_client_without_scopes_gets_defaults(self, provider):
        """Test client registration without scopes gets all valid scopes (ChatGPT compat)."""
        from mcp.shared.auth import OAuthClientInformationFull

        # ChatGPT registers without specifying scopes
        client_info = OAuthClientInformationFull(
            client_id="chatgpt-client",
            redirect_uris=["https://chatgpt.com/callback"],
            scope=None,  # No scopes specified
        )

        await provider.register_client(client_info)

        # Should have been granted all valid scopes
        stored = await provider.get_client("chatgpt-client")
        assert stored is not None
        assert stored.scope == "homeassistant mcp"

    @pytest.mark.asyncio
    async def test_get_client_not_found(self, provider):
        """Test getting non-existent client returns None."""
        result = await provider.get_client("non-existent")
        assert result is None

    @pytest.mark.asyncio
    async def test_authorize_redirects_to_consent(self, provider):
        """Test authorize redirects to consent form."""
        from mcp.shared.auth import OAuthClientInformationFull
        from mcp.server.auth.provider import AuthorizationParams
        from pydantic import AnyHttpUrl

        # Register client first
        client_info = OAuthClientInformationFull(
            client_id="test-client",
            client_name="Test",
            redirect_uris=["http://localhost/cb"],
            scope="homeassistant",
        )
        await provider.register_client(client_info)

        params = AuthorizationParams(
            redirect_uri=AnyHttpUrl("http://localhost/cb"),
            redirect_uri_provided_explicitly=True,
            state="test-state",
            scopes=["homeassistant"],
            code_challenge="challenge123",
        )

        redirect_url = await provider.authorize(client_info, params)

        # Should redirect to consent form
        assert "/consent" in redirect_url
        assert "txn_id=" in redirect_url

        # Should have stored pending authorization
        assert len(provider.pending_authorizations) == 1

    @pytest.mark.asyncio
    async def test_authorize_unregistered_client_fails(self, provider):
        """Test authorizing unregistered client raises error."""
        from mcp.shared.auth import OAuthClientInformationFull
        from mcp.server.auth.provider import AuthorizationParams, AuthorizeError
        from pydantic import AnyHttpUrl

        client_info = OAuthClientInformationFull(
            client_id="unregistered-client",
            redirect_uris=["http://localhost/cb"],
        )

        params = AuthorizationParams(
            redirect_uri=AnyHttpUrl("http://localhost/cb"),
            redirect_uri_provided_explicitly=True,
            state="state",
            scopes=[],
            code_challenge="test_challenge_value",
        )

        with pytest.raises(AuthorizeError) as exc:
            await provider.authorize(client_info, params)

        assert "not registered" in str(exc.value.error_description)

    @pytest.mark.asyncio
    async def test_validate_ha_credentials_success(self, provider):
        """Test successful HA credentials validation."""
        with patch("httpx.AsyncClient") as mock_client:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "location_name": "Home",
                "version": "2024.1.0",
            }

            mock_client_instance = AsyncMock()
            mock_client_instance.get.return_value = mock_response
            mock_client_instance.__aenter__.return_value = mock_client_instance
            mock_client_instance.__aexit__.return_value = None
            mock_client.return_value = mock_client_instance

            error = await provider._validate_ha_credentials(
                "http://ha.local:8123", "valid_token"
            )

            assert error is None

    @pytest.mark.asyncio
    async def test_validate_ha_credentials_unauthorized(self, provider):
        """Test HA credentials validation with invalid token."""
        with patch("httpx.AsyncClient") as mock_client:
            mock_response = MagicMock()
            mock_response.status_code = 401

            mock_client_instance = AsyncMock()
            mock_client_instance.get.return_value = mock_response
            mock_client_instance.__aenter__.return_value = mock_client_instance
            mock_client_instance.__aexit__.return_value = None
            mock_client.return_value = mock_client_instance

            error = await provider._validate_ha_credentials(
                "http://ha.local:8123", "invalid_token"
            )

            assert error is not None
            assert "Invalid access token" in error

    @pytest.mark.asyncio
    async def test_validate_ha_credentials_connection_error(self, provider):
        """Test HA credentials validation with connection error."""
        import httpx

        with patch("httpx.AsyncClient") as mock_client:
            mock_client_instance = AsyncMock()
            mock_client_instance.get.side_effect = httpx.ConnectError(
                "Connection failed"
            )
            mock_client_instance.__aenter__.return_value = mock_client_instance
            mock_client_instance.__aexit__.return_value = None
            mock_client.return_value = mock_client_instance

            error = await provider._validate_ha_credentials(
                "http://ha.local:8123", "token"
            )

            assert error is not None
            assert "Could not connect" in error

    @pytest.mark.asyncio
    async def test_exchange_authorization_code(self, provider):
        """Test exchanging auth code for tokens with encrypted credentials."""
        from mcp.shared.auth import OAuthClientInformationFull
        from mcp.server.auth.provider import AuthorizationCode
        from pydantic import AnyHttpUrl

        # Register client
        client_info = OAuthClientInformationFull(
            client_id="test-client",
            redirect_uris=["http://localhost/cb"],
        )
        await provider.register_client(client_info)

        # Store HA credentials (simulates consent form submission)
        provider.ha_credentials["test-client"] = HomeAssistantCredentials(
            ha_url="http://homeassistant.local:8123",
            ha_token="test_token_abc123",
        )

        # Create auth code directly
        auth_code = AuthorizationCode(
            code="test_code_123",
            client_id="test-client",
            redirect_uri=AnyHttpUrl("http://localhost/cb"),
            redirect_uri_provided_explicitly=True,
            scopes=["homeassistant"],
            expires_at=time.time() + 300,
            code_challenge="test_challenge_value",
        )
        provider.auth_codes["test_code_123"] = auth_code

        # Exchange code
        token = await provider.exchange_authorization_code(client_info, auth_code)

        assert token.access_token is not None
        assert token.refresh_token is not None
        assert token.token_type == "Bearer"
        assert token.expires_in == ACCESS_TOKEN_EXPIRY_SECONDS

        # Auth code should be consumed
        assert "test_code_123" not in provider.auth_codes

        # Credentials should be cleaned up (no longer stored in memory)
        assert "test-client" not in provider.ha_credentials

    @pytest.mark.asyncio
    async def test_load_access_token(self, provider):
        """Test loading base64-encoded stateless access token."""
        # Create an encoded token
        encoded_token = provider._encode_credentials(
            "http://homeassistant.local:8123",
            "test_token_xyz"
        )

        result = await provider.load_access_token(encoded_token)

        assert result is not None
        assert result.claims["ha_url"] == "http://homeassistant.local:8123"
        assert result.claims["ha_token"] == "test_token_xyz"
        assert result.expires_at is None  # Stateless tokens don't expire

    @pytest.mark.asyncio
    async def test_load_invalid_access_token(self, provider):
        """Test loading invalid token returns None."""
        # Try to load a non-base64 token
        result = await provider.load_access_token("invalid_random_string")

        assert result is None

    @pytest.mark.asyncio
    async def test_verify_token(self, provider):
        """Test verify_token delegates to load_access_token with base64 tokens."""
        # Create an encoded token
        encoded_token = provider._encode_credentials(
            "http://ha.local:8123",
            "valid_token"
        )

        result = await provider.verify_token(encoded_token)
        assert result is not None
        assert result.claims["ha_url"] == "http://ha.local:8123"

        result_invalid = await provider.verify_token("invalid_token_string")
        assert result_invalid is None

    @pytest.mark.asyncio
    async def test_refresh_token_exchange(self, provider):
        """Test refresh token exchange."""
        from mcp.shared.auth import OAuthClientInformationFull
        from mcp.server.auth.provider import RefreshToken

        client_info = OAuthClientInformationFull(
            client_id="test-client",
            redirect_uris=["http://localhost/cb"],
        )
        await provider.register_client(client_info)

        # Create refresh token
        refresh_token = RefreshToken(
            token="refresh_123",
            client_id="test-client",
            scopes=["homeassistant", "mcp"],
            expires_at=int(time.time() + 86400),
        )
        provider.refresh_tokens["refresh_123"] = refresh_token

        # Exchange refresh token
        new_token = await provider.exchange_refresh_token(
            client_info, refresh_token, ["homeassistant"]
        )

        assert new_token.access_token is not None
        assert new_token.refresh_token is not None
        assert new_token.refresh_token != "refresh_123"

        # Old refresh token should be revoked
        assert "refresh_123" not in provider.refresh_tokens

    @pytest.mark.asyncio
    async def test_revoke_token(self, provider):
        """Test token revocation with refresh tokens."""
        from mcp.server.auth.provider import RefreshToken

        # With stateless encrypted access tokens, we don't store access tokens in memory.
        # Only refresh tokens are stored and can be revoked.
        provider.refresh_tokens["refresh_123"] = RefreshToken(
            token="refresh_123",
            client_id="client",
            scopes=[],
            expires_at=int(time.time() + 86400),
        )

        # Revoke refresh token
        await provider.revoke_token(provider.refresh_tokens["refresh_123"])

        # Refresh token should be removed
        assert "refresh_123" not in provider.refresh_tokens

    def test_get_ha_credentials(self, provider):
        """Test getting HA credentials for a client."""
        provider.ha_credentials["client-123"] = HomeAssistantCredentials(
            ha_url="http://ha.local:8123",
            ha_token="token",
        )

        result = provider.get_ha_credentials("client-123")
        assert result is not None
        assert result.ha_url == "http://ha.local:8123"

        result_none = provider.get_ha_credentials("nonexistent")
        assert result_none is None

    def test_get_ha_credentials_for_token(self, provider):
        """Test getting HA credentials via access token."""
        from mcp.server.auth.provider import AccessToken

        # Set up client credentials
        provider.ha_credentials["client-abc"] = HomeAssistantCredentials(
            ha_url="http://ha.local:8123",
            ha_token="token",
        )

        # Create access token
        provider.access_tokens["token-xyz"] = AccessToken(
            token="token-xyz",
            client_id="client-abc",
            scopes=[],
            expires_at=int(time.time() + 3600),
        )

        result = provider.get_ha_credentials_for_token("token-xyz")
        assert result is not None
        assert result.ha_url == "http://ha.local:8123"

        result_none = provider.get_ha_credentials_for_token("invalid")
        assert result_none is None

    def test_get_routes_includes_consent(self, provider):
        """Test that routes include consent endpoints."""
        routes = provider.get_routes()

        route_paths = [r.path for r in routes]
        assert "/consent" in route_paths


class TestOAuthRoutes:
    """Tests for OAuth HTTP routes."""

    @pytest.fixture
    async def provider(self):
        """Create a provider instance for testing."""
        return HomeAssistantOAuthProvider(
            base_url="http://localhost:8086",
        )

    @pytest.fixture
    def mock_request(self):
        """Create a mock request helper."""
        from unittest.mock import Mock

        def create_request(query_params=None, form_data=None):
            request = Mock()
            request.query_params = query_params or {}

            async def get_form():
                return form_data or {}

            request.form = get_form
            return request

        return create_request

    @pytest.mark.asyncio
    async def test_enhanced_metadata_handler(self, provider):
        """Test enhanced OAuth metadata endpoint exists and has correct path."""
        routes = provider.get_routes()
        metadata_route = next(
            (r for r in routes if r.path == "/.well-known/oauth-authorization-server"),
            None
        )

        # Verify the route exists
        assert metadata_route is not None
        assert metadata_route.path == "/.well-known/oauth-authorization-server"

        # Note: Full handler testing requires ASGI app context, which is tested in E2E tests

    @pytest.mark.asyncio
    async def test_openid_configuration_endpoint(self, provider):
        """Test OpenID Configuration endpoint exists for ChatGPT compatibility."""
        routes = provider.get_routes()
        openid_route = next(
            (r for r in routes if r.path == "/.well-known/openid-configuration"),
            None
        )

        # Verify the route exists (required by ChatGPT MCP connector)
        assert openid_route is not None
        assert openid_route.path == "/.well-known/openid-configuration"

        # Note: Should return same metadata as oauth-authorization-server for compatibility

    @pytest.mark.asyncio
    async def test_consent_get_success(self, provider, mock_request):
        """Test consent form GET with valid transaction."""
        from mcp.shared.auth import OAuthClientInformationFull

        # Register client and create pending authorization
        client_info = OAuthClientInformationFull(
            client_id="test-client",
            client_name="Test Client",
            redirect_uris=["http://localhost/cb"],
        )
        await provider.register_client(client_info)

        # Create pending authorization
        txn_id = "test-txn-123"
        provider.pending_authorizations[txn_id] = {
            "client_id": "test-client",
            "client_name": "Test Client",
            "redirect_uri": "http://localhost/cb",
            "state": "test-state",
            "scopes": ["homeassistant"],
            "created_at": time.time(),
        }

        # Call consent GET
        request = mock_request(query_params={"txn_id": txn_id})
        response = await provider._consent_get(request)

        assert response.status_code == 200
        assert b"Test Client" in response.body
        assert b"test-txn-123" in response.body

    @pytest.mark.asyncio
    async def test_consent_get_missing_txn_id(self, provider, mock_request):
        """Test consent form GET with missing transaction ID."""
        request = mock_request(query_params={})
        response = await provider._consent_get(request)

        assert response.status_code == 400
        assert b"Missing transaction ID" in response.body

    @pytest.mark.asyncio
    async def test_consent_get_invalid_txn_id(self, provider, mock_request):
        """Test consent form GET with invalid transaction ID."""
        request = mock_request(query_params={"txn_id": "nonexistent"})
        response = await provider._consent_get(request)

        assert response.status_code == 400
        assert b"expired or not found" in response.body

    @pytest.mark.asyncio
    async def test_consent_get_expired_txn(self, provider, mock_request):
        """Test consent form GET with expired transaction."""
        txn_id = "expired-txn"
        provider.pending_authorizations[txn_id] = {
            "client_id": "test-client",
            "redirect_uri": "http://localhost/cb",
            "created_at": time.time() - 400,  # More than 5 minutes ago
        }

        request = mock_request(query_params={"txn_id": txn_id})
        response = await provider._consent_get(request)

        assert response.status_code == 400
        assert b"expired" in response.body
        # Transaction should be removed
        assert txn_id not in provider.pending_authorizations

    @pytest.mark.asyncio
    async def test_consent_post_success(self, provider, mock_request):
        """Test consent form POST with valid credentials."""
        from mcp.shared.auth import OAuthClientInformationFull

        # Register client
        client_info = OAuthClientInformationFull(
            client_id="test-client",
            redirect_uris=["http://localhost/cb"],
        )
        await provider.register_client(client_info)

        # Create pending authorization
        txn_id = "test-txn-456"
        provider.pending_authorizations[txn_id] = {
            "client_id": "test-client",
            "redirect_uri": "http://localhost/cb",
            "state": "test-state",
            "scopes": ["homeassistant"],
            "code_challenge": "test-challenge",
            "created_at": time.time(),
        }

        # Mock HA validation
        with patch.object(provider, "_validate_ha_credentials", return_value=None):
            request = mock_request(
                form_data={
                    "txn_id": txn_id,
                    "ha_url": "http://homeassistant.local:8123",
                    "ha_token": "test_token",
                }
            )
            response = await provider._consent_post(request)

        # Should redirect with auth code
        assert response.status_code == 303
        assert "code=" in response.headers["location"]
        assert "state=test-state" in response.headers["location"]

    @pytest.mark.asyncio
    async def test_consent_post_invalid_credentials(self, provider, mock_request):
        """Test consent form POST with invalid HA credentials."""
        txn_id = "test-txn-789"
        provider.pending_authorizations[txn_id] = {
            "client_id": "test-client",
            "redirect_uri": "http://localhost/cb",
            "created_at": time.time(),
        }

        # Mock HA validation to return error
        with patch.object(
            provider,
            "_validate_ha_credentials",
            return_value="Invalid access token"
        ):
            request = mock_request(
                form_data={
                    "txn_id": txn_id,
                    "ha_url": "http://homeassistant.local:8123",
                    "ha_token": "invalid_token",
                }
            )
            response = await provider._consent_post(request)

        # Should redirect back to consent with error
        assert response.status_code == 303
        assert "error=" in response.headers["location"]


class TestEndToEndOAuthFlow:
    """End-to-end tests for complete OAuth flow."""

    @pytest.fixture
    async def provider(self):
        """Create a provider instance for testing."""
        return HomeAssistantOAuthProvider(
            base_url="http://localhost:8086",
        )

    @pytest.mark.asyncio
    async def test_complete_oauth_flow(self, provider):
        """Test complete OAuth flow from registration to token usage."""
        from mcp.shared.auth import OAuthClientInformationFull
        from mcp.server.auth.provider import AuthorizationParams
        from pydantic import AnyHttpUrl

        # Step 1: Client registration
        client_info = OAuthClientInformationFull(
            client_id="e2e-client",
            client_name="E2E Test Client",
            redirect_uris=["http://localhost:9999/callback"],
            scope="homeassistant mcp",
        )
        await provider.register_client(client_info)

        # Verify client is registered
        stored_client = await provider.get_client("e2e-client")
        assert stored_client is not None
        assert stored_client.client_name == "E2E Test Client"

        # Step 2: Authorization request
        params = AuthorizationParams(
            redirect_uri=AnyHttpUrl("http://localhost:9999/callback"),
            redirect_uri_provided_explicitly=True,
            state="e2e-state-123",
            scopes=["homeassistant", "mcp"],
            code_challenge="e2e-challenge-xyz",
        )

        redirect_url = await provider.authorize(client_info, params)
        assert "/consent" in redirect_url
        assert "txn_id=" in redirect_url

        # Extract txn_id from redirect URL
        import urllib.parse
        parsed = urllib.parse.urlparse(redirect_url)
        query = urllib.parse.parse_qs(parsed.query)
        txn_id = query["txn_id"][0]

        # Step 3: Simulate consent form submission
        pending = provider.pending_authorizations[txn_id]
        assert pending["client_id"] == "e2e-client"

        # Store HA credentials (simulates successful consent)
        provider.ha_credentials["e2e-client"] = HomeAssistantCredentials(
            ha_url="http://homeassistant.local:8123",
            ha_token="e2e_test_token",
        )

        # Create auth code (simulates consent POST creating the code)
        from mcp.server.auth.provider import AuthorizationCode
        auth_code_value = "e2e-auth-code-123"
        auth_code = AuthorizationCode(
            code=auth_code_value,
            client_id="e2e-client",
            redirect_uri=AnyHttpUrl("http://localhost:9999/callback"),
            redirect_uri_provided_explicitly=True,
            scopes=["homeassistant", "mcp"],
            expires_at=time.time() + 300,
            code_challenge="e2e-challenge-xyz",
        )
        provider.auth_codes[auth_code_value] = auth_code

        # Step 4: Exchange auth code for tokens
        token_response = await provider.exchange_authorization_code(
            client_info, auth_code
        )

        assert token_response.access_token is not None
        assert token_response.refresh_token is not None
        assert token_response.token_type == "Bearer"

        # Auth code should be consumed
        assert auth_code_value not in provider.auth_codes

        # Step 5: Verify access token contains encrypted credentials
        access_token_obj = await provider.load_access_token(
            token_response.access_token
        )
        assert access_token_obj is not None
        assert access_token_obj.claims["ha_url"] == "http://homeassistant.local:8123"
        assert access_token_obj.claims["ha_token"] == "e2e_test_token"

        # Step 6: Use refresh token to get new access token
        refresh_token_obj = provider.refresh_tokens[token_response.refresh_token]
        new_token_response = await provider.exchange_refresh_token(
            client_info, refresh_token_obj, ["homeassistant"]
        )

        assert new_token_response.access_token is not None
        assert new_token_response.refresh_token is not None
        # Refresh token should be rotated
        assert new_token_response.refresh_token != token_response.refresh_token

        # Old refresh token should be revoked
        assert token_response.refresh_token not in provider.refresh_tokens


class TestOAuthProxyClient:
    """Tests for OAuthProxyClient in __main__.py."""

    @pytest.fixture
    def mock_auth_provider(self):
        """Create a mock auth provider."""
        provider = MagicMock()
        provider._cipher = None
        return provider

    @pytest.fixture
    def mock_access_token(self):
        """Create a mock access token with claims."""
        from fastmcp.server.auth.auth import AccessToken

        return AccessToken(
            token="encrypted-token-123",
            client_id="test-client",
            scopes=["homeassistant"],
            expires_at=None,
            claims={
                "ha_url": "http://homeassistant.local:8123",
                "ha_token": "test_ha_token_xyz",
            },
        )

    def test_oauth_proxy_client_initialization(self, mock_auth_provider):
        """Test OAuthProxyClient initialization."""
        from ha_mcp.__main__ import OAuthProxyClient

        proxy = OAuthProxyClient(mock_auth_provider)
        assert proxy._auth_provider == mock_auth_provider
        assert proxy._oauth_clients == {}

    def test_oauth_proxy_client_attribute_forwarding(self, mock_auth_provider, mock_access_token):
        """Test that OAuthProxyClient forwards attributes to HA client."""
        from ha_mcp.__main__ import OAuthProxyClient

        proxy = OAuthProxyClient(mock_auth_provider)

        # Mock get_access_token to return our mock token
        with patch("fastmcp.server.dependencies.get_access_token", return_value=mock_access_token):
            # Access an attribute (this should create a client)
            with patch("ha_mcp.client.rest_client.HomeAssistantClient") as mock_ha_client:
                mock_client_instance = MagicMock()
                mock_ha_client.return_value = mock_client_instance

                # Access a method - this triggers __getattr__ which creates the client
                _ = proxy.get_state

                # Verify HomeAssistantClient was created with correct params
                mock_ha_client.assert_called_once_with(
                    base_url="http://homeassistant.local:8123",
                    token="test_ha_token_xyz",
                )

                # Verify the client instance was stored
                assert len(proxy._oauth_clients) == 1

    def test_oauth_proxy_client_reuses_clients(self, mock_auth_provider, mock_access_token):
        """Test that OAuthProxyClient reuses client instances for same credentials."""
        from ha_mcp.__main__ import OAuthProxyClient

        proxy = OAuthProxyClient(mock_auth_provider)

        with patch("fastmcp.server.dependencies.get_access_token", return_value=mock_access_token):
            with patch("ha_mcp.client.rest_client.HomeAssistantClient") as mock_ha_client:
                mock_client_instance = MagicMock()
                mock_ha_client.return_value = mock_client_instance

                # Access attribute twice
                _ = proxy.get_state
                _ = proxy.call_service

                # Client should only be created once
                assert mock_ha_client.call_count == 1

    def test_oauth_proxy_client_no_token_raises_error(self, mock_auth_provider):
        """Test that OAuthProxyClient raises error when no token in context."""
        from ha_mcp.__main__ import OAuthProxyClient

        proxy = OAuthProxyClient(mock_auth_provider)

        # Mock get_access_token to return None
        with patch("fastmcp.server.dependencies.get_access_token", return_value=None):
            with pytest.raises(RuntimeError, match="No OAuth token"):
                _ = proxy.get_state

    def test_oauth_proxy_client_missing_claims_raises_error(self, mock_auth_provider):
        """Test that OAuthProxyClient raises error when token has no claims."""
        from ha_mcp.__main__ import OAuthProxyClient
        from fastmcp.server.auth.auth import AccessToken

        # Token without claims
        token_no_claims = AccessToken(
            token="token-123",
            client_id="test",
            scopes=[],
            expires_at=None,
            claims={},  # Empty claims
        )

        proxy = OAuthProxyClient(mock_auth_provider)

        with patch("fastmcp.server.dependencies.get_access_token", return_value=token_no_claims):
            with pytest.raises(RuntimeError, match="No Home Assistant credentials"):
                _ = proxy.get_state
