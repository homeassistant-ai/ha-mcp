"""Unit tests for add-on OIDC configuration functions in start.py.

Tests _get_oidc_config(), _validate_oidc_config(), and the mode selection
logic without requiring a running Home Assistant Supervisor.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Import start.py from the addon directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "homeassistant-addon"))
from start import _get_oidc_config, _validate_oidc_config


class TestGetOidcConfig:
    """Tests for _get_oidc_config() extraction from add-on options."""

    def test_empty_config_returns_empty(self):
        """Empty config should return empty dict."""
        assert _get_oidc_config({}) == {}

    def test_no_oidc_fields_returns_empty(self):
        """Config without OIDC fields should return empty dict."""
        config = {"backup_hint": "normal", "secret_path": "/custom"}
        assert _get_oidc_config(config) == {}

    def test_all_oidc_fields_set(self):
        """Config with all OIDC fields should return all of them."""
        config = {
            "oidc_config_url": "https://auth.example.com/.well-known/openid-configuration",
            "oidc_client_id": "my-client",
            "oidc_client_secret": "my-secret",
            "oidc_base_url": "https://mcp.example.com",
        }
        result = _get_oidc_config(config)
        assert result == config

    def test_jwt_signing_key_included_when_set(self):
        """Config with oidc_jwt_signing_key should include it in result."""
        config = {
            "oidc_config_url": "https://auth.example.com/.well-known/openid-configuration",
            "oidc_client_id": "my-client",
            "oidc_client_secret": "my-secret",
            "oidc_base_url": "https://mcp.example.com",
            "oidc_jwt_signing_key": "my-signing-key-abc123",
        }
        result = _get_oidc_config(config)
        assert result["oidc_jwt_signing_key"] == "my-signing-key-abc123"

    def test_jwt_signing_key_excluded_when_empty(self):
        """Empty oidc_jwt_signing_key should be excluded from result."""
        config = {
            "oidc_config_url": "https://auth.example.com/.well-known/openid-configuration",
            "oidc_client_id": "my-client",
            "oidc_client_secret": "my-secret",
            "oidc_base_url": "https://mcp.example.com",
            "oidc_jwt_signing_key": "",
        }
        result = _get_oidc_config(config)
        assert "oidc_jwt_signing_key" not in result

    def test_empty_string_fields_excluded(self):
        """Empty string OIDC fields should be excluded."""
        config = {
            "oidc_config_url": "https://auth.example.com/.well-known/openid-configuration",
            "oidc_client_id": "",
            "oidc_client_secret": "my-secret",
            "oidc_base_url": "",
        }
        result = _get_oidc_config(config)
        assert "oidc_client_id" not in result
        assert "oidc_base_url" not in result
        assert "oidc_config_url" in result
        assert "oidc_client_secret" in result

    def test_whitespace_only_fields_excluded(self):
        """Whitespace-only OIDC fields should be excluded."""
        config = {
            "oidc_config_url": "   ",
            "oidc_client_id": "my-client",
            "oidc_client_secret": "my-secret",
            "oidc_base_url": "https://mcp.example.com",
        }
        result = _get_oidc_config(config)
        assert "oidc_config_url" not in result

    def test_partial_oidc_fields(self):
        """Only set OIDC fields should be returned."""
        config = {
            "oidc_config_url": "https://auth.example.com/.well-known/openid-configuration",
            "oidc_client_id": "my-client",
            "backup_hint": "normal",
        }
        result = _get_oidc_config(config)
        assert len(result) == 2
        assert "backup_hint" not in result


class TestValidateOidcConfig:
    """Tests for _validate_oidc_config() completeness checking."""

    def test_empty_config_is_valid(self):
        """Empty config (no OIDC) should return None (valid)."""
        assert _validate_oidc_config({}) is None

    def test_complete_config_is_valid(self):
        """Complete OIDC config should return None (valid)."""
        config = {
            "oidc_config_url": "https://auth.example.com/.well-known/openid-configuration",
            "oidc_client_id": "my-client",
            "oidc_client_secret": "my-secret",
            "oidc_base_url": "https://mcp.example.com",
        }
        assert _validate_oidc_config(config) is None

    def test_complete_config_with_jwt_key_is_valid(self):
        """Complete OIDC config with optional jwt signing key should be valid."""
        config = {
            "oidc_config_url": "https://auth.example.com/.well-known/openid-configuration",
            "oidc_client_id": "my-client",
            "oidc_client_secret": "my-secret",
            "oidc_base_url": "https://mcp.example.com",
            "oidc_jwt_signing_key": "my-signing-key",
        }
        assert _validate_oidc_config(config) is None

    def test_partial_config_returns_error(self):
        """Partial OIDC config should return error message."""
        config = {
            "oidc_config_url": "https://auth.example.com/.well-known/openid-configuration",
            "oidc_client_id": "my-client",
        }
        error = _validate_oidc_config(config)
        assert error is not None
        assert "Incomplete OIDC configuration" in error

    def test_partial_config_lists_missing_fields(self):
        """Error message should list the missing fields."""
        config = {
            "oidc_config_url": "https://auth.example.com/.well-known/openid-configuration",
        }
        error = _validate_oidc_config(config)
        assert "OIDC Client ID" in error
        assert "OIDC Client Secret" in error
        assert "OIDC Public Base URL" in error
        assert "OIDC Discovery URL" not in error

    def test_single_missing_field(self):
        """Error should identify the single missing field."""
        config = {
            "oidc_config_url": "https://auth.example.com/.well-known/openid-configuration",
            "oidc_client_id": "my-client",
            "oidc_client_secret": "my-secret",
            # oidc_base_url missing
        }
        error = _validate_oidc_config(config)
        assert error is not None
        assert "OIDC Public Base URL" in error
        assert "OIDC Discovery URL" not in error
        assert "OIDC Client ID" not in error
        assert "OIDC Client Secret" not in error

    def test_error_suggests_secret_path_mode(self):
        """Error message should mention secret path mode as alternative."""
        config = {"oidc_config_url": "https://auth.example.com/.well-known/openid-configuration"}
        error = _validate_oidc_config(config)
        assert "secret path mode" in error
