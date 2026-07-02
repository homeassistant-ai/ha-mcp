"""Unit tests for the whole-file ``replace_file`` action of edit_yaml_config.

``replace_file`` restores a pre-#1579 ``.bak`` wholesale: it bypasses the
per-key merge and writes the supplied content verbatim, while reusing the same
path allowlist, YAML validation, atomic write, and config check as a normal
edit (#1579).
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from unittest.mock import MagicMock as MM

import pytest

# Mock HA imports before importing the module
sys.modules["voluptuous"] = MagicMock()
sys.modules["homeassistant"] = MagicMock()
sys.modules["homeassistant.components"] = MagicMock()
sys.modules["homeassistant.components.persistent_notification"] = MagicMock()
sys.modules["homeassistant.config"] = MagicMock()
sys.modules["homeassistant.config_entries"] = MagicMock()
sys.modules["homeassistant.core"] = MagicMock()
sys.modules["homeassistant.helpers"] = MagicMock()
sys.modules["homeassistant.helpers.config_validation"] = MagicMock()
sys.modules["homeassistant.helpers.storage"] = MagicMock()
sys.modules["homeassistant.loader"] = MagicMock()

from custom_components.ha_mcp_tools import (  # noqa: E402
    CALLER_TOKEN_FIELD,
    _build_edit_yaml_config_handler,
)
from custom_components.ha_mcp_tools.const import DOMAIN  # noqa: E402

_TEST_CALLER_TOKEN = "test-caller-token-whole-file-replace"


@pytest.fixture(autouse=True)
def _stub_config_check(monkeypatch):
    """Default: the post-write config check passes (valid config).

    The component validates via ``async_check_ha_config_file``; stub it so the
    handler's config-check step is deterministic in unit tests. Tests that need
    a failing check override this locally.
    """
    monkeypatch.setattr(
        "custom_components.ha_mcp_tools.async_check_ha_config_file",
        AsyncMock(return_value=None),
        raising=False,
    )


class TestReplaceFileAction:
    """handle_edit_yaml_config(action="replace_file") whole-file restore."""

    @pytest.fixture
    def hass(self, tmp_path):
        """Minimal hass mock that runs executor jobs synchronously."""
        h = MM()
        h.config = MM()
        h.config.config_dir = str(tmp_path)
        h.data = {DOMAIN: {"caller_token": _TEST_CALLER_TOKEN}}

        async def _run(fn, *args):
            return fn(*args)

        h.async_add_executor_job = AsyncMock(side_effect=_run)
        h.services = MM()
        h.services.async_call = AsyncMock(return_value=None)
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

    def test_replaces_whole_configuration_yaml_verbatim(
        self, tmp_path, hass, call_factory
    ):
        """The entire file is overwritten with the supplied content byte-for-byte,
        comments preserved — keys absent from the new content are gone."""
        cfg = Path(tmp_path) / "configuration.yaml"
        cfg.write_text("default_config:\nold_key: 1\n")

        new_content = "# restored\ndefault_config:\ntemplate:\n  - sensor: []\n"
        handler = _build_edit_yaml_config_handler(hass)
        result = self._run(
            handler(
                call_factory(
                    {
                        "file": "configuration.yaml",
                        "action": "replace_file",
                        "yaml_path": "",
                        "content": new_content,
                    }
                )
            )
        )

        assert result["success"] is True, result
        assert result["action"] == "replace_file"
        # ``written`` is the shared write-path discriminator (never a preview).
        assert result["written"] is True
        assert result["post_action"] == "restart_required"
        assert result["config_check"] == "ok"
        # Verbatim — comment kept, old_key dropped.
        assert cfg.read_text() == new_content
        assert "old_key" not in cfg.read_text()

    def test_writes_packages_file(self, tmp_path, hass, call_factory):
        """packages/*.yaml is in the write allowlist and restorable."""
        pkg = Path(tmp_path) / "packages" / "lights.yaml"
        pkg.parent.mkdir(parents=True)
        pkg.write_text("light: []\n")

        handler = _build_edit_yaml_config_handler(hass)
        result = self._run(
            handler(
                call_factory(
                    {
                        "file": "packages/lights.yaml",
                        "action": "replace_file",
                        "yaml_path": "",
                        "content": "switch: []\n",
                    }
                )
            )
        )

        assert result["success"] is True, result
        assert pkg.read_text() == "switch: []\n"

    def test_accepts_include_dir_merge_named_tag(self, tmp_path, hass, call_factory):
        """The documented ``!include_dir_merge_named`` tag must validate and
        restore through replace_file. Regression guard for the tag missing
        from the YAML loader registry, which made ``make_yaml().load()`` raise
        ``ConstructorError`` (surfaced as "Invalid YAML content")."""
        cfg = Path(tmp_path) / "configuration.yaml"
        cfg.write_text("default_config:\n")

        new_content = (
            "default_config:\nfrontend:\n  themes: !include_dir_merge_named themes/\n"
        )
        handler = _build_edit_yaml_config_handler(hass)
        result = self._run(
            handler(
                call_factory(
                    {
                        "file": "configuration.yaml",
                        "action": "replace_file",
                        "yaml_path": "",
                        "content": new_content,
                    }
                )
            )
        )

        assert result["success"] is True, result
        # The tag survived verbatim in the restored file.
        assert "!include_dir_merge_named themes/" in cfg.read_text()

    def test_requires_content(self, hass, call_factory):
        handler = _build_edit_yaml_config_handler(hass)
        result = self._run(
            handler(
                call_factory(
                    {
                        "file": "configuration.yaml",
                        "action": "replace_file",
                        "yaml_path": "",
                    }
                )
            )
        )
        assert result["success"] is False
        assert "content" in result["error"]

    def test_rejects_non_mapping_root(self, hass, call_factory):
        """A list/scalar root is not a valid config file."""
        handler = _build_edit_yaml_config_handler(hass)
        result = self._run(
            handler(
                call_factory(
                    {
                        "file": "configuration.yaml",
                        "action": "replace_file",
                        "yaml_path": "",
                        "content": "- a\n- b\n",
                    }
                )
            )
        )
        assert result["success"] is False
        assert "mapping" in result["error"]

    def test_rejects_invalid_yaml(self, hass, call_factory):
        handler = _build_edit_yaml_config_handler(hass)
        result = self._run(
            handler(
                call_factory(
                    {
                        "file": "configuration.yaml",
                        "action": "replace_file",
                        "yaml_path": "",
                        "content": "a: [unterminated\n",
                    }
                )
            )
        )
        assert result["success"] is False
        assert "Invalid YAML" in result["error"]

    def test_rejects_disallowed_path(self, hass, call_factory):
        """Whole-file replace is confined to the same allowlist as a normal edit
        (configuration.yaml / packages/*.yaml / themes/*.yaml) — www/ is out."""
        handler = _build_edit_yaml_config_handler(hass)
        result = self._run(
            handler(
                call_factory(
                    {
                        "file": "www/sneaky.yaml",
                        "action": "replace_file",
                        "yaml_path": "",
                        "content": "a: 1\n",
                    }
                )
            )
        )
        assert result["success"] is False
        assert "not allowed" in result["error"]

    def test_rejects_path_traversal(self, hass, call_factory):
        handler = _build_edit_yaml_config_handler(hass)
        result = self._run(
            handler(
                call_factory(
                    {
                        "file": "../secrets.yaml",
                        "action": "replace_file",
                        "yaml_path": "",
                        "content": "a: 1\n",
                    }
                )
            )
        )
        assert result["success"] is False
        assert "traversal" in result["error"].lower()

    def test_unauthorized_without_token(self, tmp_path, hass):
        """No caller token → structured unauthorized, no write."""
        cfg = Path(tmp_path) / "configuration.yaml"
        cfg.write_text("default_config:\n")

        handler = _build_edit_yaml_config_handler(hass)
        bad_call = MM()
        bad_call.data = {
            "file": "configuration.yaml",
            "action": "replace_file",
            "yaml_path": "",
            "content": "template: []\n",
        }
        result = self._run(handler(bad_call))

        assert result["success"] is False
        assert result.get("error_code") == "unauthorized"
        # File untouched.
        assert cfg.read_text() == "default_config:\n"

    def test_surfaces_config_check_errors(
        self, tmp_path, hass, call_factory, monkeypatch
    ):
        """A failing config check is reported but the (already atomic) write stands."""
        monkeypatch.setattr(
            "custom_components.ha_mcp_tools.async_check_ha_config_file",
            AsyncMock(return_value="boom: bad config"),
        )
        cfg = Path(tmp_path) / "configuration.yaml"
        cfg.write_text("default_config:\n")

        handler = _build_edit_yaml_config_handler(hass)
        result = self._run(
            handler(
                call_factory(
                    {
                        "file": "configuration.yaml",
                        "action": "replace_file",
                        "yaml_path": "",
                        "content": "template: []\n",
                    }
                )
            )
        )

        assert result["success"] is True
        assert result["config_check"] == "errors"
        assert result["config_check_errors"] == "boom: bad config"

    def test_surfaces_config_check_unavailable_on_failure(
        self, tmp_path, hass, call_factory, monkeypatch
    ):
        """A config check that cannot run surfaces as "unavailable" while the
        (already atomic) write still stands. Behavioral guard for the #1660
        locus — the swallowed-exception branch that the source-string guard
        below cannot catch."""
        monkeypatch.setattr(
            "custom_components.ha_mcp_tools.async_check_ha_config_file",
            AsyncMock(side_effect=RuntimeError("check boom")),
        )
        cfg = Path(tmp_path) / "configuration.yaml"
        cfg.write_text("default_config:\n")

        handler = _build_edit_yaml_config_handler(hass)
        result = self._run(
            handler(
                call_factory(
                    {
                        "file": "configuration.yaml",
                        "action": "replace_file",
                        "yaml_path": "",
                        "content": "template: []\n",
                    }
                )
            )
        )

        assert result["success"] is True
        assert result["config_check"] == "unavailable", result
        assert "check boom" in result.get("config_check_error", ""), result


def test_run_config_check_does_not_misuse_return_response():
    """Regression for #1660: the post-write config check must validate via
    ``async_check_ha_config_file``, not the ``homeassistant.check_config``
    service called with ``return_response=True`` (that service is
    ``SupportsResponse.NONE`` and signals errors by raising, so the
    response-based call always failed and the check never ran).
    """
    import inspect

    from custom_components.ha_mcp_tools import _run_config_check

    src = inspect.getsource(_run_config_check)
    assert "return_response" not in src, src
    assert "async_check_ha_config_file" in src, src
