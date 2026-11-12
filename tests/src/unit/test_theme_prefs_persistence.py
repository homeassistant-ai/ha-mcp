"""Unit tests for theme preferences persistence.

Drives the GET/POST handlers for /api/settings/theme and the served
settings page, which seeds the saved prefs into its ``server-prefs`` script.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp.settings_ui import build_settings_handlers, render_settings_html
from ha_mcp.utils.data_paths import get_data_dir


def _prefs_file() -> Path:
    """Where the handlers keep the prefs: the test's own empty data dir."""
    return get_data_dir() / "theme_prefs.json"


def _request(body: Any = None) -> MagicMock:
    """Build a request mock."""
    request = MagicMock()
    if body is None:
        request.json = AsyncMock(side_effect=json.JSONDecodeError("empty", "", 0))
    else:
        request.json = AsyncMock(return_value=body)
    return request


async def _save(body: Any) -> tuple[int, dict[str, Any]]:
    handlers = build_settings_handlers(server=None, is_sidecar=True)
    resp = await handlers["save_theme_prefs"](_request(body))
    return resp.status_code, json.loads(resp.body)


async def _load() -> dict[str, Any]:
    handlers = build_settings_handlers(server=None, is_sidecar=True)
    resp = await handlers["get_theme_prefs"](_request())
    assert resp.status_code == 200
    return json.loads(resp.body)["prefs"]


class TestSavedPrefsAreSanitized:
    """A POST keeps only the offered keys and values."""

    @pytest.mark.parametrize("body", [[1, 2, 3], "string", 42, None])
    @pytest.mark.asyncio
    async def test_non_object_body_is_refused(self, body: Any) -> None:
        handlers = build_settings_handlers(server=None, is_sidecar=True)
        request = MagicMock()
        request.json = AsyncMock(return_value=body)
        resp = await handlers["save_theme_prefs"](request)
        assert resp.status_code == 400
        assert "Body must be a JSON object" in resp.body.decode()
        assert not _prefs_file().exists()

    @pytest.mark.asyncio
    async def test_unknown_keys_dropped(self) -> None:
        status, body = await _save(
            {"theme": "light", "unknown_key": "value", "another": 123}
        )
        assert status == 200
        assert body["applied"] == {"theme": "light"}
        assert json.loads(_prefs_file().read_text()) == {"theme": "light"}

    @pytest.mark.asyncio
    async def test_out_of_enum_values_leave_nothing_to_save(self) -> None:
        status, body = await _save(
            {
                "theme": "purple",  # Not in allowed values.
                "fontSize": "200",  # Not in allowed values.
                "contrast": "medium",  # Not in allowed values.
                "shade": "dark-gray",  # Not in allowed values.
            }
        )
        assert status == 400
        assert body["success"] is False
        assert "No valid theme preference fields" in str(body)
        assert not _prefs_file().exists()

    @pytest.mark.asyncio
    async def test_valid_full_set_kept(self) -> None:
        raw = {
            "theme": "light",
            "fontSize": "130",
            "contrast": "high",
            "shade": "paper",
        }
        status, body = await _save(raw)
        assert status == 200
        assert body["applied"] == raw

    @pytest.mark.asyncio
    async def test_custom_keeps_only_valid_hex_parts(self) -> None:
        custom = {
            "bg": "#112233",  # Valid.
            "text": "not a hex",  # Invalid.
            "accent": "#abcdef",  # Valid.
            "unknown": "#ffffff",  # Unknown part.
        }
        status, body = await _save({"theme": "dark", "custom": json.dumps(custom)})
        assert status == 200
        assert body["applied"]["theme"] == "dark"
        # Only valid hex parts re-serialized.
        assert json.loads(body["applied"]["custom"]) == {
            "bg": "#112233",
            "accent": "#abcdef",
        }

    @pytest.mark.asyncio
    async def test_custom_empty_string_kept(self) -> None:
        """Empty string is the cleared-marker and must be kept."""
        status, body = await _save({"theme": "light", "custom": ""})
        assert status == 200
        assert body["applied"] == {"theme": "light", "custom": ""}

    @pytest.mark.asyncio
    async def test_custom_invalid_json_dropped(self) -> None:
        status, body = await _save({"theme": "light", "custom": "not valid json {{{"})
        # Invalid JSON is dropped; theme is kept.
        assert status == 200
        assert body["applied"] == {"theme": "light"}


