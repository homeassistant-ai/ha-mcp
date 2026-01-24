"""Unit tests for configuration handling."""

import os
import subprocess
import sys



class TestConfigErrorHandling:
    """Test configuration error handling and user-friendly messages."""

    def test_missing_env_vars_shows_friendly_message(self):
        """When HOMEASSISTANT_URL and TOKEN are missing, show friendly error."""
        # Run ha-mcp without any env vars set
        env = os.environ.copy()
        # Remove any HA env vars that might be set
        env.pop("HOMEASSISTANT_URL", None)
        env.pop("HOMEASSISTANT_TOKEN", None)
        env.pop("HAMCP_ENV_FILE", None)

        result = subprocess.run(
            [sys.executable, "-m", "ha_mcp"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

        # Should exit with error code
        assert result.returncode != 0

        # Should show friendly message, not raw stacktrace
        stderr = result.stderr
        assert "Configuration Error" in stderr
        assert "HOMEASSISTANT_URL" in stderr
        assert "HOMEASSISTANT_TOKEN" in stderr
        assert "Long-Lived Access Tokens" in stderr
        assert "github.com/homeassistant-ai/ha-mcp" in stderr

        # Should NOT show raw pydantic validation error
        assert "pydantic_core._pydantic_core.ValidationError" not in stderr
        assert "Field required [type=missing" not in stderr

    def test_missing_only_url_shows_that_var(self):
        """When only HOMEASSISTANT_URL is missing, show that in message."""
        env = os.environ.copy()
        env.pop("HOMEASSISTANT_URL", None)
        env.pop("HAMCP_ENV_FILE", None)
        env["HOMEASSISTANT_TOKEN"] = "test_token_value"

        result = subprocess.run(
            [sys.executable, "-m", "ha_mcp"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode != 0
        assert "HOMEASSISTANT_URL" in result.stderr

    def test_missing_only_token_shows_that_var(self):
        """When only HOMEASSISTANT_TOKEN is missing, show that in message."""
        env = os.environ.copy()
        env.pop("HOMEASSISTANT_TOKEN", None)
        env.pop("HAMCP_ENV_FILE", None)
        env["HOMEASSISTANT_URL"] = "http://test.local:8123"

        result = subprocess.run(
            [sys.executable, "-m", "ha_mcp"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode != 0
        assert "HOMEASSISTANT_TOKEN" in result.stderr

    def test_no_env_file_warning_removed(self):
        """No warning should be shown when .env file is missing."""
        env = os.environ.copy()
        env.pop("HOMEASSISTANT_URL", None)
        env.pop("HOMEASSISTANT_TOKEN", None)
        env.pop("HAMCP_ENV_FILE", None)

        result = subprocess.run(
            [sys.executable, "-m", "ha_mcp"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

        # Should NOT contain the old noisy warning
        combined_output = result.stdout + result.stderr
        assert "[ENV] WARNING: No environment file found" not in combined_output

    def test_smoke_test_still_works(self):
        """Smoke test should work with dummy credentials."""
        env = os.environ.copy()
        # Smoke test sets its own dummy credentials
        env.pop("HAMCP_ENV_FILE", None)

        result = subprocess.run(
            [sys.executable, "-m", "ha_mcp", "--smoke-test"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

        assert result.returncode == 0
        assert "SMOKE TEST PASSED" in result.stdout
