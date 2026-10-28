"""Supervisor discovery rules for supported screenshot engines."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from ha_mcp import config
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.dashboard_screenshot import provision
from ha_mcp.dashboard_screenshot.theme_guard import EngineCredential
from tests.src.unit.test_dashboard_screenshot import _patch_supervisor, _puppet_info

_WPE_SCHEMA = [
    {"name": "home_assistant_url"},
    {"name": "home_assistant_token"},
    {"name": "dashboard_path"},
]


def _wpe_info(*, state: str, hostname: str | None = None) -> dict[str, Any]:
    info: dict[str, Any] = {
        "name": "WPE Screenshot Engine",
        "state": state,
        "version": "0.3.0",
        "schema": _WPE_SCHEMA,
        "options": {
            "home_assistant_url": "http://homeassistant:8123",
            "home_assistant_token": "wpe-test-token",
            "dashboard_path": "lovelace/energy",
        },
    }
    if hostname is not None:
        info["hostname"] = hostname
    return info


class TestWpeSupervisorDiscovery:
    async def test_discovers_verified_app_on_internal_port(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_supervisor(
            monkeypatch,
            {
                "/addons": {
                    "data": {
                        "addons": [
                            {"slug": "abc_puppet"},
                            {"slug": "def_wpe_screenshot_engine"},
                        ]
                    }
                },
                "/addons/abc_puppet/info": {"data": _puppet_info(state="stopped")},
                "/addons/def_wpe_screenshot_engine/info": {
                    "data": _wpe_info(state="started", hostname="def-wpe")
                },
            },
        )

        target = await provision._discover_engine_via_supervisor()

        # Supervisor-network discovery uses the app's internal port, not the
        # configurable host port used by clients outside the app network.
        assert target.url == "http://def-wpe:8080"
        assert target.engine_kind == "wpe"
        assert target.engine_version == "0.3.0"
        assert target.addon_credential is None

    async def test_started_wpe_and_puppet_are_ambiguous(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_supervisor(
            monkeypatch,
            {
                "/addons": {
                    "data": {
                        "addons": [
                            {"slug": "abc_puppet"},
                            {"slug": "def_wpe_screenshot_engine"},
                        ]
                    }
                },
                "/addons/abc_puppet/info": {
                    "data": _puppet_info(state="started", hostname="abc-puppet")
                },
                "/addons/def_wpe_screenshot_engine/info": {
                    "data": _wpe_info(state="started", hostname="def-wpe")
                },
            },
        )

        with pytest.raises(ToolError, match=r"(?i)ambiguous"):
            await provision._discover_engine_via_supervisor()

    async def test_wrong_app_identity_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        invalid_info = _wpe_info(state="started", hostname="wrong-engine")
        invalid_info["name"] = "Unrelated app"
        _patch_supervisor(
            monkeypatch,
            {
                "/addons": {
                    "data": {"addons": [{"slug": "def_wpe_screenshot_engine"}]}
                },
                "/addons/def_wpe_screenshot_engine/info": {"data": invalid_info},
            },
        )

        with pytest.raises(ToolError, match=r"(?i)identity and schema"):
            await provision._discover_engine_via_supervisor()

    async def test_incomplete_app_schema_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        invalid_info = _wpe_info(state="started", hostname="def-wpe")
        invalid_info["schema"] = _WPE_SCHEMA[:-1]
        _patch_supervisor(
            monkeypatch,
            {
                "/addons": {
                    "data": {"addons": [{"slug": "def_wpe_screenshot_engine"}]}
                },
                "/addons/def_wpe_screenshot_engine/info": {"data": invalid_info},
            },
        )

        with pytest.raises(ToolError, match=r"(?i)identity and schema"):
            await provision._discover_engine_via_supervisor()

    async def test_installed_app_must_be_started(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_supervisor(
            monkeypatch,
            {
                "/addons": {
                    "data": {"addons": [{"slug": "def_wpe_screenshot_engine"}]}
                },
                "/addons/def_wpe_screenshot_engine/info": {
                    "data": _wpe_info(state="stopped")
                },
            },
        )

        with pytest.raises(ToolError, match=r"(?i)not started") as exc_info:
            await provision._discover_engine_via_supervisor()

        assert str(exc_info.value).lower().count("start it") == 1

    async def test_explicit_url_keeps_puppet_theme_guard_with_wpe_installed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SUPERVISOR_TOKEN", "test-supervisor-token")
        monkeypatch.setattr(
            config,
            "get_global_settings",
            lambda: SimpleNamespace(
                dashboard_screenshot_engine_url="http://chosen-engine:8080"
            ),
        )
        _patch_supervisor(
            monkeypatch,
            {
                "/addons": {
                    "data": {
                        "addons": [
                            {"slug": "def_puppet"},
                            {"slug": "abc_wpe_screenshot_engine"},
                        ]
                    }
                },
                "/addons/def_puppet/info": {
                    "data": _puppet_info(state="started", hostname="def-puppet")
                },
            },
        )

        target = await provision.resolve_engine()

        assert target.url == "http://chosen-engine:8080"
        assert target.addon_credential == EngineCredential(
            url="http://homeassistant:8123", token="secret"
        )
