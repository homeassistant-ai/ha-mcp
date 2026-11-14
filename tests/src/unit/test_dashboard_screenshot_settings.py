"""The dashboard-screenshot settings, tool-registration gate and frontend paths."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from pydantic import ValidationError

from ha_mcp import config

from ._dashboard_screenshot_common import (
    clear_screenshot_env,
    fresh_settings,
    set_settings,
)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: Any) -> None:
    clear_screenshot_env(monkeypatch)


@pytest.fixture(autouse=True)
def _reset_settings() -> Iterator[None]:
    yield
    config.reset_global_settings()


class TestSettings:
    def test_flag_default_disabled(self) -> None:
        assert fresh_settings().enable_dashboard_screenshot is False

    def test_flag_enabled_via_env(self, monkeypatch: Any) -> None:
        # A beta feature: it is on only with the beta master toggle on too.
        monkeypatch.setenv("ENABLE_BETA_FEATURES", "true")
        monkeypatch.setenv("HAMCP_ENABLE_DASHBOARD_SCREENSHOT", "true")
        assert fresh_settings().enable_dashboard_screenshot is True

    def test_flag_empty_string_means_false(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("HAMCP_ENABLE_DASHBOARD_SCREENSHOT", "")
        assert fresh_settings().enable_dashboard_screenshot is False

    def test_engine_url_default_empty(self) -> None:
        assert fresh_settings().dashboard_screenshot_engine_url == ""

    def test_engine_url_from_env(self, monkeypatch: Any) -> None:
        monkeypatch.setenv(
            "HAMCP_DASHBOARD_SCREENSHOT_ENGINE_URL", "http://engine:10000"
        )
        assert fresh_settings().dashboard_screenshot_engine_url == "http://engine:10000"

    @pytest.mark.parametrize("bad", ["ftp://engine:10000", "engine:10000", "//engine"])
    def test_engine_url_rejects_non_http(self, monkeypatch: Any, bad: str) -> None:
        # The validator must fail loudly at startup on a scheme-less / wrong
        # scheme URL instead of letting it 0-byte-fail at render time.
        monkeypatch.setenv("HAMCP_DASHBOARD_SCREENSHOT_ENGINE_URL", bad)
        with pytest.raises(ValidationError):
            fresh_settings()

    def test_engine_url_validator_strips_trailing_slash(self, monkeypatch: Any) -> None:
        # The field validator (not just resolve_engine) normalizes the URL.
        monkeypatch.setenv(
            "HAMCP_DASHBOARD_SCREENSHOT_ENGINE_URL", "http://engine:10000/"
        )
        assert fresh_settings().dashboard_screenshot_engine_url == "http://engine:10000"

    def test_flag_in_feature_flag_fields(self) -> None:
        assert "enable_dashboard_screenshot" in {
            f.field for f in config.FEATURE_FLAG_FIELDS
        }

    def test_flag_is_beta_gated(self) -> None:
        assert "enable_dashboard_screenshot" in config.BETA_FEATURE_FIELDS

    def test_engine_url_not_a_feature_flag(self) -> None:
        # Connection string, deliberately not a web-editable beta toggle.
        names = {f.field for f in config.FEATURE_FLAG_FIELDS}
        assert "dashboard_screenshot_engine_url" not in names
        assert "dashboard_screenshot_engine_url" not in config.BETA_FEATURE_FIELDS


class _RecordingMcp:
    def __init__(self) -> None:
        self.added: list[Any] = []

    def add_tool(self, tool: Any) -> None:
        self.added.append(tool)


class TestRegistrationGate:
    def test_gate_off_registers_nothing(self, monkeypatch: Any) -> None:
        from ha_mcp.tools import tools_dashboard_screenshot as mod

        set_settings(monkeypatch, enable_dashboard_screenshot=False)
        mcp = _RecordingMcp()
        mod.register_dashboard_screenshot_tools(mcp, client=None)
        assert mcp.added == []

    def test_gate_on_registers_tool(self, monkeypatch: Any) -> None:
        from ha_mcp.tools import tools_dashboard_screenshot as mod

        set_settings(monkeypatch, enable_dashboard_screenshot=True)
        mcp = _RecordingMcp()
        mod.register_dashboard_screenshot_tools(mcp, client=None)
        assert len(mcp.added) == 1


class TestDashboardFrontendPath:
    @pytest.mark.parametrize(
        ("url_path", "expected"),
        [(None, "lovelace"), ("default", "lovelace"), ("my-dash", "my-dash")],
    )
    def test_maps_default_and_passes_through(
        self, url_path: str | None, expected: str
    ) -> None:
        # The tools_config_dashboards wrapper was inlined away; this now
        # exercises the underlying paths.py implementation directly.
        from ha_mcp.dashboard_screenshot.paths import dashboard_frontend_path

        assert dashboard_frontend_path(url_path) == expected


def test_public_screenshot_option_names_stay_in_parity() -> None:
    """view_path is the only render option shared with the config tools.

    Progressive disclosure: the advanced render params below live solely on
    the dedicated ha_get_dashboard_screenshot tool. The config tools
    (ha_config_get_dashboard / ha_config_set_dashboard) intentionally do not
    accept them — this guards against them silently reappearing there.
    """
    import inspect

    from ha_mcp.tools.tools_config_dashboards import DashboardConfigTools
    from ha_mcp.tools.tools_dashboard_screenshot import DashboardScreenshotTools

    advanced = {
        "width",
        "height",
        "viewport_presets",
        "orientation",
        "zoom",
        "wait_ms",
        "full_page",
        "theme",
        "dark_mode",
        "language",
        "image_format",
        "render_timeout_seconds",
    }

    standalone = set(
        inspect.signature(
            DashboardScreenshotTools.ha_get_dashboard_screenshot
        ).parameters
    )
    get_options = set(
        inspect.signature(DashboardConfigTools.ha_config_get_dashboard).parameters
    )
    set_options = set(
        inspect.signature(DashboardConfigTools.ha_config_set_dashboard).parameters
    )

    assert {"view_path", *advanced} <= standalone
    assert "view_path" in get_options
    assert "view_path" in set_options
    assert not (advanced & get_options)
    assert not (advanced & set_options)
