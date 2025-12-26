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
    def provider(self):
        """Create a provider instance for testing."""
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
        """Test loading encrypted stateless access token."""
        # Create an encrypted token
        encrypted_token = provider._encrypt_credentials(
            "http://homeassistant.local:8123",
            "test_token_xyz"
        )

        result = await provider.load_access_token(encrypted_token)

        assert result is not None
        assert result.claims["ha_url"] == "http://homeassistant.local:8123"
        assert result.claims["ha_token"] == "test_token_xyz"
        assert result.expires_at is None  # Stateless tokens don't expire

    @pytest.mark.asyncio
    async def test_load_invalid_access_token(self, provider):
        """Test loading invalid token returns None."""
        # Try to load a non-encrypted token
        result = await provider.load_access_token("invalid_random_string")

        assert result is None

    @pytest.mark.asyncio
    async def test_verify_token(self, provider):
        """Test verify_token delegates to load_access_token with encrypted tokens."""
        # Create an encrypted token
        encrypted_token = provider._encrypt_credentials(
            "http://ha.local:8123",
            "valid_token"
        )

        result = await provider.verify_token(encrypted_token)
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
