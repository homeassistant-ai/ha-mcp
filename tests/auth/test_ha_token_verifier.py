"""Tests for Home Assistant token verification."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from ha_mcp.auth.ha_token_verifier import HATokenVerifier


class TestHATokenVerifier:
    """Test Home Assistant token verifier."""

    @pytest.fixture
    def mock_supervisor_token(self):
        """Mock SUPERVISOR_TOKEN environment variable."""
        with patch.dict(os.environ, {"SUPERVISOR_TOKEN": "mock-supervisor-token"}):
            yield

    @pytest.fixture
    def verifier(self, mock_supervisor_token):
        """Create a HATokenVerifier instance."""
        return HATokenVerifier()

    async def test_init_with_supervisor_token(self, mock_supervisor_token):
        """Test initialization with SUPERVISOR_TOKEN present."""
        verifier = HATokenVerifier()
        assert verifier.ha_api_url == "http://supervisor/core/api/"
        assert verifier.required_scopes == []

    async def test_init_without_supervisor_token(self):
        """Test initialization without SUPERVISOR_TOKEN (warning logged)."""
        with patch.dict(os.environ, {}, clear=True):
            verifier = HATokenVerifier()
            assert verifier.ha_api_url == "http://supervisor/core/api/"

    async def test_verify_token_valid(self, verifier):
        """Test token verification with valid token."""
        mock_response = MagicMock()
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await verifier.verify_token("valid-token")

        assert result is not None
        assert result.client_id == "ha-user"
        assert result.scopes == []

        # Verify the API was called with correct headers
        mock_client.get.assert_called_once()
        call_args = mock_client.get.call_args
        assert call_args[0][0] == "http://supervisor/core/api/"
        assert call_args[1]["headers"]["Authorization"] == "Bearer valid-token"

    async def test_verify_token_invalid(self, verifier):
        """Test token verification with invalid token (401)."""
        mock_response = MagicMock()
        mock_response.status_code = 401

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await verifier.verify_token("invalid-token")

        assert result is None

    async def test_verify_token_unexpected_status(self, verifier):
        """Test token verification with unexpected status code."""
        mock_response = MagicMock()
        mock_response.status_code = 500

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await verifier.verify_token("some-token")

        assert result is None

    async def test_verify_token_network_error(self, verifier):
        """Test token verification with network error."""
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.HTTPError("Connection failed"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await verifier.verify_token("some-token")

        assert result is None

    async def test_verify_token_unexpected_exception(self, verifier):
        """Test token verification with unexpected exception."""
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("Unexpected error"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await verifier.verify_token("some-token")

        assert result is None

    async def test_verify_token_with_required_scopes(self, mock_supervisor_token):
        """Test token verification with required scopes."""
        verifier = HATokenVerifier(required_scopes=["read", "write"])

        mock_response = MagicMock()
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await verifier.verify_token("valid-token")

        assert result is not None
        assert result.scopes == ["read", "write"]
