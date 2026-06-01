"""Unit tests for YAML-mode dashboard validators in ha_mcp_tools."""

import asyncio
import sys
from pathlib import Path
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock
from unittest.mock import MagicMock as MM

import pytest

# Mock HA imports before importing the module
sys.modules["voluptuous"] = MagicMock()
sys.modules["homeassistant"] = MagicMock()
sys.modules["homeassistant.components"] = MagicMock()
sys.modules["homeassistant.components.persistent_notification"] = MagicMock()
sys.modules["homeassistant.config_entries"] = MagicMock()
sys.modules["homeassistant.core"] = MagicMock()
sys.modules["homeassistant.helpers"] = MagicMock()
sys.modules["homeassistant.helpers.config_validation"] = MagicMock()
sys.modules["homeassistant.helpers.storage"] = MagicMock()
sys.modules["homeassistant.loader"] = MagicMock()

from custom_components.ha_mcp_tools import CALLER_TOKEN_FIELD  # noqa: E402
from custom_components.ha_mcp_tools.const import (  # noqa: E402
    DASHBOARD_URL_PATH_PATTERN,
    DOMAIN,
    RESERVED_DASHBOARD_URL_PATHS,
)

# Both handler fixtures below preload this token into hass.data and inject
# it into every call_factory payload so the caller-token gate added by the
# auth PR is transparent to these dashboard tests (which exercise the
# yaml-editing logic, not the auth boundary).
_TEST_CALLER_TOKEN = "test-caller-token-yaml-dashboards"


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
            "",  # empty
            "no_underscore",  # underscores not allowed
            "NoUpper",  # uppercase not allowed
            "single",  # must contain a hyphen
            "-leading-hyphen",  # cannot start with hyphen
            "trailing-hyphen-",  # cannot end with hyphen
            "double--hyphen",  # no consecutive hyphens
            "has space",  # no spaces
            "has/slash",  # no slashes
            "has.dot",  # no dots
            "..",  # path traversal-ish
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
            "dashboards/main.yml",  # wrong extension
            "main.yaml",  # not under dashboards/
            "www/dashboard.yaml",  # other allowlist dir, not dashboards
            "",
            "dashboards/",
            "dashboards/main",  # no extension
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


class TestParseYamlPathPackagesOnly:
    """PACKAGES_ONLY_YAML_KEYS (automation/script/scene) gating.

    Accepted only when is_package=True; in configuration.yaml the rejection
    must point at the storage-mode tools, not just dump the generic
    allowlist.
    """

    @pytest.fixture(scope="class")
    def parse(self):
        from custom_components.ha_mcp_tools import _parse_and_validate_yaml_path

        return _parse_and_validate_yaml_path

    @pytest.mark.parametrize("key", ["automation", "script", "scene"])
    def test_accepts_packages_only_key_when_is_package(self, parse, key):
        kind, parts, err = parse(key, is_package=True)
        assert err is None
        assert kind == "single"
        assert parts == (key,)

    @pytest.mark.parametrize("key", ["automation", "script", "scene"])
    def test_rejects_packages_only_key_in_configuration_yaml(self, parse, key):
        _, _, err = parse(key, is_package=False)
        assert err is not None
        assert "packages/*.yaml" in err
        # Spell each tool name out individually — an agent reading the
        # rejection sees the combined slash-form as one malformed name.
        assert "ha_config_set_automation" in err
        assert "ha_config_set_script" in err
        assert "ha_config_set_scene" in err

    def test_default_is_package_false(self, parse):
        # Omitting is_package must behave like is_package=False — no silent
        # acceptance of PACKAGES_ONLY keys through positional callers.
        _, _, err = parse("automation")
        assert err is not None
        assert "packages/*.yaml" in err

    def test_union_list_in_generic_error_when_is_package(self, parse):
        # Unknown single key inside a package file: error must enumerate both
        # ALLOWED_YAML_KEYS and PACKAGES_ONLY_YAML_KEYS so the user sees the
        # full surface they can pick from.
        _, _, err = parse("frontend", is_package=True)
        assert err is not None
        assert "automation" in err
        assert "script" in err
        assert "scene" in err

    def test_generic_error_excludes_packages_only_when_not_package(self, parse):
        # Unknown single key against configuration.yaml: allowlist must NOT
        # include PACKAGES_ONLY keys — listing them would mislead the user
        # into thinking they can rename their key to one of those.
        _, _, err = parse("frontend", is_package=False)
        assert err is not None
        assert "automation" not in err
        assert "script" not in err
        assert "scene" not in err


