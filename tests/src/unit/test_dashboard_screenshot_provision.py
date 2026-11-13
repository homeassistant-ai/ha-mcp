"""Supervisor discovery rules for supported screenshot engines."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from ha_mcp import config
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client import supervisor_client
from ha_mcp.dashboard_screenshot import provision
from ha_mcp.dashboard_screenshot.theme_guard import EngineCredential

_PUPPET_SCHEMA = [
    {"name": "access_token"},
    {"name": "keep_browser_open"},
    {"name": "home_assistant_url"},
]
_WPE_SCHEMA = [
    {"name": "home_assistant_url"},
    {"name": "home_assistant_token"},
    {"name": "dashboard_path"},
]


def _puppet_info(*, state: str, hostname: str | None = None) -> dict[str, Any]:
    info: dict[str, Any] = {
        "name": "Puppet",
        "state": state,
        "version": "2.4.9",
        "schema": _PUPPET_SCHEMA,
        "options": {
            "access_token": "secret",
            "keep_browser_open": False,
            "home_assistant_url": "http://homeassistant:8123",
        },
    }
    if hostname is not None:
        info["hostname"] = hostname
    return info


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


def _serve_supervisor(
    monkeypatch: pytest.MonkeyPatch, answers: dict[str, dict[str, Any]]
) -> None:
    """Answer the Supervisor REST paths in ``answers``; any other path is a 404."""

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path not in answers:
            return httpx.Response(404, json={"result": "error", "message": "Not found"})
        return httpx.Response(
            200, json={"result": "ok", "data": answers[request.url.path]}
        )

    def client(**_: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url="http://supervisor", transport=httpx.MockTransport(answer)
        )

    monkeypatch.setattr(supervisor_client, "make_supervisor_httpx_client", client)


@pytest.fixture
def supervisor_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """HA OS / Supervised, with no explicit engine URL."""
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-supervisor-token")
    monkeypatch.setattr(
        config.get_global_settings(), "dashboard_screenshot_engine_url", ""
    )


class TestWpeSupervisorDiscovery:
    async def test_discovers_verified_app_on_internal_port(
        self, monkeypatch: pytest.MonkeyPatch, supervisor_mode: None
    ) -> None:
        _serve_supervisor(
            monkeypatch,
            {
                "/addons": {
                    "addons": [
                        {"slug": "abc_puppet"},
                        {"slug": "def_wpe_screenshot_engine"},
                    ]
                },
                "/addons/abc_puppet/info": _puppet_info(state="stopped"),
                "/addons/def_wpe_screenshot_engine/info": _wpe_info(
                    state="started", hostname="def-wpe"
                ),
            },
        )

        target = await provision.resolve_engine()

        # Supervisor-network discovery uses the app's internal port, not the
        # configurable host port used by clients outside the app network.
        assert target.url == "http://def-wpe:8080"
        assert target.engine_kind == "wpe"
        assert target.engine_version == "0.3.0"
        assert target.addon_credential is None

    async def test_started_wpe_and_puppet_are_ambiguous(
        self, monkeypatch: pytest.MonkeyPatch, supervisor_mode: None
    ) -> None:
        _serve_supervisor(
            monkeypatch,
            {
                "/addons": {
                    "addons": [
                        {"slug": "abc_puppet"},
                        {"slug": "def_wpe_screenshot_engine"},
                    ]
                },
                "/addons/abc_puppet/info": _puppet_info(
                    state="started", hostname="abc-puppet"
                ),
                "/addons/def_wpe_screenshot_engine/info": _wpe_info(
                    state="started", hostname="def-wpe"
                ),
            },
        )

        with pytest.raises(ToolError, match=r"(?i)ambiguous"):
            await provision.resolve_engine()

    async def test_wrong_app_identity_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch, supervisor_mode: None
    ) -> None:
        invalid_info = _wpe_info(state="started", hostname="wrong-engine")
        invalid_info["name"] = "Unrelated app"
        _serve_supervisor(
            monkeypatch,
            {
                "/addons": {"addons": [{"slug": "def_wpe_screenshot_engine"}]},
                "/addons/def_wpe_screenshot_engine/info": invalid_info,
            },
        )

        with pytest.raises(ToolError, match=r"(?i)identity and schema"):
            await provision.resolve_engine()

    async def test_incomplete_app_schema_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch, supervisor_mode: None
    ) -> None:
        invalid_info = _wpe_info(state="started", hostname="def-wpe")
        invalid_info["schema"] = _WPE_SCHEMA[:-1]
        _serve_supervisor(
            monkeypatch,
            {
                "/addons": {"addons": [{"slug": "def_wpe_screenshot_engine"}]},
                "/addons/def_wpe_screenshot_engine/info": invalid_info,
            },
        )

        with pytest.raises(ToolError, match=r"(?i)identity and schema"):
            await provision.resolve_engine()

    async def test_installed_app_must_be_started(
        self, monkeypatch: pytest.MonkeyPatch, supervisor_mode: None
    ) -> None:
        _serve_supervisor(
            monkeypatch,
            {
                "/addons": {"addons": [{"slug": "def_wpe_screenshot_engine"}]},
                "/addons/def_wpe_screenshot_engine/info": _wpe_info(state="stopped"),
            },
        )

        with pytest.raises(ToolError, match=r"(?i)not started") as exc_info:
            await provision.resolve_engine()

        assert str(exc_info.value).lower().count("start it") == 1

    async def test_explicit_url_keeps_puppet_theme_guard_with_wpe_installed(
        self, monkeypatch: pytest.MonkeyPatch, supervisor_mode: None
    ) -> None:
        monkeypatch.setattr(
            config.get_global_settings(),
            "dashboard_screenshot_engine_url",
            "http://chosen-engine:8080",
        )
        _serve_supervisor(
            monkeypatch,
            {
                "/addons": {
                    "addons": [
                        {"slug": "def_puppet"},
                        {"slug": "abc_wpe_screenshot_engine"},
                    ]
                },
                "/addons/def_puppet/info": _puppet_info(
                    state="started", hostname="def-puppet"
                ),
            },
        )

        target = await provision.resolve_engine()

        assert target.url == "http://chosen-engine:8080"
        assert target.addon_credential == EngineCredential(
            url="http://homeassistant:8123", token="secret"
        )
