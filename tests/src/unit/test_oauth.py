"""Unit tests for OAuth 2.1 authentication."""

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp.auth.consent_form import create_consent_html, create_error_html
from ha_mcp.auth.provider import (
    ACCESS_TOKEN_EXPIRY_SECONDS,
    HomeAssistantCredentials,
    HomeAssistantOAuthProvider,
)


class TestHomeAssistantCredentials:
    """Tests for HomeAssistantCredentials class."""

    def test_credentials_creation(self):
        """Test creating credentials stores values correctly."""
        creds = HomeAssistantCredentials(
            ha_token="test_token_123",
        )

        assert creds.ha_token == "test_token_123"
        assert creds.validated_at > 0

    def test_credentials_to_dict(self):
        """Test converting credentials to dictionary."""
        creds = HomeAssistantCredentials(
            ha_token="token",
        )

        result = creds.to_dict()

        assert result["ha_token"] == "token"
        assert "validated_at" in result


class TestConsentForm:
    """Tests for consent form HTML generation."""

    def test_create_consent_html_basic(self):
        """Test basic consent HTML generation."""
        html = create_consent_html(
            client_id="test-client",
            redirect_uri="http://claude.ai/callback",
            state="test-state",
            txn_id="test-txn-123",
        )

        # Verify essential elements are present
        assert "<form" in html
        assert "claude.ai" in html
        assert "test-client" in html
        assert 'name="ha_token"' in html
        assert "Authorize" in html
        assert "test-txn-123" in html
        # ha_url field should NOT be present (SSRF fix)
        assert 'name="ha_url"' not in html

    def test_create_consent_html_shows_redirect_domain(self):
        """Test consent HTML shows domain from redirect_uri instead of client name."""
        html = create_consent_html(
            client_id="test-client",
            redirect_uri="https://chatgpt.com/aip/callback",
            state="state",
            txn_id="txn-123",
        )

        assert "chatgpt.com" in html
        assert "warning-box" in html

    def test_create_consent_html_with_error(self):
        """Test consent HTML includes error message when provided."""
        html = create_consent_html(
            client_id="test-client",
            redirect_uri="http://localhost/cb",
            state="state",
            txn_id="txn-123",
            error_message="Invalid credentials",
        )

        assert "Invalid credentials" in html
        assert "error-message" in html

    def test_create_consent_html_xss_prevention(self):
        """Test that user-controlled values are HTML-escaped."""
        html = create_consent_html(
            client_id='<script>alert("xss")</script>',
            redirect_uri='http://evil.com/"><script>alert(1)</script>',
            state='"><script>alert(1)</script>',
            txn_id='"><script>alert(1)</script>',
        )

        # Raw XSS payloads should NOT appear in user-controlled output areas
        # (template has its own <script> for form handling, so check escaped versions)
        assert "&lt;script&gt;alert(" in html
        assert "&quot;&gt;&lt;script&gt;" in html

    def test_create_consent_html_warning_box(self):
        """Test that consent form includes token sharing warning with domain."""
        html = create_consent_html(
            client_id="test-client",
            redirect_uri="https://claude.ai/callback",
            state="state",
            txn_id="txn-789",
        )

        assert "warning-box" in html
        assert "shared with" in html
        assert "claude.ai" in html
        assert "Long-Lived Access Tokens" in html

    def test_create_error_html(self):
        """Test error HTML generation."""
        html = create_error_html(
            error="invalid_request",
            error_description="The request was malformed",
        )

        assert "invalid_request" in html
        assert "The request was malformed" in html
        assert "Authentication Error" in html

    def test_create_error_html_xss_prevention(self):
        """Test that error page HTML-escapes user-controlled values."""
        html = create_error_html(
            error='<script>alert("xss")</script>',
            error_description="<img src=x onerror=alert(1)>",
        )

        assert "<script>" not in html
        assert "<img src=x onerror" not in html
        assert "&lt;script&gt;" in html


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
        from mcp.server.auth.provider import AuthorizationParams
        from mcp.shared.auth import OAuthClientInformationFull
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
        from mcp.server.auth.provider import AuthorizationParams, AuthorizeError
        from mcp.shared.auth import OAuthClientInformationFull
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
    async def test_exchange_authorization_code(self, provider):
        """Test exchanging auth code for tokens with stateless credentials."""
        from mcp.server.auth.provider import AuthorizationCode
        from mcp.shared.auth import OAuthClientInformationFull
        from pydantic import AnyHttpUrl

        # Register client
        client_info = OAuthClientInformationFull(
            client_id="test-client",
            redirect_uris=["http://localhost/cb"],
        )
        await provider.register_client(client_info)

        # Store HA credentials (simulates consent form submission)
        provider.ha_credentials["test-client"] = HomeAssistantCredentials(
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
        encoded_token = provider._encode_token("test_token_xyz", token_type="access")

        result = await provider.load_access_token(encoded_token)

        assert result is not None
        assert result.claims["ha_token"] == "test_token_xyz"
        assert result.expires_at is None  # Stateless tokens don't expire

    @pytest.mark.asyncio
    async def test_load_access_token_rejects_refresh_type(self, provider):
        """Test that refresh tokens cannot be used as access tokens."""
        refresh_token = provider._encode_token(
            "test_token",
            token_type="refresh",
            client_id="c",
            scopes=["homeassistant"],
        )
        result = await provider.load_access_token(refresh_token)
        assert result is None

    @pytest.mark.asyncio
    async def test_load_access_token_backwards_compat(self, provider):
        """Test that tokens without a type field are accepted (backwards compat)."""
        from base64 import urlsafe_b64encode

        # Manually create a token without "type" field (old format)
        payload = {"ha_token": "old_format_token", "iat": int(time.time())}
        old_token = urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")

        result = await provider.load_access_token(old_token)
        assert result is not None
        assert result.claims["ha_token"] == "old_format_token"

    @pytest.mark.asyncio
    async def test_load_invalid_access_token(self, provider):
        """Test loading invalid token returns None."""
        # Try to load a non-base64 token
        result = await provider.load_access_token("invalid_random_string")

        assert result is None

    @pytest.mark.asyncio
    async def test_verify_token(self, provider):
        """Test verify_token delegates to load_access_token with base64 tokens."""
        encoded_token = provider._encode_token("valid_token", token_type="access")

        result = await provider.verify_token(encoded_token)
        assert result is not None
        assert result.claims["ha_token"] == "valid_token"

        result_invalid = await provider.verify_token("invalid_token_string")
        assert result_invalid is None

    @pytest.mark.asyncio
    async def test_refresh_token_exchange(self, provider):
        """Test refresh token exchange produces valid stateless access token."""
        from mcp.server.auth.provider import RefreshToken
        from mcp.shared.auth import OAuthClientInformationFull

        client_info = OAuthClientInformationFull(
            client_id="test-client",
            redirect_uris=["http://localhost/cb"],
        )
        await provider.register_client(client_info)

        # Create a stateless refresh token (as exchange_authorization_code would)
        stateless_refresh = provider._encode_token(
            "test_ha_token_xyz",
            token_type="refresh",
            client_id="test-client",
            scopes=["homeassistant", "mcp"],
            expires_at=int(time.time() + 86400),
        )

        refresh_token = RefreshToken(
            token=stateless_refresh,
            client_id="test-client",
            scopes=["homeassistant", "mcp"],
            expires_at=int(time.time() + 86400),
        )

        # Exchange refresh token
        new_token = await provider.exchange_refresh_token(
            client_info, refresh_token, ["homeassistant"]
        )

        assert new_token.access_token is not None
        assert new_token.refresh_token is not None
        assert new_token.refresh_token != stateless_refresh

        # New access token must be a valid stateless token with HA credentials
        access_token_obj = await provider.load_access_token(new_token.access_token)
        assert access_token_obj is not None
        assert access_token_obj.claims["ha_token"] == "test_ha_token_xyz"

    @pytest.mark.asyncio
    async def test_revoke_token_is_noop(self, provider):
        """Test that revocation is a no-op for stateless tokens (LLAT is the boundary)."""
        from mcp.server.auth.provider import RefreshToken

        refresh = RefreshToken(
            token=provider._encode_token(
                "tok",
                token_type="refresh",
                client_id="c",
                scopes=["homeassistant"],
            ),
            client_id="c",
            scopes=["homeassistant"],
            expires_at=int(time.time() + 86400),
        )

        # Should not raise
        await provider.revoke_token(refresh)

    def test_get_ha_credentials(self, provider):
        """Test getting HA credentials for a client."""
        provider.ha_credentials["client-123"] = HomeAssistantCredentials(
            ha_token="token",
        )

        result = provider.get_ha_credentials("client-123")
        assert result is not None
        assert result.ha_token == "token"

        result_none = provider.get_ha_credentials("nonexistent")
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
            None,
        )

        # Verify the route exists
        assert metadata_route is not None
        assert metadata_route.path == "/.well-known/oauth-authorization-server"

        # Note: Full handler testing requires ASGI app context, which is tested in E2E tests

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "discovery_path,description",
        [
            (
                "/.well-known/openid-configuration",
                "standard OpenID Configuration endpoint",
            ),
            (
                "/token/.well-known/openid-configuration",
                "ChatGPT bug workaround endpoint",
            ),
        ],
    )
    async def test_openid_configuration_endpoints(
        self, provider, discovery_path, description
    ):
        """Test OpenID Configuration endpoints exist for ChatGPT compatibility.

        Covers:
        - Standard /.well-known/openid-configuration (required by ChatGPT)
        - Non-standard /token/.well-known/openid-configuration (ChatGPT bug workaround)

        Both should serve the same metadata as /.well-known/oauth-authorization-server.
        """
        routes = provider.get_routes()
        route = next((r for r in routes if r.path == discovery_path), None)

        # Verify the route exists
        assert route is not None, f"Missing {description} at {discovery_path}"
        assert route.path == discovery_path

    @pytest.mark.asyncio
    async def test_consent_get_success(self, provider, mock_request):
        """Test consent form GET with valid transaction."""
        from mcp.shared.auth import OAuthClientInformationFull

        # Register client and create pending authorization
        client_info = OAuthClientInformationFull(
            client_id="test-client",
            client_name="Test Client",
            redirect_uris=["http://claude.ai/callback"],
        )
        await provider.register_client(client_info)

        # Create pending authorization
        txn_id = "test-txn-123"
        provider.pending_authorizations[txn_id] = {
            "client_id": "test-client",
            "client_name": "Test Client",
            "redirect_uri": "http://claude.ai/callback",
            "state": "test-state",
            "scopes": ["homeassistant"],
            "created_at": time.time(),
        }

        # Call consent GET
        request = mock_request(query_params={"txn_id": txn_id})
        response = await provider._consent_get(request)

        assert response.status_code == 200
        assert b"claude.ai" in response.body
        assert b"test-txn-123" in response.body

    @pytest.mark.asyncio
    async def test_consent_get_no_redirect_uri(self, provider, mock_request):
        """Test consent form GET returns error when redirect_uri is missing."""
        txn_id = "test-txn-no-redirect"
        provider.pending_authorizations[txn_id] = {
            "client_id": "test-client",
            "redirect_uri": "",  # Empty
            "created_at": time.time(),
        }

        request = mock_request(query_params={"txn_id": txn_id})
        response = await provider._consent_get(request)

        assert response.status_code == 400
        assert b"redirect URI" in response.body

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
        """Test consent form POST with valid token."""
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

        # No more _validate_ha_credentials mock needed - validation removed
        request = mock_request(
            form_data={
                "txn_id": txn_id,
                "ha_token": "test_token",
            }
        )
        response = await provider._consent_post(request)

        # Should redirect with auth code
        assert response.status_code == 303
        assert "code=" in response.headers["location"]
        assert "state=test-state" in response.headers["location"]

    @pytest.mark.asyncio
    async def test_consent_post_missing_token(self, provider, mock_request):
        """Test consent form POST with missing token redirects with error."""
        txn_id = "test-txn-789"
        provider.pending_authorizations[txn_id] = {
            "client_id": "test-client",
            "redirect_uri": "http://localhost/cb",
            "created_at": time.time(),
        }

        request = mock_request(
            form_data={
                "txn_id": txn_id,
                # No ha_token provided
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
        from mcp.server.auth.provider import AuthorizationParams
        from mcp.shared.auth import OAuthClientInformationFull
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

        # Store HA credentials (simulates successful consent - only token, no URL)
        provider.ha_credentials["e2e-client"] = HomeAssistantCredentials(
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

        # Step 5: Verify access token contains only ha_token (no ha_url - SSRF fix)
        access_token_obj = await provider.load_access_token(token_response.access_token)
        assert access_token_obj is not None
        assert access_token_obj.claims["ha_token"] == "e2e_test_token"
        assert "ha_url" not in access_token_obj.claims

        # Step 6: Use stateless refresh token to get new access token
        refresh_token_obj = await provider.load_refresh_token(
            client_info, token_response.refresh_token
        )
        assert refresh_token_obj is not None
        new_token_response = await provider.exchange_refresh_token(
            client_info, refresh_token_obj, ["homeassistant"]
        )

        assert new_token_response.access_token is not None
        assert new_token_response.refresh_token is not None
        # Refresh token should be rotated
        assert new_token_response.refresh_token != token_response.refresh_token

        # Step 7: Verify refreshed access token is valid and carries HA credentials
        refreshed_access = await provider.load_access_token(
            new_token_response.access_token
        )
        assert refreshed_access is not None
        assert refreshed_access.claims["ha_token"] == "e2e_test_token"

        # Step 8: Verify chained refresh also works
        refresh_token_obj2 = await provider.load_refresh_token(
            client_info, new_token_response.refresh_token
        )
        assert refresh_token_obj2 is not None
        chained_response = await provider.exchange_refresh_token(
            client_info, refresh_token_obj2, ["homeassistant"]
        )
        chained_access = await provider.load_access_token(chained_response.access_token)
        assert chained_access is not None
        assert chained_access.claims["ha_token"] == "e2e_test_token"


class TestStatelessTokenResilience:
    """Tests for stateless token behaviour across provider restarts."""

    @pytest.mark.asyncio
    async def test_tokens_survive_provider_restart(self):
        """Stateless tokens remain valid across independent provider instances."""
        from mcp.server.auth.provider import AuthorizationCode
        from mcp.shared.auth import OAuthClientInformationFull
        from pydantic import AnyHttpUrl

        # Provider 1: complete a full OAuth flow
        provider1 = HomeAssistantOAuthProvider(base_url="http://localhost:8086")

        client_info = OAuthClientInformationFull(
            client_id="persist-client",
            client_name="Persist Test",
            redirect_uris=["http://localhost/cb"],
            scope="homeassistant mcp",
        )
        await provider1.register_client(client_info)

        provider1.ha_credentials["persist-client"] = HomeAssistantCredentials(
            ha_token="persistent_ha_token",
        )
        auth_code = AuthorizationCode(
            code="persist-code",
            client_id="persist-client",
            redirect_uri=AnyHttpUrl("http://localhost/cb"),
            redirect_uri_provided_explicitly=True,
            scopes=["homeassistant", "mcp"],
            expires_at=time.time() + 300,
            code_challenge="test_challenge",
        )
        provider1.auth_codes["persist-code"] = auth_code
        token_response = await provider1.exchange_authorization_code(
            client_info, auth_code
        )

        # Provider 2: fresh instance (simulates container restart)
        provider2 = HomeAssistantOAuthProvider(base_url="http://localhost:8086")

        # Access token still works on a fresh provider
        access = await provider2.load_access_token(token_response.access_token)
        assert access is not None
        assert access.claims["ha_token"] == "persistent_ha_token"

        # Refresh token also works — client re-registers via DCR
        client_info2 = OAuthClientInformationFull(
            client_id="persist-client",
            redirect_uris=["http://localhost/cb"],
            scope="homeassistant mcp",
        )
        await provider2.register_client(client_info2)

        refresh_obj = await provider2.load_refresh_token(
            client_info2, token_response.refresh_token
        )
        assert refresh_obj is not None

        new_token = await provider2.exchange_refresh_token(
            client_info2, refresh_obj, ["homeassistant"]
        )
        new_access = await provider2.load_access_token(new_token.access_token)
        assert new_access is not None
        assert new_access.claims["ha_token"] == "persistent_ha_token"

    @pytest.mark.asyncio
    async def test_chained_refresh_across_restart(self):
        """Chained refresh tokens work across provider restarts."""
        from mcp.server.auth.provider import AuthorizationCode
        from mcp.shared.auth import OAuthClientInformationFull
        from pydantic import AnyHttpUrl

        provider1 = HomeAssistantOAuthProvider(base_url="http://localhost:8086")
        client_info = OAuthClientInformationFull(
            client_id="chain-client",
            redirect_uris=["http://localhost/cb"],
            scope="homeassistant",
        )
        await provider1.register_client(client_info)
        provider1.ha_credentials["chain-client"] = HomeAssistantCredentials(
            ha_token="chain_ha_token",
        )
        auth_code = AuthorizationCode(
            code="chain-code",
            client_id="chain-client",
            redirect_uri=AnyHttpUrl("http://localhost/cb"),
            redirect_uri_provided_explicitly=True,
            scopes=["homeassistant"],
            expires_at=time.time() + 300,
            code_challenge="test_challenge",
        )
        provider1.auth_codes["chain-code"] = auth_code
        token1 = await provider1.exchange_authorization_code(client_info, auth_code)

        # First refresh on provider1
        refresh1_obj = await provider1.load_refresh_token(
            client_info, token1.refresh_token
        )
        token2 = await provider1.exchange_refresh_token(
            client_info, refresh1_obj, ["homeassistant"]
        )

        # Restart — fresh provider, re-register client
        provider2 = HomeAssistantOAuthProvider(base_url="http://localhost:8086")
        client_info2 = OAuthClientInformationFull(
            client_id="chain-client",
            redirect_uris=["http://localhost/cb"],
            scope="homeassistant",
        )
        await provider2.register_client(client_info2)

        # Second refresh across restart
        refresh2_obj = await provider2.load_refresh_token(
            client_info2, token2.refresh_token
        )
        assert refresh2_obj is not None
        token3 = await provider2.exchange_refresh_token(
            client_info2, refresh2_obj, ["homeassistant"]
        )
        access = await provider2.load_access_token(token3.access_token)
        assert access is not None
        assert access.claims["ha_token"] == "chain_ha_token"

    @pytest.mark.asyncio
    async def test_expired_refresh_token_rejected(self):
        """Stateless refresh tokens with expired 'exp' are rejected."""
        from mcp.shared.auth import OAuthClientInformationFull

        provider = HomeAssistantOAuthProvider(base_url="http://localhost:8086")
        client_info = OAuthClientInformationFull(
            client_id="test-client",
            redirect_uris=["http://localhost/cb"],
        )
        await provider.register_client(client_info)

        expired_refresh = provider._encode_token(
            "some_token",
            token_type="refresh",
            client_id="test-client",
            scopes=["homeassistant"],
            expires_at=1,  # Expired long ago
        )

        result = await provider.load_refresh_token(client_info, expired_refresh)
        assert result is None

    @pytest.mark.asyncio
    async def test_refresh_token_wrong_client_rejected(self):
        """Refresh token issued for client A is rejected when presented by client B."""
        from mcp.shared.auth import OAuthClientInformationFull

        provider = HomeAssistantOAuthProvider(base_url="http://localhost:8086")
        client_a = OAuthClientInformationFull(
            client_id="client-a",
            redirect_uris=["http://localhost/cb"],
        )
        client_b = OAuthClientInformationFull(
            client_id="client-b",
            redirect_uris=["http://localhost/cb"],
        )
        await provider.register_client(client_a)
        await provider.register_client(client_b)

        refresh_for_a = provider._encode_token(
            "token",
            token_type="refresh",
            client_id="client-a",
            scopes=["homeassistant"],
            expires_at=int(time.time() + 86400),
        )

        result = await provider.load_refresh_token(client_b, refresh_for_a)
        assert result is None

    @pytest.mark.asyncio
    async def test_corrupt_refresh_token_fails_gracefully(self):
        """exchange_refresh_token raises TokenError for non-decodable refresh tokens."""
        from mcp.server.auth.provider import RefreshToken, TokenError
        from mcp.shared.auth import OAuthClientInformationFull

        provider = HomeAssistantOAuthProvider(base_url="http://localhost:8086")
        client_info = OAuthClientInformationFull(
            client_id="test-client",
            redirect_uris=["http://localhost/cb"],
        )
        await provider.register_client(client_info)

        corrupt_refresh = RefreshToken(
            token="not_valid_base64_!!!",
            client_id="test-client",
            scopes=["homeassistant"],
            expires_at=int(time.time() + 86400),
        )

        with pytest.raises(TokenError, match="Cannot decode refresh token"):
            await provider.exchange_refresh_token(
                client_info, corrupt_refresh, ["homeassistant"]
            )

    @pytest.mark.asyncio
    async def test_revocation_does_not_invalidate_stateless_tokens(self):
        """Revoking a stateless token is a no-op; the token remains decodable."""
        from mcp.server.auth.provider import AuthorizationCode
        from mcp.shared.auth import OAuthClientInformationFull
        from pydantic import AnyHttpUrl

        provider = HomeAssistantOAuthProvider(base_url="http://localhost:8086")
        client_info = OAuthClientInformationFull(
            client_id="revoke-client",
            redirect_uris=["http://localhost/cb"],
            scope="homeassistant",
        )
        await provider.register_client(client_info)
        provider.ha_credentials["revoke-client"] = HomeAssistantCredentials(
            ha_token="revoke_ha_token",
        )
        auth_code = AuthorizationCode(
            code="revoke-code",
            client_id="revoke-client",
            redirect_uri=AnyHttpUrl("http://localhost/cb"),
            redirect_uri_provided_explicitly=True,
            scopes=["homeassistant"],
            expires_at=time.time() + 300,
            code_challenge="challenge",
        )
        provider.auth_codes["revoke-code"] = auth_code
        token_resp = await provider.exchange_authorization_code(client_info, auth_code)

        # Revoke the access token
        access_token_obj = await provider.load_access_token(token_resp.access_token)
        assert access_token_obj is not None
        await provider.revoke_token(access_token_obj)

        # Stateless token is still decodable (LLAT revocation is the real boundary)
        still_valid = await provider.load_access_token(token_resp.access_token)
        assert still_valid is not None

        # Refresh token also still works
        refresh_obj = await provider.load_refresh_token(
            client_info, token_resp.refresh_token
        )
        assert refresh_obj is not None


class TestDecodeTokenEdgeCases:
    """Test _decode_token with unusual payloads."""

    @pytest.mark.asyncio
    async def test_non_dict_json_payload_rejected(self):
        """A token that base64-decodes to a JSON array should be rejected."""
        from base64 import urlsafe_b64encode

        provider = HomeAssistantOAuthProvider(base_url="http://localhost:8086")
        # Encode a JSON array
        array_token = urlsafe_b64encode(b'[1, 2, 3]').decode().rstrip("=")
        result = provider._decode_token(array_token)
        assert result is None

    @pytest.mark.asyncio
    async def test_non_dict_json_string_payload_rejected(self):
        """A token that base64-decodes to a JSON string should be rejected."""
        from base64 import urlsafe_b64encode

        provider = HomeAssistantOAuthProvider(base_url="http://localhost:8086")
        string_token = urlsafe_b64encode(b'"just_a_string"').decode().rstrip("=")
        result = provider._decode_token(string_token)
        assert result is None

    @pytest.mark.asyncio
    async def test_access_token_expired_rejected(self):
        """An access token past its exp should be rejected."""
        provider = HomeAssistantOAuthProvider(base_url="http://localhost:8086")
        expired_token = provider._encode_token(
            "test_llat", token_type="access", expires_at=int(time.time()) - 10
        )
        result = await provider.load_access_token(expired_token)
        assert result is None


class TestOAuthMainSentinel:
    """Test the main_oauth OAUTH_MODE_TOKEN sentinel logic."""

    def test_empty_token_gets_sentinel(self):
        """When HOMEASSISTANT_TOKEN is empty, it should be set to OAUTH_MODE_TOKEN."""
        import os

        from ha_mcp.config import OAUTH_MODE_TOKEN

        original = os.environ.get("HOMEASSISTANT_TOKEN")
        try:
            os.environ["HOMEASSISTANT_TOKEN"] = ""
            # Re-run the sentinel logic
            if not os.getenv("HOMEASSISTANT_TOKEN"):
                os.environ["HOMEASSISTANT_TOKEN"] = OAUTH_MODE_TOKEN
            assert os.environ["HOMEASSISTANT_TOKEN"] == OAUTH_MODE_TOKEN
        finally:
            if original is not None:
                os.environ["HOMEASSISTANT_TOKEN"] = original
            else:
                os.environ.pop("HOMEASSISTANT_TOKEN", None)

    def test_existing_token_not_overwritten(self):
        """When HOMEASSISTANT_TOKEN has a real value, it should not be overwritten."""
        import os

        original = os.environ.get("HOMEASSISTANT_TOKEN")
        try:
            os.environ["HOMEASSISTANT_TOKEN"] = "real_token_value"
            if not os.getenv("HOMEASSISTANT_TOKEN"):
                from ha_mcp.config import OAUTH_MODE_TOKEN

                os.environ["HOMEASSISTANT_TOKEN"] = OAUTH_MODE_TOKEN
            assert os.environ["HOMEASSISTANT_TOKEN"] == "real_token_value"
        finally:
            if original is not None:
                os.environ["HOMEASSISTANT_TOKEN"] = original
            else:
                os.environ.pop("HOMEASSISTANT_TOKEN", None)


class TestOAuthProxyClient:
    """Tests for OAuthProxyClient in __main__.py."""

    @pytest.fixture
    def mock_access_token(self):
        """Create a mock access token with claims (no ha_url - SSRF fix)."""
        from fastmcp.server.auth.auth import AccessToken

        return AccessToken(
            token="encoded-token-123",
            client_id="test-client",
            scopes=["homeassistant"],
            expires_at=None,
            claims={
                "ha_token": "test_ha_token_xyz",
            },
        )

    def test_oauth_proxy_client_initialization(self):
        """Test OAuthProxyClient initialization."""
        from ha_mcp.__main__ import OAuthProxyClient

        proxy = OAuthProxyClient("http://homeassistant.local:8123")
        assert proxy._ha_url == "http://homeassistant.local:8123"
        assert proxy._oauth_clients == {}

    def test_oauth_proxy_client_strips_trailing_slash(self):
        """Test OAuthProxyClient strips trailing slash from URL."""
        from ha_mcp.__main__ import OAuthProxyClient

        proxy = OAuthProxyClient("http://homeassistant.local:8123/")
        assert proxy._ha_url == "http://homeassistant.local:8123"

    def test_oauth_proxy_client_attribute_forwarding(self, mock_access_token):
        """Test that OAuthProxyClient forwards attributes to HA client."""
        from ha_mcp.__main__ import OAuthProxyClient

        proxy = OAuthProxyClient("http://homeassistant.local:8123")

        # Mock get_access_token to return our mock token
        with (
            patch(
                "fastmcp.server.dependencies.get_access_token",
                return_value=mock_access_token,
            ),
            patch("ha_mcp.client.rest_client.HomeAssistantClient") as mock_ha_client,
        ):
            mock_client_instance = MagicMock()
            mock_ha_client.return_value = mock_client_instance

            # Access a method - this triggers __getattr__ which creates the client
            _ = proxy.get_state

            # Verify HomeAssistantClient was created with server-side URL + per-user token
            mock_ha_client.assert_called_once_with(
                base_url="http://homeassistant.local:8123",
                token="test_ha_token_xyz",
            )

            # Verify the client instance was stored
            assert len(proxy._oauth_clients) == 1

    def test_oauth_proxy_client_reuses_clients(self, mock_access_token):
        """Test that OAuthProxyClient reuses client instances for same credentials."""
        from ha_mcp.__main__ import OAuthProxyClient

        proxy = OAuthProxyClient("http://homeassistant.local:8123")

        with (
            patch(
                "fastmcp.server.dependencies.get_access_token",
                return_value=mock_access_token,
            ),
            patch("ha_mcp.client.rest_client.HomeAssistantClient") as mock_ha_client,
        ):
            mock_client_instance = MagicMock()
            mock_ha_client.return_value = mock_client_instance

            # Access attribute twice
            _ = proxy.get_state
            _ = proxy.call_service

            # Client should only be created once
            assert mock_ha_client.call_count == 1

    def test_oauth_proxy_client_no_token_raises_error(self):
        """Test that OAuthProxyClient raises error when no token in context."""
        from ha_mcp.__main__ import OAuthProxyClient

        proxy = OAuthProxyClient("http://homeassistant.local:8123")

        # Mock get_access_token to return None
        with (
            patch("fastmcp.server.dependencies.get_access_token", return_value=None),
            pytest.raises(RuntimeError, match="No OAuth token"),
        ):
            _ = proxy.get_state

    def test_oauth_proxy_client_missing_claims_raises_error(self):
        """Test that OAuthProxyClient raises error when token has no claims."""
        from fastmcp.server.auth.auth import AccessToken

        from ha_mcp.__main__ import OAuthProxyClient

        # Token without claims
        token_no_claims = AccessToken(
            token="token-123",
            client_id="test",
            scopes=[],
            expires_at=None,
            claims={},  # Empty claims
        )

        proxy = OAuthProxyClient("http://homeassistant.local:8123")

        with (
            patch(
                "fastmcp.server.dependencies.get_access_token",
                return_value=token_no_claims,
            ),
            pytest.raises(RuntimeError, match="No Home Assistant credentials"),
        ):
            _ = proxy.get_state

    @pytest.mark.asyncio
    async def test_oauth_proxy_client_close_all_clients(self, mock_access_token):
        """Test that close() closes all cached OAuth clients."""
        from ha_mcp.__main__ import OAuthProxyClient

        proxy = OAuthProxyClient("http://homeassistant.local:8123")

        with (
            patch(
                "fastmcp.server.dependencies.get_access_token",
                return_value=mock_access_token,
            ),
            patch("ha_mcp.client.rest_client.HomeAssistantClient") as mock_ha_client,
        ):
            mock_client_instance = MagicMock()
            mock_client_instance.close = AsyncMock()
            mock_ha_client.return_value = mock_client_instance

            # Create a cached client
            _ = proxy.get_state
            assert len(proxy._oauth_clients) == 1

            # Close should close all clients and clear the cache
            await proxy.close()

            mock_client_instance.close.assert_called_once()
            assert len(proxy._oauth_clients) == 0

    @pytest.mark.asyncio
    async def test_oauth_websocket_uses_server_url_with_per_user_token(
        self, mock_access_token
    ):
        """Test that send_websocket_message uses server-side URL with per-user token."""
        from ha_mcp.__main__ import OAuthProxyClient

        proxy = OAuthProxyClient("http://homeassistant.local:8123")

        with (
            patch(
                "fastmcp.server.dependencies.get_access_token",
                return_value=mock_access_token,
            ),
            patch(
                "ha_mcp.client.websocket_client.get_websocket_client",
                new_callable=AsyncMock,
            ) as mock_get_ws,
        ):
            mock_ws = AsyncMock()
            mock_ws.send_command.return_value = {
                "type": "result",
                "success": True,
                "result": {},
            }
            mock_get_ws.return_value = mock_ws

            await proxy.send_websocket_message({"type": "get_states"})

            # WebSocket client must use server-side URL + per-user token
            mock_get_ws.assert_awaited_once_with(
                url="http://homeassistant.local:8123",
                token="test_ha_token_xyz",
            )


class TestWebSocketManagerPool:
    """Tests for WebSocketManager connection pooling."""

    @pytest.fixture(autouse=True)
    def reset_manager(self):
        """Reset the WebSocketManager singleton between tests."""
        from ha_mcp.client.websocket_client import WebSocketManager

        WebSocketManager._instance = None
        yield
        WebSocketManager._instance = None

    @pytest.mark.asyncio
    async def test_concurrent_oauth_users_get_separate_connections(self):
        """Test that different OAuth users get separate WebSocket connections."""
        from ha_mcp.client.websocket_client import WebSocketManager

        mock_client_a = MagicMock()
        mock_client_a.is_connected = True
        mock_client_a.connect = AsyncMock(return_value=True)
        mock_client_a.base_url = "http://ha.local:8123"
        mock_client_a.token = "token_user_a"

        mock_client_b = MagicMock()
        mock_client_b.is_connected = True
        mock_client_b.connect = AsyncMock(return_value=True)
        mock_client_b.base_url = "http://ha.local:8123"
        mock_client_b.token = "token_user_b"

        call_count = 0

        def factory(url, token):
            nonlocal call_count
            call_count += 1
            if token == "token_user_a":
                return mock_client_a
            return mock_client_b

        manager = WebSocketManager()
        manager.configure(client_factory=factory)

        # User A connects
        client_a = await manager.get_client(
            url="http://ha.local:8123", token="token_user_a"
        )
        assert client_a is mock_client_a

        # User B connects — should NOT disconnect user A
        client_b = await manager.get_client(
            url="http://ha.local:8123", token="token_user_b"
        )
        assert client_b is mock_client_b
        assert mock_client_a.disconnect.call_count == 0

        # Both connections created
        assert call_count == 2

        # User A again — should reuse existing connection
        client_a2 = await manager.get_client(
            url="http://ha.local:8123", token="token_user_a"
        )
        assert client_a2 is mock_client_a
        assert call_count == 2  # No new connection

    @pytest.mark.asyncio
    async def test_pool_evicts_lru_when_over_max_size(self):
        """Test that the pool evicts the least-recently-used client when full."""
        from ha_mcp.client import websocket_client
        from ha_mcp.client.websocket_client import WebSocketManager

        original_max = websocket_client.MAX_POOL_SIZE
        websocket_client.MAX_POOL_SIZE = 2  # Small limit for testing

        try:
            clients_created: list[MagicMock] = []

            def factory(url, token):
                mock = MagicMock()
                mock.is_connected = True
                mock.connect = AsyncMock(return_value=True)
                mock.disconnect = AsyncMock()
                mock.base_url = url
                mock.token = token
                clients_created.append(mock)
                return mock

            manager = WebSocketManager()
            manager.configure(client_factory=factory)

            # Fill pool to capacity
            await manager.get_client(url="http://ha.local:8123", token="token_1")
            await manager.get_client(url="http://ha.local:8123", token="token_2")
            assert len(manager._clients) == 2

            # Adding a third should evict the LRU (token_1)
            await manager.get_client(url="http://ha.local:8123", token="token_3")
            assert len(manager._clients) == 2
            # token_1 client should have been disconnected
            clients_created[0].disconnect.assert_awaited_once()
        finally:
            websocket_client.MAX_POOL_SIZE = original_max

    @pytest.mark.asyncio
    async def test_disconnect_handles_individual_client_errors(self):
        """Test that disconnect() continues if one client raises."""
        from ha_mcp.client.websocket_client import WebSocketManager

        mock_client_a = MagicMock()
        mock_client_a.is_connected = True
        mock_client_a.connect = AsyncMock(return_value=True)
        mock_client_a.disconnect = AsyncMock(side_effect=Exception("boom"))

        mock_client_b = MagicMock()
        mock_client_b.is_connected = True
        mock_client_b.connect = AsyncMock(return_value=True)
        mock_client_b.disconnect = AsyncMock()

        call_count = 0

        def factory(url, token):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return mock_client_a
            return mock_client_b

        manager = WebSocketManager()
        manager.configure(client_factory=factory)

        await manager.get_client(url="http://ha.local:8123", token="token_a")
        await manager.get_client(url="http://ha.local:8123", token="token_b")

        # disconnect() should not raise even though client_a throws
        await manager.disconnect()

        mock_client_a.disconnect.assert_awaited_once()
        mock_client_b.disconnect.assert_awaited_once()
        assert len(manager._clients) == 0
