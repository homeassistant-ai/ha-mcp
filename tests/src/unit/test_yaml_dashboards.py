"""Unit tests for YAML-mode dashboard validators in ha_mcp_tools."""

import sys
from unittest.mock import MagicMock

import pytest

# Mock HA imports before importing the module
sys.modules["voluptuous"] = MagicMock()
sys.modules["homeassistant"] = MagicMock()
sys.modules["homeassistant.config_entries"] = MagicMock()
sys.modules["homeassistant.core"] = MagicMock()
sys.modules["homeassistant.helpers"] = MagicMock()
sys.modules["homeassistant.helpers.config_validation"] = MagicMock()

from custom_components.ha_mcp_tools.const import (  # noqa: E402
    DASHBOARD_URL_PATH_PATTERN,
    RESERVED_DASHBOARD_URL_PATHS,
)


class TestDashboardUrlPathPattern:
    """url_path must match HA's lovelace dashboard rules: lowercase, hyphenated."""

    @pytest.mark.parametrize(
        "url_path",
        [
            "energy-dashboard",
            "my-dashboard",
            "main-view",
            "a-b",
            "long-multi-segment-path",
            "with-123-numbers",
        ],
    )
    def test_accepts_valid_url_paths(self, url_path):
        assert DASHBOARD_URL_PATH_PATTERN.fullmatch(url_path), url_path

    @pytest.mark.parametrize(
        "url_path",
        [
            "",                     # empty
            "no_underscore",        # underscores not allowed
            "NoUpper",              # uppercase not allowed
            "single",               # must contain a hyphen
            "-leading-hyphen",      # cannot start with hyphen
            "trailing-hyphen-",     # cannot end with hyphen
            "double--hyphen",       # no consecutive hyphens
            "has space",            # no spaces
            "has/slash",            # no slashes
            "has.dot",              # no dots
            "..",                   # path traversal-ish
        ],
    )
    def test_rejects_invalid_url_paths(self, url_path):
        assert not DASHBOARD_URL_PATH_PATTERN.fullmatch(url_path), url_path


class TestReservedDashboardUrlPaths:
    """Reserved url_paths used by HA core dashboards must be excluded."""

    def test_includes_lovelace(self):
        assert "lovelace" in RESERVED_DASHBOARD_URL_PATHS

    def test_includes_core_dashboard_routes(self):
        for name in (
            "overview",
            "map",
            "logbook",
            "history",
            "energy",
            "developer-tools",
            "config",
            "profile",
            "media-browser",
            "todo",
            "calendar",
        ):
            assert name in RESERVED_DASHBOARD_URL_PATHS, name

    def test_is_frozenset(self):
        assert isinstance(RESERVED_DASHBOARD_URL_PATHS, frozenset)


class TestValidateDashboardFilename:
    """`filename:` value in a YAML-mode dashboard entry must stay under dashboards/."""

    @pytest.fixture(scope="class")
    def validate(self):
        from custom_components.ha_mcp_tools import _validate_dashboard_filename
        return _validate_dashboard_filename

    @pytest.mark.parametrize(
        "filename",
        [
            "dashboards/main.yaml",
            "dashboards/sub/nested.yaml",
            "dashboards/energy-2026.yaml",
        ],
    )
    def test_accepts_valid_filenames(self, validate, filename):
        err = validate(filename)
        assert err is None, f"{filename} should be valid, got: {err}"

    @pytest.mark.parametrize(
        "filename",
        [
            "../secrets.yaml",
            "/etc/passwd",
            "dashboards/../secrets.yaml",
            "dashboards/main.yml",       # wrong extension
            "main.yaml",                 # not under dashboards/
            "www/dashboard.yaml",        # other allowlist dir, not dashboards
            "",
            "dashboards/",
            "dashboards/main",           # no extension
        ],
    )
    def test_rejects_invalid_filenames(self, validate, filename):
        err = validate(filename)
        assert err is not None, f"{filename} should be rejected"
        assert isinstance(err, str)
