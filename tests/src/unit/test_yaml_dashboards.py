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


class TestParseYamlPath:
    """yaml_path must accept either a single allowed key OR
    a 3-segment dotted path 'lovelace.dashboards.<url_path>'."""

    @pytest.fixture(scope="class")
    def parse(self):
        from custom_components.ha_mcp_tools import _parse_and_validate_yaml_path
        return _parse_and_validate_yaml_path

    def test_accepts_single_allowed_key(self, parse):
        kind, parts, err = parse("template")
        assert err is None
        assert kind == "single"
        assert parts == ("template",)

    def test_accepts_lovelace_dashboards_dotted(self, parse):
        kind, parts, err = parse("lovelace.dashboards.energy-dash")
        assert err is None
        assert kind == "lovelace_dashboard"
        assert parts == ("lovelace", "dashboards", "energy-dash")

    def test_rejects_unknown_single_key(self, parse):
        _, _, err = parse("frontend")
        assert err is not None
        assert "not in the allowed list" in err

    def test_rejects_bare_lovelace(self, parse):
        _, _, err = parse("lovelace")
        assert err is not None

    def test_rejects_lovelace_mode(self, parse):
        _, _, err = parse("lovelace.mode")
        assert err is not None
        assert "lovelace.dashboards.<url_path>" in err

    def test_rejects_lovelace_dashboards_without_url_path(self, parse):
        _, _, err = parse("lovelace.dashboards")
        assert err is not None

    def test_rejects_too_many_segments(self, parse):
        _, _, err = parse("lovelace.dashboards.foo.bar")
        assert err is not None

    def test_rejects_reserved_url_path(self, parse):
        _, _, err = parse("lovelace.dashboards.lovelace")
        assert err is not None
        assert "reserved" in err.lower()

    def test_rejects_invalid_url_path_format(self, parse):
        _, _, err = parse("lovelace.dashboards.UPPER")
        assert err is not None

    def test_rejects_other_dotted_path(self, parse):
        _, _, err = parse("homeassistant.customize")
        assert err is not None
