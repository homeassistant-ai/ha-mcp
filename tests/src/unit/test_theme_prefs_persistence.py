"""Unit tests for theme preferences persistence.

Tests the backend helpers (_sanitize_theme_prefs, _load_theme_prefs,
_render_settings_html) and the GET/POST handlers for /api/settings/theme.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from _pytest.monkeypatch import MonkeyPatch

from ha_mcp.settings_ui import (
    _load_theme_prefs,
    _render_settings_html,
    _sanitize_theme_prefs,
    build_settings_handlers,
)


@pytest.fixture(autouse=True)
def _reset_data_dir_cache() -> Generator[None]:
    """Clear the shared resolved-dir cache between tests."""
    from ha_mcp.utils.data_paths import get_data_dir

    get_data_dir.cache_clear()
    yield
    get_data_dir.cache_clear()


class TestSanitizeThemePrefs:
    """Test ``_sanitize_theme_prefs`` validation and sanitization."""

    def test_non_dict_returns_none(self) -> None:
        assert _sanitize_theme_prefs([1, 2, 3]) is None
        assert _sanitize_theme_prefs("string") is None
        assert _sanitize_theme_prefs(42) is None
        assert _sanitize_theme_prefs(None) is None

    def test_unknown_keys_dropped(self) -> None:
        raw = {"theme": "light", "unknown_key": "value", "another": 123}
        result = _sanitize_theme_prefs(raw)
        assert result == {"theme": "light"}

    def test_invalid_enum_values_dropped(self) -> None:
        raw = {
            "theme": "purple",  # Not in allowed values.
            "fontSize": "200",  # Not in allowed values.
            "contrast": "medium",  # Not in allowed values.
            "shade": "dark-gray",  # Not in allowed values.
        }
        result = _sanitize_theme_prefs(raw)
        assert result == {}

    def test_valid_full_set_kept(self) -> None:
        raw = {
            "theme": "light",
            "fontSize": "130",
            "contrast": "high",
            "shade": "paper",
        }
        result = _sanitize_theme_prefs(raw)
        assert result == raw

    def test_custom_with_mixed_valid_invalid_parts(self) -> None:
        raw = {
            "theme": "dark",
            "custom": json.dumps(
                {
                    "bg": "#112233",  # Valid.
                    "text": "not a hex",  # Invalid.
                    "accent": "#abcdef",  # Valid.
                    "unknown": "#ffffff",  # Unknown part.
                }
            ),
        }
        result = _sanitize_theme_prefs(raw)
        assert result is not None
        assert result["theme"] == "dark"
        # Only valid hex parts re-serialized.
        custom_obj = json.loads(result["custom"])
        assert custom_obj == {"bg": "#112233", "accent": "#abcdef"}

    def test_custom_empty_string_kept(self) -> None:
        """Empty string is the cleared-marker and must be kept."""
        raw = {"theme": "light", "custom": ""}
        result = _sanitize_theme_prefs(raw)
        assert result == {"theme": "light", "custom": ""}

    def test_custom_invalid_json_dropped(self) -> None:
        raw = {"theme": "light", "custom": "not valid json {{{"}
        result = _sanitize_theme_prefs(raw)
        # Invalid JSON is silently dropped; theme is kept.
        assert result == {"theme": "light"}


class TestLoadThemePrefs:
    """Test ``_load_theme_prefs`` best-effort loading."""

    def test_missing_file_returns_empty(
        self, monkeypatch: MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr("ha_mcp.settings_ui._theme.get_data_dir", lambda: tmp_path)
        assert _load_theme_prefs() == {}

    def test_corrupt_json_returns_empty(
        self, monkeypatch: MonkeyPatch, tmp_path: Path
    ) -> None:
        path = tmp_path / "theme_prefs.json"
        path.write_text("not valid json {{{")
        monkeypatch.setattr("ha_mcp.settings_ui._theme.get_data_dir", lambda: tmp_path)
        result = _load_theme_prefs()
        assert result == {}

    def test_file_with_out_of_enum_values_sanitized_away(
        self, monkeypatch: MonkeyPatch, tmp_path: Path
    ) -> None:
        path = tmp_path / "theme_prefs.json"
        path.write_text(json.dumps({"theme": "purple", "fontSize": "200"}))
        monkeypatch.setattr("ha_mcp.settings_ui._theme.get_data_dir", lambda: tmp_path)
        result = _load_theme_prefs()
        # Both values are out of enum; sanitized to empty.
        assert result == {}

    def test_valid_prefs_loaded(self, monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
        prefs = {"theme": "light", "fontSize": "130", "contrast": "high"}
        path = tmp_path / "theme_prefs.json"
        path.write_text(json.dumps(prefs))
        monkeypatch.setattr("ha_mcp.settings_ui._theme.get_data_dir", lambda: tmp_path)
        result = _load_theme_prefs()
        assert result == prefs

    def test_dropped_key_warns_once_per_process(
        self,
        monkeypatch: MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A stale/invalid key warns on first load only — the page handler
        calls ``_load_theme_prefs()`` on every view, and a permanently
        invalid file entry must not spam the log per view (#1574 review).
        """
        path = tmp_path / "theme_prefs.json"
        path.write_text(json.dumps({"theme": "light", "legacy_key": "x"}))
        monkeypatch.setattr("ha_mcp.settings_ui._theme.get_data_dir", lambda: tmp_path)
        monkeypatch.setattr(
            "ha_mcp.settings_ui._theme._WARNED_DROPPED_THEME_PREFS", set()
        )
        with caplog.at_level("WARNING", logger="ha_mcp.settings_ui"):
            assert _load_theme_prefs() == {"theme": "light"}
            assert _load_theme_prefs() == {"theme": "light"}
        warnings = [
            r for r in caplog.records if "Ignoring invalid theme pref" in r.getMessage()
        ]
        assert len(warnings) == 1, warnings
        assert "legacy_key" in warnings[0].getMessage()