class TestPostActionTableContract:
    """PACKAGES_ONLY_YAML_KEYS and YAML_KEY_POST_ACTIONS must stay in sync.

    If a future PR adds a key to PACKAGES_ONLY_YAML_KEYS without a matching
    reload_service entry, callers would see the default restart_required
    response — a regression we should catch at unit-test time.
    """

    def test_packages_only_keys_have_post_actions(self):
        from custom_components.ha_mcp_tools.const import (
            PACKAGES_ONLY_YAML_KEYS,
            YAML_KEY_POST_ACTIONS,
        )

        missing = PACKAGES_ONLY_YAML_KEYS - set(YAML_KEY_POST_ACTIONS)
        assert not missing, (
            f"PACKAGES_ONLY_YAML_KEYS missing YAML_KEY_POST_ACTIONS entries: {missing}"
        )

    def test_packages_only_disjoint_from_allowed(self):
        """ALLOWED_YAML_KEYS and PACKAGES_ONLY_YAML_KEYS must not overlap.

        ``_parse_and_validate_yaml_path`` checks ``ALLOWED_YAML_KEYS``
        first, then the ``is_package and key in PACKAGES_ONLY_YAML_KEYS``
        branch. If a future change accidentally lands a packages-only
        key (``automation`` / ``script`` / ``scene``) into
        ``ALLOWED_YAML_KEYS`` as well, the gating branch becomes dead
        code and that key would silently land in ``configuration.yaml``.
        """
        from custom_components.ha_mcp_tools.const import (
            ALLOWED_YAML_KEYS,
            PACKAGES_ONLY_YAML_KEYS,
        )

        overlap = ALLOWED_YAML_KEYS & PACKAGES_ONLY_YAML_KEYS
        assert not overlap, f"sets must be disjoint; overlap: {overlap}"

    @pytest.mark.parametrize(
        ("key", "expected_service"),
        [
            ("automation", "automation.reload"),
            ("script", "script.reload"),
            ("scene", "scene.reload"),
        ],
    )
    def test_packages_only_reload_services(self, key, expected_service):
        from custom_components.ha_mcp_tools.const import YAML_KEY_POST_ACTIONS

        entry = YAML_KEY_POST_ACTIONS[key]
        assert entry["post_action"] == "reload_available"
        assert entry["reload_service"] == expected_service


