"""Unit tests for theme file editing in ha_mcp_tools."""

import sys
from unittest.mock import AsyncMock, MagicMock

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
from custom_components.ha_mcp_tools.const import DOMAIN  # noqa: E402

_TEST_CALLER_TOKEN = "test-caller-token-yaml-themes"


class TestParseYamlPathThemes:
    """Theme yaml_path validation: single segment without dots."""

    @pytest.fixture(scope="class")
    def parse(self):
        from custom_components.ha_mcp_tools import _parse_and_validate_yaml_path

        return _parse_and_validate_yaml_path

    def test_accepts_simple_theme_name(self, parse):
        kind, parts, err = parse("my-theme", is_theme=True)
        assert err is None
        assert kind == "theme"
        assert parts == ("my-theme",)

    def test_accepts_theme_name_with_numbers(self, parse):
        kind, parts, err = parse("theme2024", is_theme=True)
        assert err is None
        assert kind == "theme"

    def test_rejects_dotted_theme_name(self, parse):
        _, _, err = parse("my.theme", is_theme=True)
        assert err is not None
        assert "cannot contain dots" in err

    def test_rejects_empty_theme_name(self, parse):
        _, _, err = parse("", is_theme=True)
        assert err is not None


class TestThemeEditHandler:
    """Component handler tests for themes/*.yaml editing."""

    @pytest.fixture
    def hass(self, tmp_path):
        """Minimal hass mock that runs executor jobs synchronously."""
        h = MagicMock()
        h.config = MagicMock()
        h.config.config_dir = str(tmp_path)
        h.data = {DOMAIN: {"caller_token": _TEST_CALLER_TOKEN}}

        async def _run(fn, *args):
            return fn(*args)

        h.async_add_executor_job = AsyncMock(side_effect=_run)

        reload_calls = []

        async def fake_service_call(domain, service, data, **kwargs):
            if domain == "frontend" and service == "reload_themes":
                reload_calls.append((domain, service, data))
                return None
            return {"errors": None}

        h.services = MagicMock()
        h.services.async_call = AsyncMock(side_effect=fake_service_call)
        h.reload_calls = reload_calls
        return h

    @pytest.fixture
    def call_factory(self):
        """Build a ServiceCall-like object with the caller token pre-injected."""

        def _make(data):
            call = MagicMock()
            call.data = {**data, CALLER_TOKEN_FIELD: _TEST_CALLER_TOKEN}
            return call

        return _make

    @pytest.fixture
    def handler(self, hass):
        from custom_components.ha_mcp_tools import _build_edit_yaml_config_handler

        return _build_edit_yaml_config_handler(hass)

    async def test_theme_add_creates_file_with_theme_key(
        self, handler, hass, call_factory, tmp_path
    ):
        theme_file = tmp_path / "themes" / "dark-theme.yaml"
        theme_file.parent.mkdir(parents=True, exist_ok=True)

        call = call_factory(
            {
                "file": "themes/dark-theme.yaml",
                "action": "add",
                "yaml_path": "dark-theme",
                "content": "primary-color: '#1976D2'\naccent-color: '#FFC107'",
                "backup": False,
            }
        )

        result = await handler(call)

        assert result["success"] is True
        assert result["action"] == "add"
        assert result["yaml_path"] == "dark-theme"
        assert result["post_action"] == "reload_performed"
        assert result["reload_service"] == "frontend.reload_themes"

        written = theme_file.read_text()
        assert "dark-theme:" in written
        assert "primary-color: '#1976D2'" in written

        assert len(hass.reload_calls) == 1
        assert hass.reload_calls[0] == ("frontend", "reload_themes", {})

    async def test_theme_add_merges_into_existing_theme(
        self, handler, hass, call_factory, tmp_path
    ):
        theme_file = tmp_path / "themes" / "dark-theme.yaml"
        theme_file.parent.mkdir(parents=True, exist_ok=True)
        theme_file.write_text(
            "dark-theme:\n  primary-color: '#1976D2'\n  text-color: '#FFFFFF'"
        )

        call = call_factory(
            {
                "file": "themes/dark-theme.yaml",
                "action": "add",
                "yaml_path": "dark-theme",
                "content": "accent-color: '#FFC107'",
                "backup": False,
            }
        )

        result = await handler(call)

        assert result["success"] is True

        written = theme_file.read_text()
        assert "primary-color: '#1976D2'" in written
        assert "text-color: '#FFFFFF'" in written
        assert "accent-color: '#FFC107'" in written

    async def test_theme_add_type_mismatch_error(
        self, handler, hass, call_factory, tmp_path
    ):
        theme_file = tmp_path / "themes" / "bad-theme.yaml"
        theme_file.parent.mkdir(parents=True, exist_ok=True)
        theme_file.write_text("bad-theme: not-a-dict")

        call = call_factory(
            {
                "file": "themes/bad-theme.yaml",
                "action": "add",
                "yaml_path": "bad-theme",
                "content": "primary-color: '#1976D2'",
                "backup": False,
            }
        )

        result = await handler(call)

        assert result["success"] is False
        assert "Type mismatch" in result["error"]

    async def test_theme_replace_overwrites_content(
        self, handler, hass, call_factory, tmp_path
    ):
        theme_file = tmp_path / "themes" / "light-theme.yaml"
        theme_file.parent.mkdir(parents=True, exist_ok=True)
        theme_file.write_text(
            "light-theme:\n  primary-color: '#000000'\n  old-key: old-value"
        )

        call = call_factory(
            {
                "file": "themes/light-theme.yaml",
                "action": "replace",
                "yaml_path": "light-theme",
                "content": "primary-color: '#FFFFFF'\nnew-key: new-value",
                "backup": False,
            }
        )

        result = await handler(call)

        assert result["success"] is True

        written = theme_file.read_text()
        assert "primary-color: '#FFFFFF'" in written
        assert "new-key: new-value" in written
        assert "old-key" not in written

    async def test_theme_remove_deletes_theme_key(
        self, handler, hass, call_factory, tmp_path
    ):
        theme_file = tmp_path / "themes" / "multi-theme.yaml"
        theme_file.parent.mkdir(parents=True, exist_ok=True)
        theme_file.write_text(
            "theme1:\n  primary-color: '#111111'\ntheme2:\n  primary-color: '#222222'"
        )

        call = call_factory(
            {
                "file": "themes/multi-theme.yaml",
                "action": "remove",
                "yaml_path": "theme1",
                "backup": False,
            }
        )

        result = await handler(call)

        assert result["success"] is True

        written = theme_file.read_text()
        assert "theme1" not in written
        assert "theme2" in written

    async def test_theme_remove_not_found_error(
        self, handler, hass, call_factory, tmp_path
    ):
        theme_file = tmp_path / "themes" / "empty-theme.yaml"
        theme_file.parent.mkdir(parents=True, exist_ok=True)
        theme_file.write_text("other-theme:\n  primary-color: '#000000'")

        call = call_factory(
            {
                "file": "themes/empty-theme.yaml",
                "action": "remove",
                "yaml_path": "nonexistent",
                "backup": False,
            }
        )

        result = await handler(call)

        assert result["success"] is False
        assert "not found" in result["error"]

    async def test_theme_content_must_be_mapping(
        self, handler, hass, call_factory, tmp_path
    ):
        call = call_factory(
            {
                "file": "themes/new-theme.yaml",
                "action": "add",
                "yaml_path": "new-theme",
                "content": "- not\n- a\n- mapping",
                "backup": False,
            }
        )

        result = await handler(call)

        assert result["success"] is False
        assert "must be a YAML mapping" in result["error"]

    async def test_theme_dotted_name_rejected(
        self, handler, hass, call_factory, tmp_path
    ):
        call = call_factory(
            {
                "file": "themes/bad-name.yaml",
                "action": "add",
                "yaml_path": "my.dotted.theme",
                "content": "primary-color: '#000000'",
                "backup": False,
            }
        )

        result = await handler(call)

        assert result["success"] is False
        assert "cannot contain dots" in result["error"]

    async def test_theme_path_traversal_blocked(
        self, handler, hass, call_factory, tmp_path
    ):
        call = call_factory(
            {
                "file": "themes/../secrets.yaml",
                "action": "add",
                "yaml_path": "theme",
                "content": "primary-color: '#000000'",
                "backup": False,
            }
        )

        result = await handler(call)

        assert result["success"] is False
        assert result["success"] is False

    async def test_theme_reload_failure_degrades_gracefully(
        self, handler, tmp_path, call_factory
    ):
        h = MagicMock()
        h.config = MagicMock()
        h.config.config_dir = str(tmp_path)
        h.data = {DOMAIN: {"caller_token": _TEST_CALLER_TOKEN}}

        async def _run(fn, *args):
            return fn(*args)

        h.async_add_executor_job = AsyncMock(side_effect=_run)

        async def failing_service_call(domain, service, data, **kwargs):
            if domain == "frontend" and service == "reload_themes":
                raise RuntimeError("Reload service unavailable")
            return {"errors": None}

        h.services = MagicMock()
        h.services.async_call = AsyncMock(side_effect=failing_service_call)

        from custom_components.ha_mcp_tools import _build_edit_yaml_config_handler

        handler = _build_edit_yaml_config_handler(h)

        theme_file = tmp_path / "themes" / "test-theme.yaml"
        theme_file.parent.mkdir(parents=True, exist_ok=True)

        call = call_factory(
            {
                "file": "themes/test-theme.yaml",
                "action": "add",
                "yaml_path": "test-theme",
                "content": "primary-color: '#000000'",
                "backup": False,
            }
        )

        result = await handler(call)

        assert result["success"] is True
        assert result["post_action"] == "reload_available"
        assert result["reload_service"] == "frontend.reload_themes"
        assert "reload_error" in result
        assert "unavailable" in result["reload_error"]

    async def test_theme_nested_directory_supported(
        self, handler, hass, call_factory, tmp_path
    ):
        theme_file = tmp_path / "themes" / "custom" / "nested-theme.yaml"
        theme_file.parent.mkdir(parents=True, exist_ok=True)

        call = call_factory(
            {
                "file": "themes/custom/nested-theme.yaml",
                "action": "add",
                "yaml_path": "nested-theme",
                "content": "primary-color: '#123456'",
                "backup": False,
            }
        )

        result = await handler(call)

        assert result["success"] is True
        assert theme_file.exists()

    async def test_configuration_yaml_still_works(
        self, handler, hass, call_factory, tmp_path
    ):
        config_file = tmp_path / "configuration.yaml"
        config_file.write_text("homeassistant:\n  name: Home")

        call = call_factory(
            {
                "file": "configuration.yaml",
                "action": "add",
                "yaml_path": "template",
                "content": "- sensor:\n    - name: Test\n      state: 'on'",
                "backup": False,
            }
        )

        result = await handler(call)

        assert result["success"] is True

    async def test_packages_yaml_still_works(
        self, handler, hass, call_factory, tmp_path
    ):
        package_file = tmp_path / "packages" / "test.yaml"
        package_file.parent.mkdir(parents=True, exist_ok=True)

        call = call_factory(
            {
                "file": "packages/test.yaml",
                "action": "add",
                "yaml_path": "automation",
                "content": "- id: test\n  alias: Test\n  trigger: []",
                "backup": False,
            }
        )

        result = await handler(call)

        assert result["success"] is True