class TestRenderSettingsHtml:
    """Test ``_render_settings_html`` substitution."""

    def test_placeholder_replaced_with_escaped_json(
        self, monkeypatch: MonkeyPatch, tmp_path: Path
    ) -> None:
        prefs = {"theme": "light", "fontSize": "130"}
        path = tmp_path / "theme_prefs.json"
        path.write_text(json.dumps(prefs))
        monkeypatch.setattr("ha_mcp.settings_ui._theme.get_data_dir", lambda: tmp_path)
        html = _render_settings_html()
        # Placeholder should not survive.
        assert "__HA_MCP_THEME_PREFS__" not in html
        # Escaped JSON should be present (quotes become &quot;).
        assert "&quot;theme&quot;:&quot;light&quot;" in html
        assert "&quot;fontSize&quot;:&quot;130&quot;" in html

    def test_empty_prefs_still_substitutes(
        self, monkeypatch: MonkeyPatch, tmp_path: Path
    ) -> None:
        """When prefs are empty, placeholder is replaced with '{}'."""
        monkeypatch.setattr("ha_mcp.settings_ui._theme.get_data_dir", lambda: tmp_path)
        html = _render_settings_html()
        assert "__HA_MCP_THEME_PREFS__" not in html
        # Empty object is still valid JSON; check for the attribute.
        assert "data-prefs=" in html