class TestHandleEditYamlConfigDashboards:
    """Integration of dashboard branch into handle_edit_yaml_config."""

    @pytest.fixture
    def hass(self, tmp_path):
        """Minimal hass mock that runs executor jobs synchronously."""
        h = MM()
        h.config = MM()
        h.config.config_dir = str(tmp_path)
        # Seed the caller token so _caller_token_ok passes (auth PR).
        h.data = {DOMAIN: {"caller_token": _TEST_CALLER_TOKEN}}

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
        """Build a ServiceCall-like object with the caller token pre-injected."""

        def _make(data):
            call = MM()
            call.data = {**data, CALLER_TOKEN_FIELD: _TEST_CALLER_TOKEN}
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
                            "mode: yaml\ntitle: Bad\nfilename: ../secrets.yaml\n"
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
    after the factory refactor."""

    @pytest.fixture
    def hass(self, tmp_path):
        h = MM()
        h.config = MM()
        h.config.config_dir = str(tmp_path)
        # Seed the caller token so _caller_token_ok passes (auth PR).
        h.data = {DOMAIN: {"caller_token": _TEST_CALLER_TOKEN}}

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
            call.data = {**data, CALLER_TOKEN_FIELD: _TEST_CALLER_TOKEN}
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
        cfg.write_text("shell_command:\n  old_cmd: 'echo old'\n")

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

    def test_single_key_post_action_for_template(self, tmp_path, hass, call_factory):
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
                        "content": ("- sensor:\n    - name: t\n      state: 'ok'\n"),
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


class TestHandleEditYamlConfigPathTraversal:
    """Path-traversal defense composes ``os.path.normpath`` before the
    ``fnmatch`` package check, so a crafted ``packages/../configuration.yaml``
    cannot smuggle a PACKAGES_ONLY key into ``configuration.yaml``."""

    @pytest.fixture
    def hass(self, tmp_path):
        h = MM()
        h.config = MM()
        h.config.config_dir = str(tmp_path)
        h.data = {DOMAIN: {"caller_token": _TEST_CALLER_TOKEN}}

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
            call.data = {**data, CALLER_TOKEN_FIELD: _TEST_CALLER_TOKEN}
            return call

        return _make

    def _run(self, coro):
        return asyncio.run(coro)

    def test_packages_dotdot_normalizes_to_configuration_yaml_and_rejects(
        self, tmp_path, hass, call_factory
    ):
        """``packages/../configuration.yaml`` → ``configuration.yaml`` after
        normpath, so the ``packages/*.yaml`` fnmatch does not match and
        ``yaml_path="automation"`` is rejected with the storage-mode pointer.

        Pins the layering: normpath at __init__.py:330 must run before
        the fnmatch package check at __init__.py:338 so a crafted path
        cannot smuggle a PACKAGES_ONLY key into configuration.yaml. The
        defense is correct by construction; this is belt-and-suspenders
        against a future refactor reordering those two steps."""
        from custom_components.ha_mcp_tools import _build_edit_yaml_config_handler

        cfg = Path(tmp_path) / "configuration.yaml"
        cfg.write_text("")

        handler = _build_edit_yaml_config_handler(hass)
        result = self._run(
            handler(
                call_factory(
                    {
                        "file": "packages/../configuration.yaml",
                        "action": "add",
                        "yaml_path": "automation",
                        "content": "- id: x\n  alias: x\n",
                        "backup": False,
                    }
                )
            )
        )
        assert result["success"] is False
        assert "packages/*.yaml" in result["error"]
        assert "ha_config_set_automation" in result["error"]


class TestHandleEditYamlConfigPackagesGate:
    """Server-side per-key gate: the component independently refuses a
    PACKAGES_ONLY key when the caller passes it in ``disabled_packages_keys``,
    even if the wrapper's client-side gate were bypassed (defense in depth).
    Mirrors the wrapper-side coverage in test_yaml_config_tool.py."""

    @pytest.fixture
    def hass(self, tmp_path):
        h = MM()
        h.config = MM()
        h.config.config_dir = str(tmp_path)
        h.data = {DOMAIN: {"caller_token": _TEST_CALLER_TOKEN}}

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
            call.data = {**data, CALLER_TOKEN_FIELD: _TEST_CALLER_TOKEN}
            return call

        return _make

    def _run(self, coro):
        return asyncio.run(coro)

    # Per-key valid content (automation/scene are lists, script is a map).
    _CONTENT: ClassVar[dict[str, str]] = {
        "automation": "- id: x\n  alias: x\n  trigger: []\n  action: []\n",
        "script": "my_script:\n  sequence: []\n",
        "scene": "- id: x\n  name: x\n  entities: {}\n",
    }

    @pytest.mark.parametrize("key", ["automation", "script", "scene"])
    def test_disabled_key_rejected_server_side(self, hass, call_factory, key):
        """A disabled key targeting packages/*.yaml is refused by the
        component itself — independently of the wrapper — with a message
        pointing back at the caller's runtime configuration."""
        from custom_components.ha_mcp_tools import _build_edit_yaml_config_handler

        handler = _build_edit_yaml_config_handler(hass)
        result = self._run(
            handler(
                call_factory(
                    {
                        "file": f"packages/{key}.yaml",
                        "action": "add",
                        "yaml_path": key,
                        "content": self._CONTENT[key],
                        "backup": False,
                        "disabled_packages_keys": [key],
                    }
                )
            )
        )
        assert result["success"] is False, result
        assert key in result["error"]
        assert "disabled by the caller's runtime configuration" in result["error"]

    def test_other_packages_key_not_blocked(self, hass, call_factory):
        """The gate is per-key: disabling ``automation`` must not block a
        ``script`` write to the same packages surface."""
        from custom_components.ha_mcp_tools import _build_edit_yaml_config_handler

        handler = _build_edit_yaml_config_handler(hass)
        result = self._run(
            handler(
                call_factory(
                    {
                        "file": "packages/things.yaml",
                        "action": "add",
                        "yaml_path": "script",
                        "content": self._CONTENT["script"],
                        "backup": False,
                        "disabled_packages_keys": ["automation"],
                    }
                )
            )
        )
        assert result["success"] is True, result

    def test_configuration_yaml_disabled_key_falls_through(
        self, tmp_path, hass, call_factory
    ):
        """A disabled PACKAGES_ONLY key written to configuration.yaml must
        fall through to the storage-mode advisory (``_parse_and_validate_yaml_path``),
        NOT the per-key gate message — the gate is scoped to packages targets."""
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
                        "yaml_path": "automation",
                        "content": self._CONTENT["automation"],
                        "backup": False,
                        "disabled_packages_keys": ["automation"],
                    }
                )
            )
        )
        assert result["success"] is False, result
        # The storage-mode advisory, not the per-key gate message.
        assert "disabled by the caller's runtime configuration" not in result["error"]
        assert "ha_config_set_automation" in result["error"]

    def test_unknown_disabled_key_is_noop(self, hass, call_factory):
        """An unrecognized key in ``disabled_packages_keys`` is filtered out
        (typo guard), so it cannot accidentally block a real, enabled key."""
        from custom_components.ha_mcp_tools import _build_edit_yaml_config_handler

        handler = _build_edit_yaml_config_handler(hass)
        result = self._run(
            handler(
                call_factory(
                    {
                        "file": "packages/automations.yaml",
                        "action": "add",
                        "yaml_path": "automation",
                        "content": self._CONTENT["automation"],
                        "backup": False,
                        "disabled_packages_keys": ["automatoin"],  # typo
                    }
                )
            )
        )
        assert result["success"] is True, result

    def test_default_empty_disabled_allows(self, hass, call_factory):
        """Omitting ``disabled_packages_keys`` entirely (older wrapper, or the
        schema default) imposes no per-key restriction on a packages write."""
        from custom_components.ha_mcp_tools import _build_edit_yaml_config_handler

        handler = _build_edit_yaml_config_handler(hass)
        result = self._run(
            handler(
                call_factory(
                    {
                        "file": "packages/automations.yaml",
                        "action": "add",
                        "yaml_path": "automation",
                        "content": self._CONTENT["automation"],
                        "backup": False,
                    }
                )
            )
        )
        assert result["success"] is True, result

    def test_flag_map_matches_packages_only_keys(self):
        """The wrapper's ``_YAML_PACKAGES_FLAG_BY_KEY`` must stay in lockstep
        with the component's ``PACKAGES_ONLY_YAML_KEYS`` (no import-time
        coupling exists), and every mapped flag must be a real ``Settings``
        field. Otherwise a packages-only key silently becomes ungated, or
        ``_disabled_packages_keys`` raises at runtime."""
        from custom_components.ha_mcp_tools.const import PACKAGES_ONLY_YAML_KEYS
        from ha_mcp.config import Settings
        from ha_mcp.tools.tools_yaml_config import _YAML_PACKAGES_FLAG_BY_KEY

        assert set(_YAML_PACKAGES_FLAG_BY_KEY) == set(PACKAGES_ONLY_YAML_KEYS)
        for flag in _YAML_PACKAGES_FLAG_BY_KEY.values():
            assert flag in Settings.model_fields, (
                f"{flag} is mapped in _YAML_PACKAGES_FLAG_BY_KEY but is not a "
                "Settings field"
            )
