"""Unit tests for YAML-mode dashboard validators in ha_mcp_tools."""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from unittest.mock import MagicMock as MM

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


class TestHandleEditYamlConfigDashboards:
    """Integration of dashboard branch into handle_edit_yaml_config."""

    @pytest.fixture
    def hass(self, tmp_path):
        """Minimal hass mock that runs executor jobs synchronously."""
        h = MM()
        h.config = MM()
        h.config.config_dir = str(tmp_path)
        # Run executor jobs inline so we can assert filesystem state
        async def _run(fn, *args):
            return fn(*args)
        h.async_add_executor_job = AsyncMock(side_effect=_run)
        # check_config service returns 'ok'
        h.services = MM()
        h.services.async_call = AsyncMock(return_value={"errors": None})
        return h

    @pytest.fixture
    def call_factory(self):
        """Build a ServiceCall-like object."""
        def _make(data):
            call = MM()
            call.data = data
            return call
        return _make

    def _run(self, coro):
        return asyncio.run(coro)

    def test_register_yaml_mode_dashboard(self, tmp_path, hass, call_factory):
        """A new lovelace.dashboards.<url_path> entry is added under that key only."""
        from custom_components.ha_mcp_tools import _build_edit_yaml_config_handler

        cfg = Path(tmp_path) / "configuration.yaml"
        cfg.write_text("default_config:\n")

        handler = _build_edit_yaml_config_handler(hass)
        result = self._run(
            handler(
                call_factory(
                    {
                        "file": "configuration.yaml",
                        "action": "add",
                        "yaml_path": "lovelace.dashboards.energy-dash",
                        "content": (
                            "mode: yaml\n"
                            "title: Energy\n"
                            "filename: dashboards/energy.yaml\n"
                            "show_in_sidebar: true\n"
                        ),
                        "backup": False,
                    }
                )
            )
        )
        assert result["success"] is True, result
        text = cfg.read_text()
        assert "lovelace:" in text
        assert "dashboards:" in text
        assert "energy-dash:" in text
        assert "default_config:" in text
        # Must NOT introduce lovelace.mode
        assert "mode: yaml" in text  # appears under the dashboard entry though
        # Make sure 'mode:' isn't a sibling of 'dashboards:' under 'lovelace:'
        # (i.e., lovelace key should only contain 'dashboards')
        import yaml
        parsed = yaml.safe_load(text)
        assert set(parsed["lovelace"].keys()) == {"dashboards"}

    def test_rejects_filename_traversal(self, tmp_path, hass, call_factory):
        from custom_components.ha_mcp_tools import _build_edit_yaml_config_handler

        cfg = Path(tmp_path) / "configuration.yaml"
        cfg.write_text("")

        handler = _build_edit_yaml_config_handler(hass)
        result = self._run(
            handler(
                call_factory(
                    {
                        "file": "configuration.yaml",
                        "action": "add",
                        "yaml_path": "lovelace.dashboards.bad-dash",
                        "content": (
                            "mode: yaml\n"
                            "title: Bad\n"
                            "filename: ../secrets.yaml\n"
                        ),
                        "backup": False,
                    }
                )
            )
        )
        assert result["success"] is False
        assert "filename" in result["error"]

    def test_rejects_reserved_url_path(self, tmp_path, hass, call_factory):
        from custom_components.ha_mcp_tools import _build_edit_yaml_config_handler

        cfg = Path(tmp_path) / "configuration.yaml"
        cfg.write_text("")

        handler = _build_edit_yaml_config_handler(hass)
        result = self._run(
            handler(
                call_factory(
                    {
                        "file": "configuration.yaml",
                        "action": "add",
                        "yaml_path": "lovelace.dashboards.lovelace",
                        "content": "mode: yaml\ntitle: x\nfilename: dashboards/x.yaml\n",
                        "backup": False,
                    }
                )
            )
        )
        assert result["success"] is False
        assert "reserved" in result["error"].lower()

    def test_remove_dashboard_entry_only(self, tmp_path, hass, call_factory):
        """`remove` only deletes the targeted dashboard, not the whole lovelace key."""
        from custom_components.ha_mcp_tools import _build_edit_yaml_config_handler

        cfg = Path(tmp_path) / "configuration.yaml"
        cfg.write_text(
            "lovelace:\n"
            "  dashboards:\n"
            "    energy-dash:\n"
            "      mode: yaml\n"
            "      title: Energy\n"
            "      filename: dashboards/energy.yaml\n"
            "    weather-dash:\n"
            "      mode: yaml\n"
            "      title: Weather\n"
            "      filename: dashboards/weather.yaml\n"
        )

        handler = _build_edit_yaml_config_handler(hass)
        result = self._run(
            handler(
                call_factory(
                    {
                        "file": "configuration.yaml",
                        "action": "remove",
                        "yaml_path": "lovelace.dashboards.energy-dash",
                        "backup": False,
                    }
                )
            )
        )
        assert result["success"] is True
        import yaml
        parsed = yaml.safe_load(cfg.read_text())
        assert "energy-dash" not in parsed["lovelace"]["dashboards"]
        assert "weather-dash" in parsed["lovelace"]["dashboards"]

    def test_rejects_missing_filename_key(self, tmp_path, hass, call_factory):
        """add/replace without `filename:` should be rejected by the validator."""
        from custom_components.ha_mcp_tools import _build_edit_yaml_config_handler

        cfg = Path(tmp_path) / "configuration.yaml"
        cfg.write_text("")

        handler = _build_edit_yaml_config_handler(hass)
        result = self._run(
            handler(
                call_factory(
                    {
                        "file": "configuration.yaml",
                        "action": "add",
                        "yaml_path": "lovelace.dashboards.no-filename",
                        "content": "mode: yaml\ntitle: x\n",  # filename omitted
                        "backup": False,
                    }
                )
            )
        )
        assert result["success"] is False
        assert "filename" in result["error"]

    def test_add_merges_into_existing_dashboard_entry(
        self, tmp_path, hass, call_factory
    ):
        """add into an existing url_path merges (dict.update) rather than replaces."""
        from custom_components.ha_mcp_tools import _build_edit_yaml_config_handler

        cfg = Path(tmp_path) / "configuration.yaml"
        cfg.write_text(
            "lovelace:\n"
            "  dashboards:\n"
            "    energy-dash:\n"
            "      mode: yaml\n"
            "      title: Old\n"
            "      filename: dashboards/energy.yaml\n"
        )

        handler = _build_edit_yaml_config_handler(hass)
        result = self._run(
            handler(
                call_factory(
                    {
                        "file": "configuration.yaml",
                        "action": "add",
                        "yaml_path": "lovelace.dashboards.energy-dash",
                        "content": (
                            "title: New\n"
                            "filename: dashboards/energy.yaml\n"
                            "show_in_sidebar: true\n"
                        ),
                        "backup": False,
                    }
                )
            )
        )
        assert result["success"] is True, result
        import yaml
        parsed = yaml.safe_load(cfg.read_text())
        entry = parsed["lovelace"]["dashboards"]["energy-dash"]
        # Old keys retained, overlapping keys overwritten, new keys added
        assert entry["mode"] == "yaml"
        assert entry["title"] == "New"
        assert entry["show_in_sidebar"] is True


