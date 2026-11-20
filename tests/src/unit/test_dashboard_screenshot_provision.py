"""Supervisor discovery rules for supported screenshot engines."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from aiohasupervisor.models.addons import AddonState

from ha_mcp import config
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.dashboard_screenshot import provision
from ha_mcp.dashboard_screenshot.theme_guard import EngineCredential

from ._dashboard_screenshot_common import (
    _puppet_info,
    clear_screenshot_env,
    set_settings,
)
from ._tools_addons_core import Supervisor, app_info, installed_apps
from ._tools_addons_core import supervisor as supervisor
from ._tools_addons_core import target_app as target_app

_WPE_SCHEMA = [
    {"name": "home_assistant_url"},
    {"name": "home_assistant_token"},
    {"name": "dashboard_path"},
]


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch, supervisor: Supervisor) -> None:
    clear_screenshot_env(monkeypatch)
    monkeypatch.setenv("SUPERVISOR_BASE_URL", f"http://{supervisor.url}")


def _wpe_info(*, state: str, hostname: str | None = None) -> dict[str, Any]:
    return app_info(
        "def_wpe_screenshot_engine",
        name="WPE Screenshot Engine",
        state=AddonState(state),
        version="0.3.0",
        hostname=hostname or "",
        schema=_WPE_SCHEMA,
        options={
            "home_assistant_url": "http://homeassistant:8123",
            "home_assistant_token": "wpe-test-token",
            "dashboard_path": "lovelace/energy",
        },
    )


def _serve_supervisor(supervisor: Supervisor, answers: dict[str, Any]) -> None:
    for path, data in answers.items():
        if isinstance(data, httpx.TransportError):
            supervisor.drop("GET", path)
        else:
            supervisor.answer("GET", path, data)


@pytest.fixture
def supervisor_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """HA OS / Supervised, with no explicit engine URL."""
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-supervisor-token")
    monkeypatch.setattr(
        config.get_global_settings(), "dashboard_screenshot_engine_url", ""
    )


class TestWpeSupervisorDiscovery:
    async def test_discovers_verified_app_on_internal_port(
        self, supervisor: Supervisor, supervisor_mode: None
    ) -> None:
        _serve_supervisor(
            supervisor,
            {
                "/addons": installed_apps("abc_puppet", "def_wpe_screenshot_engine"),
                "/addons/abc_puppet/info": _puppet_info(
                    slug="abc_puppet", state="stopped"
                ),
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
        self, supervisor: Supervisor, supervisor_mode: None
    ) -> None:
        _serve_supervisor(
            supervisor,
            {
                "/addons": installed_apps("abc_puppet", "def_wpe_screenshot_engine"),
                "/addons/abc_puppet/info": _puppet_info(
                    slug="abc_puppet", state="started", hostname="abc-puppet"
                ),
                "/addons/def_wpe_screenshot_engine/info": _wpe_info(
                    state="started", hostname="def-wpe"
                ),
            },
        )

        with pytest.raises(ToolError, match=r"(?i)ambiguous"):
            await provision.resolve_engine()

    async def test_wrong_app_identity_is_rejected(
        self, supervisor: Supervisor, supervisor_mode: None
    ) -> None:
        invalid_info = _wpe_info(state="started", hostname="wrong-engine")
        invalid_info["name"] = "Unrelated app"
        _serve_supervisor(
            supervisor,
            {
                "/addons": installed_apps("def_wpe_screenshot_engine"),
                "/addons/def_wpe_screenshot_engine/info": invalid_info,
            },
        )

        with pytest.raises(ToolError, match=r"(?i)identity and schema"):
            await provision.resolve_engine()

    async def test_incomplete_app_schema_is_rejected(
        self, supervisor: Supervisor, supervisor_mode: None
    ) -> None:
        invalid_info = _wpe_info(state="started", hostname="def-wpe")
        invalid_info["schema"] = _WPE_SCHEMA[:-1]
        _serve_supervisor(
            supervisor,
            {
                "/addons": installed_apps("def_wpe_screenshot_engine"),
                "/addons/def_wpe_screenshot_engine/info": invalid_info,
            },
        )

        with pytest.raises(ToolError, match=r"(?i)identity and schema"):
            await provision.resolve_engine()

    async def test_installed_app_must_be_started(
        self, supervisor: Supervisor, supervisor_mode: None
    ) -> None:
        _serve_supervisor(
            supervisor,
            {
                "/addons": installed_apps("def_wpe_screenshot_engine"),
                "/addons/def_wpe_screenshot_engine/info": _wpe_info(state="stopped"),
            },
        )

        with pytest.raises(ToolError, match=r"(?i)not started") as exc_info:
            await provision.resolve_engine()

        assert str(exc_info.value).lower().count("start it") == 1

    async def test_explicit_url_keeps_puppet_theme_guard_with_wpe_installed(
        self,
        supervisor: Supervisor,
        monkeypatch: pytest.MonkeyPatch,
        supervisor_mode: None,
    ) -> None:
        monkeypatch.setattr(
            config.get_global_settings(),
            "dashboard_screenshot_engine_url",
            "http://chosen-engine:8080",
        )
        _serve_supervisor(
            supervisor,
            {
                "/addons": installed_apps("def_puppet", "abc_wpe_screenshot_engine"),
                "/addons/def_puppet/info": _puppet_info(
                    slug="def_puppet", state="started", hostname="def-puppet"
                ),
            },
        )

        target = await provision.resolve_engine()

        assert target.url == "http://chosen-engine:8080"
        assert target.addon_credential == EngineCredential(
            url="http://homeassistant:8123", token="secret"
        )


class TestResolveEngine:
    async def test_explicit_url_strips_trailing_slash(self, monkeypatch: Any) -> None:
        from ha_mcp.dashboard_screenshot import provision

        set_settings(
            monkeypatch, dashboard_screenshot_engine_url="http://engine:10000/"
        )
        target = await provision.resolve_engine()
        assert target.url == "http://engine:10000"
        assert target.engine_kind is None
        assert target.engine_version is None
        # An explicitly configured engine has no discoverable credential.
        assert target.addon_credential is None

    async def test_stdio_no_token_raises(self, monkeypatch: Any) -> None:
        from ha_mcp.dashboard_screenshot import provision

        set_settings(monkeypatch, dashboard_screenshot_engine_url="")
        with pytest.raises(ToolError):
            await provision.resolve_engine()

    async def test_explicit_url_picks_up_addon_credential_in_addon_mode(
        self, supervisor: Supervisor, monkeypatch: Any
    ) -> None:
        # An explicit engine URL on HA OS must not disable the Puppet theme
        # guard: the add-on credential is still discovered best-effort.
        from ha_mcp.dashboard_screenshot import provision
        from ha_mcp.dashboard_screenshot.theme_guard import EngineCredential

        monkeypatch.setenv("SUPERVISOR_TOKEN", "sup")
        set_settings(monkeypatch, dashboard_screenshot_engine_url="http://engine:10000")
        _serve_supervisor(
            supervisor,
            {
                "/addons": installed_apps("def_puppet"),
                "/addons/def_puppet/info": _puppet_info(
                    slug="def_puppet", state="started", hostname="def-puppet"
                ),
            },
        )
        target = await provision.resolve_engine()
        assert target.url == "http://engine:10000"
        assert target.addon_credential == EngineCredential(
            url="http://homeassistant:8123", token="secret"
        )

    async def test_explicit_url_credential_discovery_failure_is_silent(
        self, supervisor: Supervisor, monkeypatch: Any
    ) -> None:
        import httpx

        from ha_mcp.dashboard_screenshot import provision

        monkeypatch.setenv("SUPERVISOR_TOKEN", "sup")
        set_settings(monkeypatch, dashboard_screenshot_engine_url="http://engine:10000")
        _serve_supervisor(supervisor, {"/addons": httpx.ConnectError("boom")})
        target = await provision.resolve_engine()
        assert target.url == "http://engine:10000"
        assert target.addon_credential is None


class TestDiscoverEngineViaSupervisor:
    async def test_not_installed_raises_resource_not_found(
        self, supervisor: Supervisor, supervisor_mode: None
    ) -> None:
        from ha_mcp.dashboard_screenshot import provision

        _serve_supervisor(
            supervisor,
            {"/addons": installed_apps("core_ssh", "a_db")},
        )
        with pytest.raises(ToolError) as exc:
            await provision.resolve_engine()
        assert "not installed" in str(exc.value).lower()

    async def test_prefers_started_match(
        self, supervisor: Supervisor, supervisor_mode: None
    ) -> None:
        from ha_mcp.dashboard_screenshot import provision

        _serve_supervisor(
            supervisor,
            {
                "/addons": installed_apps("abc_puppet", "def_puppet"),
                "/addons/abc_puppet/info": _puppet_info(
                    slug="abc_puppet", state="stopped"
                ),
                "/addons/def_puppet/info": _puppet_info(
                    slug="def_puppet", state="started", hostname="def-puppet"
                ),
            },
        )
        target = await provision.resolve_engine()
        assert target.url == "http://def-puppet:10000"
        assert target.engine_kind == "puppet"
        assert target.engine_version == "2.4.9"
        # The engine user's credential rides along so the theme guard can
        # authenticate without a second discovery round-trip.
        from ha_mcp.dashboard_screenshot.theme_guard import EngineCredential

        assert target.addon_credential == EngineCredential(
            url="http://homeassistant:8123", token="secret"
        )

    async def test_multiple_verified_started_matches_fail_closed(
        self, supervisor: Supervisor, supervisor_mode: None
    ) -> None:
        from ha_mcp.dashboard_screenshot import provision

        _serve_supervisor(
            supervisor,
            {
                "/addons": installed_apps("abc_puppet", "def_puppet"),
                "/addons/abc_puppet/info": _puppet_info(
                    slug="abc_puppet", state="started", hostname="abc-puppet"
                ),
                "/addons/def_puppet/info": _puppet_info(
                    slug="def_puppet", state="started", hostname="def-puppet"
                ),
            },
        )

        with pytest.raises(ToolError) as exc_info:
            await provision.resolve_engine()

        assert "ambiguous" in str(exc_info.value)

    async def test_started_without_hostname_uses_its_ip(
        self, supervisor: Supervisor, supervisor_mode: None
    ) -> None:
        from ha_mcp.dashboard_screenshot import provision

        _serve_supervisor(
            supervisor,
            {
                "/addons": installed_apps("def_puppet"),
                "/addons/def_puppet/info": _puppet_info(
                    slug="def_puppet", state="started"
                ),
            },
        )
        target = await provision.resolve_engine()
        assert target.url == "http://127.0.0.1:10000"

    async def test_installed_but_not_started_raises(
        self, supervisor: Supervisor, supervisor_mode: None
    ) -> None:
        from ha_mcp.dashboard_screenshot import provision

        _serve_supervisor(
            supervisor,
            {
                "/addons": installed_apps("def_puppet"),
                "/addons/def_puppet/info": _puppet_info(
                    slug="def_puppet", state="stopped"
                ),
            },
        )
        with pytest.raises(ToolError) as exc:
            await provision.resolve_engine()
        assert "not started" in str(exc.value).lower()

    async def test_supervisor_http_error_raises_connection_failed(
        self, supervisor: Supervisor, supervisor_mode: None
    ) -> None:
        import httpx

        from ha_mcp.dashboard_screenshot import provision

        _serve_supervisor(supervisor, {"/addons": httpx.ConnectError("boom")})
        with pytest.raises(ToolError) as exc:
            await provision.resolve_engine()
        assert "supervisor" in str(exc.value).lower()