class TestThemePrefsHandlers:
    """Test GET and POST handlers for /api/settings/theme."""

    def _make_request(self, body: Any = None) -> MagicMock:
        """Build a request mock."""
        request = MagicMock()
        if body is None:
            request.json = AsyncMock(side_effect=json.JSONDecodeError("empty", "", 0))
        else:
            request.json = AsyncMock(return_value=body)
        return request

    @pytest.mark.asyncio
    async def test_get_returns_persisted_prefs(
        self, monkeypatch: MonkeyPatch, tmp_path: Path
    ) -> None:
        prefs = {"theme": "dark", "contrast": "high"}
        path = tmp_path / "theme_prefs.json"
        path.write_text(json.dumps(prefs))
        monkeypatch.setattr("ha_mcp.settings_ui._theme.get_data_dir", lambda: tmp_path)

        handlers = build_settings_handlers(server=None, is_sidecar=True)
        resp = await handlers["get_theme_prefs"](self._make_request())

        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert body["prefs"] == prefs

    @pytest.mark.asyncio
    async def test_post_non_dict_body_returns_400(
        self, monkeypatch: MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr("ha_mcp.settings_ui._theme.get_data_dir", lambda: tmp_path)
        handlers = build_settings_handlers(server=None, is_sidecar=True)
        resp = await handlers["save_theme_prefs"](self._make_request([1, 2, 3]))
        assert resp.status_code == 400
        body = json.loads(resp.body)
        assert body["success"] is False

    @pytest.mark.asyncio
    async def test_post_no_valid_fields_returns_400(
        self, monkeypatch: MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr("ha_mcp.settings_ui._theme.get_data_dir", lambda: tmp_path)
        handlers = build_settings_handlers(server=None, is_sidecar=True)
        resp = await handlers["save_theme_prefs"](
            self._make_request({"theme": "purple"})  # Invalid enum.
        )
        assert resp.status_code == 400
        body = json.loads(resp.body)
        assert "No valid theme preference fields" in str(body)

    @pytest.mark.asyncio
    async def test_post_merges_into_existing_file(
        self, monkeypatch: MonkeyPatch, tmp_path: Path
    ) -> None:
        """Partial POST merges with existing prefs (RMW under lock)."""
        path = tmp_path / "theme_prefs.json"
        path.write_text(json.dumps({"theme": "dark", "fontSize": "100"}))
        monkeypatch.setattr("ha_mcp.settings_ui._theme.get_data_dir", lambda: tmp_path)

        handlers = build_settings_handlers(server=None, is_sidecar=True)
        # POST only contrast; theme and fontSize should survive.
        resp = await handlers["save_theme_prefs"](
            self._make_request({"contrast": "high"})
        )

        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert body["success"] is True
        assert body["applied"] == {"contrast": "high"}

        # Verify merge happened on disk.
        saved = json.loads(path.read_text())
        assert saved == {"theme": "dark", "fontSize": "100", "contrast": "high"}

    @pytest.mark.asyncio
    async def test_post_then_get_round_trip(
        self, monkeypatch: MonkeyPatch, tmp_path: Path
    ) -> None:
        """POST then GET returns the persisted prefs."""
        monkeypatch.setattr("ha_mcp.settings_ui._theme.get_data_dir", lambda: tmp_path)
        handlers = build_settings_handlers(server=None, is_sidecar=True)

        # POST initial prefs.
        await handlers["save_theme_prefs"](
            self._make_request({"theme": "light", "shade": "paper"})
        )

        # GET them back.
        resp = await handlers["get_theme_prefs"](self._make_request())
        body = json.loads(resp.body)
        assert body["prefs"] == {"theme": "light", "shade": "paper"}

    @pytest.mark.asyncio
    async def test_oserror_on_write_returns_500(
        self, monkeypatch: MonkeyPatch, tmp_path: Path
    ) -> None:
        """OSError during write (e.g. read-only fs) returns 500."""
        read_only_dir = tmp_path / "readonly"
        read_only_dir.mkdir()
        monkeypatch.setattr(
            "ha_mcp.settings_ui._theme.get_data_dir", lambda: read_only_dir
        )

        # Monkeypatch os.replace to raise OSError.
        import os

        def fake_replace(src: Any, dst: Any) -> None:
            raise OSError(30, "Read-only file system")

        monkeypatch.setattr(os, "replace", fake_replace)

        handlers = build_settings_handlers(server=None, is_sidecar=True)
        resp = await handlers["save_theme_prefs"](
            self._make_request({"theme": "light"})
        )

        assert resp.status_code == 500
        body = json.loads(resp.body)
        assert body["success"] is False