class TestHandleEditYamlConfigSingleKey:
    """Single-key branch of _build_edit_yaml_config_handler must behave the same
    after the factory refactor (regression guard for issue #1034)."""

    @pytest.fixture
    def hass(self, tmp_path):
        h = MM()
        h.config = MM()
        h.config.config_dir = str(tmp_path)
        async def _run(fn, *args):
            return fn(*args)
        h.async_add_executor_job = AsyncMock(side_effect=_run)
        h.services = MM()
        h.services.async_call = AsyncMock(return_value={"errors": None})
        return h

    @pytest.fixture
    def call_factory(self):
        def _make(data):
            call = MM()
            call.data = data
            return call
        return _make

    def _run(self, coro):
        return asyncio.run(coro)

    def test_single_key_add_creates_new_key(self, tmp_path, hass, call_factory):
        from custom_components.ha_mcp_tools import _build_edit_yaml_config_handler

        cfg = Path(tmp_path) / "configuration.yaml"
        cfg.write_text("default_config:\n")

        handler = _build_edit_yaml_config_handler(hass)
        result = self._run(
            handler(
                call_factory(
                    {
                        "file": "configuration.yaml",
                        "action": "add",
                        "yaml_path": "command_line",
                        "content": "- sensor:\n    name: foo\n    command: 'echo 1'\n",
                        "backup": False,
                    }
                )
            )
        )
        assert result["success"] is True, result
        import yaml
        parsed = yaml.safe_load(cfg.read_text())
        assert "command_line" in parsed
        assert parsed["default_config"] is None

    def test_single_key_replace_overwrites(self, tmp_path, hass, call_factory):
        from custom_components.ha_mcp_tools import _build_edit_yaml_config_handler

        cfg = Path(tmp_path) / "configuration.yaml"
        cfg.write_text(
            "shell_command:\n"
            "  old_cmd: 'echo old'\n"
        )

        handler = _build_edit_yaml_config_handler(hass)
        result = self._run(
            handler(
                call_factory(
                    {
                        "file": "configuration.yaml",
                        "action": "replace",
                        "yaml_path": "shell_command",
                        "content": "new_cmd: 'echo new'\n",
                        "backup": False,
                    }
                )
            )
        )
        assert result["success"] is True, result
        import yaml
        parsed = yaml.safe_load(cfg.read_text())
        assert parsed["shell_command"] == {"new_cmd": "echo new"}

    def test_single_key_remove_missing_errors(self, tmp_path, hass, call_factory):
        from custom_components.ha_mcp_tools import _build_edit_yaml_config_handler

        cfg = Path(tmp_path) / "configuration.yaml"
        cfg.write_text("default_config:\n")

        handler = _build_edit_yaml_config_handler(hass)
        result = self._run(
            handler(
                call_factory(
                    {
                        "file": "configuration.yaml",
                        "action": "remove",
                        "yaml_path": "command_line",
                        "backup": False,
                    }
                )
            )
        )
        assert result["success"] is False
        assert "command_line" in result["error"]

    def test_single_key_post_action_for_template(
        self, tmp_path, hass, call_factory
    ):
        """template -> reload_available; covers post-action lookup for single-key kind."""
        from custom_components.ha_mcp_tools import _build_edit_yaml_config_handler

        cfg = Path(tmp_path) / "configuration.yaml"
        cfg.write_text("")

        handler = _build_edit_yaml_config_handler(hass)
        result = self._run(
            handler(
                call_factory(
                    {
                        "file": "configuration.yaml",
                        "action": "add",
                        "yaml_path": "template",
                        "content": (
                            "- sensor:\n"
                            "    - name: t\n"
                            "      state: 'ok'\n"
                        ),
                        "backup": False,
                    }
                )
            )
        )
        assert result["success"] is True, result
        assert result["post_action"] == "reload_available"
        assert result["reload_service"] == "homeassistant.reload_custom_templates"

    def test_single_key_post_action_for_shell_command(
        self, tmp_path, hass, call_factory
    ):
        """shell_command falls through to default 'restart_required'."""
        from custom_components.ha_mcp_tools import _build_edit_yaml_config_handler

        cfg = Path(tmp_path) / "configuration.yaml"
        cfg.write_text("")

        handler = _build_edit_yaml_config_handler(hass)
        result = self._run(
            handler(
                call_factory(
                    {
                        "file": "configuration.yaml",
                        "action": "add",
                        "yaml_path": "shell_command",
                        "content": "echo: 'echo hi'\n",
                        "backup": False,
                    }
                )
            )
        )
        assert result["success"] is True, result
        assert result["post_action"] == "restart_required"