class TestLoadedPrefs:
    """GET reads the saved file best-effort."""

    @pytest.mark.asyncio
    async def test_missing_file_returns_empty(self) -> None:
        assert await _load() == {}

    @pytest.mark.asyncio
    async def test_corrupt_json_returns_empty(self) -> None:
        _prefs_file().write_text("not valid json {{{")
        assert await _load() == {}

    @pytest.mark.asyncio
    async def test_file_with_out_of_enum_values_sanitized_away(self) -> None:
        _prefs_file().write_text(json.dumps({"theme": "purple", "fontSize": "200"}))
        # Both values are out of enum; sanitized to empty.
        assert await _load() == {}

    @pytest.mark.asyncio
    async def test_dropped_key_warns_once_per_process(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A stale/invalid key warns on first load only — the page handler
        loads the prefs on every view, and a permanently invalid file entry
        must not spam the log per view (#1574 review).
        """
        # A key no earlier test in the process has warned about.
        stale = f"legacy_key_{uuid.uuid4().hex[:8]}"
        _prefs_file().write_text(json.dumps({"theme": "light", stale: "x"}))
        with caplog.at_level("WARNING", logger="ha_mcp.settings_ui"):
            assert await _load() == {"theme": "light"}
            assert await _load() == {"theme": "light"}
        warnings = [
            r for r in caplog.records if "Ignoring invalid theme pref" in r.getMessage()
        ]
        assert len(warnings) == 1, warnings
        assert stale in warnings[0].getMessage()


class TestRenderSettingsHtml:
    """The served page seeds the saved prefs."""

    def test_placeholder_replaced_with_escaped_json(self) -> None:
        prefs = {"theme": "light", "fontSize": "130"}
        _prefs_file().write_text(json.dumps(prefs))
        html = render_settings_html()
        # Placeholder should not survive.
        assert "__HA_MCP_THEME_PREFS__" not in html
        # Escaped JSON should be present (quotes become &quot;).
        assert "&quot;theme&quot;:&quot;light&quot;" in html
        assert "&quot;fontSize&quot;:&quot;130&quot;" in html

    def test_empty_prefs_still_substitutes(self) -> None:
        """When prefs are empty, placeholder is replaced with '{}'."""
        html = render_settings_html()
        assert "__HA_MCP_THEME_PREFS__" not in html
        # Empty object is still valid JSON; check for the attribute.
        assert "data-prefs=" in html


class TestThemePrefsHandlers:
    """GET and POST round trips for /api/settings/theme."""

    @pytest.mark.asyncio
    async def test_get_returns_persisted_prefs(self) -> None:
        prefs = {"theme": "dark", "contrast": "high"}
        _prefs_file().write_text(json.dumps(prefs))
        assert await _load() == prefs

    @pytest.mark.asyncio
    async def test_post_merges_into_existing_file(self) -> None:
        """Partial POST merges with existing prefs (RMW under lock)."""
        _prefs_file().write_text(json.dumps({"theme": "dark", "fontSize": "100"}))

        # POST only contrast; theme and fontSize should survive.
        status, body = await _save({"contrast": "high"})

        assert status == 200
        assert body["success"] is True
        assert body["applied"] == {"contrast": "high"}
        saved = json.loads(_prefs_file().read_text())
        assert saved == {"theme": "dark", "fontSize": "100", "contrast": "high"}

    @pytest.mark.asyncio
    async def test_post_then_get_round_trip(self) -> None:
        """POST then GET returns the persisted prefs."""
        await _save({"theme": "light", "shade": "paper"})
        assert await _load() == {"theme": "light", "shade": "paper"}

    @pytest.mark.asyncio
    async def test_oserror_on_write_returns_500(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OSError during write (e.g. read-only fs) returns 500."""
        import os

        def fake_replace(src: Any, dst: Any) -> None:
            raise OSError(30, "Read-only file system")

        monkeypatch.setattr(os, "replace", fake_replace)

        status, body = await _save({"theme": "light"})

        assert status == 500
        assert body["success"] is False
