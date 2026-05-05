"""Unit tests for the settings UI config persistence and tool visibility."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse

from ha_mcp.settings_ui import (
    FEATURE_GATED_TOOLS,
    MANDATORY_TOOLS,
    _get_config_path,
    apply_tool_visibility,
    load_tool_config,
    register_settings_routes,
    save_tool_config,
)

SaveHandler = Callable[[Request], Awaitable[JSONResponse]]


class TestConfigPersistence:
    """Test load/save of tool_config.json."""

    def test_save_and_load(self, tmp_path: Path):
        config = {"tools": {"ha_hacs_info": "disabled", "ha_restart": "pinned"}}
        config_path = tmp_path / "tool_config.json"
        with patch("ha_mcp.settings_ui._get_config_path", return_value=config_path):
            save_tool_config(config)
            loaded = load_tool_config()
        assert loaded == config

    def test_load_missing_file(self, tmp_path: Path):
        config_path = tmp_path / "nonexistent.json"
        with patch("ha_mcp.settings_ui._get_config_path", return_value=config_path):
            assert load_tool_config() == {}

    def test_load_corrupt_file(self, tmp_path: Path):
        config_path = tmp_path / "corrupt.json"
        config_path.write_text("not json {{{")
        with patch("ha_mcp.settings_ui._get_config_path", return_value=config_path):
            assert load_tool_config() == {}

    def test_seed_from_env_vars(self, tmp_path: Path):
        config_path = tmp_path / "tool_config.json"
        settings = MagicMock()
        settings.disabled_tools = "ha_hacs_info,ha_hacs_download"
        settings.pinned_tools = "ha_restart"
        with patch("ha_mcp.settings_ui._get_config_path", return_value=config_path):
            config = load_tool_config(settings)
        assert config["tools"]["ha_hacs_info"] == "disabled"
        assert config["tools"]["ha_hacs_download"] == "disabled"
        assert config["tools"]["ha_restart"] == "pinned"
        assert config_path.exists()


class TestApplyToolVisibility:
    """Test apply_tool_visibility logic."""

    def test_disables_tools(self):
        mcp = MagicMock()
        settings = MagicMock()
        settings.enable_yaml_config_editing = True
        config = {"tools": {"ha_hacs_info": "disabled", "ha_restart": "enabled"}}
        apply_tool_visibility(mcp, config, settings)
        mcp.disable.assert_called_once()
        disabled_names = mcp.disable.call_args[1]["names"]
        assert "ha_hacs_info" in disabled_names
        assert "ha_restart" not in disabled_names

    def test_mandatory_tools_not_disabled(self):
        mcp = MagicMock()
        settings = MagicMock()
        settings.enable_yaml_config_editing = True
        config = {"tools": dict.fromkeys(MANDATORY_TOOLS, "disabled")}
        apply_tool_visibility(mcp, config, settings)
        if mcp.disable.called:
            disabled_names = mcp.disable.call_args[1]["names"]
            for name in MANDATORY_TOOLS:
                assert name not in disabled_names

    def test_yaml_editing_off_disables_tool(self):
        mcp = MagicMock()
        settings = MagicMock()
        settings.enable_yaml_config_editing = False
        config = {"tools": {}}
        apply_tool_visibility(mcp, config, settings)
        mcp.disable.assert_called_once()
        disabled_names = mcp.disable.call_args[1]["names"]
        assert "ha_config_set_yaml" in disabled_names

    def test_yaml_editing_on_does_not_disable_tool(self):
        mcp = MagicMock()
        settings = MagicMock()
        settings.enable_yaml_config_editing = True
        config = {"tools": {}}
        apply_tool_visibility(mcp, config, settings)
        if mcp.disable.called:
            disabled_names = mcp.disable.call_args[1]["names"]
            assert "ha_config_set_yaml" not in disabled_names

    def test_yaml_editing_on_but_ui_disabled_keeps_tool_disabled(self):
        # AND semantics: even when the safety toggle is on, a UI-saved
        # "disabled" state must be respected. (Regression guard for
        # Patch76 G9.2 — the previous behavior force-enabled the tool
        # whenever the safety toggle was on, overriding the UI choice.)
        mcp = MagicMock()
        settings = MagicMock()
        settings.enable_yaml_config_editing = True
        config = {"tools": {"ha_config_set_yaml": "disabled"}}
        apply_tool_visibility(mcp, config, settings)
        mcp.disable.assert_called_once()
        disabled_names = mcp.disable.call_args[1]["names"]
        assert "ha_config_set_yaml" in disabled_names

    def test_returns_pinned_names(self):
        mcp = MagicMock()
        settings = MagicMock()
        settings.enable_yaml_config_editing = True
        config = {"tools": {"ha_restart": "pinned", "ha_hacs_info": "enabled"}}
        pinned = apply_tool_visibility(mcp, config, settings)
        assert "ha_restart" in pinned
        assert "ha_hacs_info" not in pinned

    def test_empty_config_no_disable(self):
        mcp = MagicMock()
        settings = MagicMock()
        settings.enable_yaml_config_editing = True
        config = {}
        apply_tool_visibility(mcp, config, settings)
        mcp.disable.assert_not_called()


@pytest.fixture(autouse=True)
def _reset_data_dir_cache():
    """Clear the shared resolved-dir cache between tests."""
    from ha_mcp.utils.data_paths import get_data_dir

    get_data_dir.cache_clear()
    yield
    get_data_dir.cache_clear()


class TestConfigPath:
    """Thin wrapper around utils.data_paths.get_data_dir; full priority
    order is tested in tests/src/unit/test_data_paths.py.
    """

    def test_returns_data_dir_plus_filename(self, monkeypatch, tmp_path):
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.delenv("HA_MCP_CONFIG_DIR", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert _get_config_path() == tmp_path / ".ha-mcp" / "tool_config.json"

    def test_load_tool_config_does_not_crash_on_unreadable_config_dir(
        self, monkeypatch, tmp_path
    ):
        """Regression for #1125 + the same-class follow-up bug.

        When the resolved path's parent isn't traversable by the runtime
        UID (e.g. ``HA_MCP_CONFIG_DIR`` pointing at an existing 0700 dir
        owned by another user), ``Path.exists()`` would raise
        ``PermissionError`` because ``EACCES`` is not in
        ``pathlib._IGNORED_ERRNOS``. ``load_tool_config()`` must treat it
        as "no config yet" instead of crashing.
        """
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.delenv("HA_MCP_CONFIG_DIR", raising=False)
        unreadable_dir = tmp_path / "unreadable"
        unreadable_dir.mkdir()
        cfg_path = unreadable_dir / "tool_config.json"
        monkeypatch.setattr("ha_mcp.settings_ui._get_config_path", lambda: cfg_path)

        original_read = Path.read_text

        def fake_read_text(self: Path, *args, **kwargs):
            if self == cfg_path:
                raise PermissionError(13, "Permission denied")
            return original_read(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", fake_read_text)

        # Must not raise.
        assert load_tool_config() == {}

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="chmod 0o000 doesn't model POSIX EACCES on Windows",
    )
    def test_load_tool_config_handles_real_eacces_on_posix(
        self, monkeypatch, tmp_path
    ):
        """End-to-end variant of the EACCES regression: a real 0o000 dir.

        The mocked-``read_text`` test above pins the going-forward contract,
        but a future maintainer who reintroduces an upstream ``Path.exists()``
        check would not be caught by it. This test exercises the actual
        permission boundary: ``read_text`` on a file under a 0o000 dir
        raises ``PermissionError`` (errno EACCES) from the kernel.
        """
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.delenv("HA_MCP_CONFIG_DIR", raising=False)
        locked_dir = tmp_path / "locked"
        locked_dir.mkdir()
        cfg_path = locked_dir / "tool_config.json"
        cfg_path.write_text("{}")
        monkeypatch.setattr("ha_mcp.settings_ui._get_config_path", lambda: cfg_path)
        os.chmod(locked_dir, 0o000)
        try:
            assert load_tool_config() == {}
        finally:
            os.chmod(locked_dir, 0o755)  # let pytest clean up tmp_path


class TestSaveToolConfig:
    """Tests for the bool return contract added so the HTTP route can
    surface failures to the UI instead of lying that the save succeeded."""

    def test_returns_true_on_success(self, tmp_path):
        cfg_path = tmp_path / "tool_config.json"
        with patch("ha_mcp.settings_ui._get_config_path", return_value=cfg_path):
            assert save_tool_config({"tools": {"x": "disabled"}}) is True
        assert cfg_path.exists()

    def test_returns_false_on_oserror(self, monkeypatch, tmp_path):
        cfg_path = tmp_path / "tool_config.json"
        monkeypatch.setattr("ha_mcp.settings_ui._get_config_path", lambda: cfg_path)

        def fake_write_text(self: Path, *args, **kwargs):
            if self == cfg_path:
                raise OSError(30, "Read-only file system")
            return Path.write_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", fake_write_text)
        assert save_tool_config({"tools": {"x": "disabled"}}) is False


class TestFeatureGatedTools:
    """Test the FEATURE_GATED_TOOLS dict aligns with the beta tag system."""

    def test_install_mcp_tools_is_gated(self):
        # Patch76 G7: ha_install_mcp_tools must appear as a stub when its
        # feature flag is off; otherwise users have no way to discover the
        # tool exists.
        assert "ha_install_mcp_tools" in FEATURE_GATED_TOOLS
        assert FEATURE_GATED_TOOLS["ha_install_mcp_tools"]["disabled_by"] == (
            "enable_custom_component_integration"
        )

    def test_filesystem_tools_use_addon_option_name(self):
        # disabled_by should reference the dev addon option name (matches
        # how the JS renders "set <code>{disabled_by}</code> in the dev
        # add-on config or the matching env var (see docs/beta.md)").
        for name in (
            "ha_list_files",
            "ha_read_file",
            "ha_write_file",
            "ha_delete_file",
        ):
            assert FEATURE_GATED_TOOLS[name]["disabled_by"] == "enable_filesystem_tools"


class TestRouteRegistration:
    """Test register_settings_routes mounting under secret_path (Patch76 G1)."""

    def _collect_paths(self, mcp):
        return [call.args[0] for call in mcp.custom_route.call_args_list]

    def test_registers_root_in_addon_mode(self, monkeypatch):
        monkeypatch.setenv("SUPERVISOR_TOKEN", "fake")
        mcp = MagicMock()
        mcp.custom_route = MagicMock(return_value=lambda fn: fn)
        register_settings_routes(mcp, MagicMock(), secret_path="/private_x")
        paths = self._collect_paths(mcp)
        # Root for ingress + secret-prefixed for direct port access
        assert "/" in paths
        assert "/settings" in paths
        assert "/private_x/settings" in paths
        assert "/private_x/api/settings/tools" in paths

    def test_secret_path_only_when_not_addon(self, monkeypatch):
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        mcp = MagicMock()
        mcp.custom_route = MagicMock(return_value=lambda fn: fn)
        register_settings_routes(mcp, MagicMock(), secret_path="/mcp")
        paths = self._collect_paths(mcp)
        # No root mount in Docker/standalone — only the secret-prefixed routes
        assert "/" not in paths
        assert "/settings" not in paths
        assert "/mcp/settings" in paths
        assert "/mcp/api/settings/tools" in paths

    def test_no_routes_when_no_addon_and_no_secret(self, monkeypatch):
        # Refuse to mount publicly: no auth → no routes.
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        mcp = MagicMock()
        mcp.custom_route = MagicMock(return_value=lambda fn: fn)
        register_settings_routes(mcp, MagicMock(), secret_path="")
        assert mcp.custom_route.call_count == 0


class TestSaveToolsValidation:
    """Test POST /api/settings/tools handler validation (Patch76 G3)."""

    def _make_request(self, body):
        request = MagicMock()
        request.json = AsyncMock(return_value=body)
        return request

    def _capture_handler(self, monkeypatch) -> SaveHandler:
        # Capture the _save_tools handler that register_settings_routes
        # mounts so we can call it directly instead of going through HTTP.
        monkeypatch.setenv("SUPERVISOR_TOKEN", "fake")
        captured: dict[str, Any] = {}

        def custom_route_factory(path, methods):
            def decorator(fn):
                if path == "/api/settings/tools" and "POST" in methods:
                    captured["save"] = fn
                return fn

            return decorator

        mcp = MagicMock()
        mcp.custom_route = MagicMock(side_effect=custom_route_factory)
        register_settings_routes(mcp, MagicMock(), secret_path="/x")
        return captured["save"]

    @pytest.mark.asyncio
    async def test_rejects_non_dict_body_array(self, monkeypatch, tmp_path):
        # Patch76 G3: a JSON array body would AttributeError on body.get
        # → 500. Must be a structured 400 instead.
        monkeypatch.setattr(
            "ha_mcp.settings_ui._get_config_path",
            lambda: tmp_path / "tool_config.json",
        )
        save = self._capture_handler(monkeypatch)
        resp = await save(self._make_request([1, 2, 3]))
        assert resp.status_code == 400
        body = json.loads(resp.body)
        assert body["success"] is False

    @pytest.mark.asyncio
    async def test_rejects_non_dict_body_null(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "ha_mcp.settings_ui._get_config_path",
            lambda: tmp_path / "tool_config.json",
        )
        save = self._capture_handler(monkeypatch)
        resp = await save(self._make_request(None))
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_rejects_non_dict_states(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "ha_mcp.settings_ui._get_config_path",
            lambda: tmp_path / "tool_config.json",
        )
        save = self._capture_handler(monkeypatch)
        resp = await save(self._make_request({"states": "not-a-dict"}))
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_drops_garbage_state_values(self, monkeypatch, tmp_path):
        config_path = tmp_path / "tool_config.json"
        monkeypatch.setattr("ha_mcp.settings_ui._get_config_path", lambda: config_path)
        save = self._capture_handler(monkeypatch)
        resp = await save(
            self._make_request(
                {
                    "states": {
                        "ha_good_tool": "disabled",
                        "ha_bad_value": "not_a_real_state",
                        42: "disabled",  # non-string key
                    },
                }
            )
        )
        assert resp.status_code == 200
        saved = json.loads(config_path.read_text())
        assert saved["tools"] == {"ha_good_tool": "disabled"}

    @pytest.mark.asyncio
    async def test_returns_500_when_save_fails(self, monkeypatch, tmp_path):
        """``save_tool_config`` returning False (read-only fs, etc.) must
        surface as a 500 to the UI — otherwise the JS shows "Saved" while
        the change was lost."""
        config_path = tmp_path / "tool_config.json"
        monkeypatch.setattr("ha_mcp.settings_ui._get_config_path", lambda: config_path)
        monkeypatch.setattr("ha_mcp.settings_ui.save_tool_config", lambda _: False)
        save = self._capture_handler(monkeypatch)
        resp = await save(self._make_request({"states": {"ha_good_tool": "disabled"}}))
        assert resp.status_code == 500
        body = json.loads(resp.body)
        assert body["success"] is False
        assert "HA_MCP_CONFIG_DIR" in str(body)
