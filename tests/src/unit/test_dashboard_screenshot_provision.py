"""Supervisor discovery rules for supported screenshot engines."""

from __future__ import annotations

import json
from types import SimpleNamespace
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


class TestResolveEngine:
    async def test_explicit_url_strips_trailing_slash(self, monkeypatch: Any) -> None:
        from ha_mcp.dashboard_screenshot import provision

        monkeypatch.setattr(
            config,
            "get_global_settings",
            lambda: SimpleNamespace(
                dashboard_screenshot_engine_url="http://engine:10000/"
            ),
        )
        target = await provision.resolve_engine()
        assert target.url == "http://engine:10000"
        assert target.engine_kind is None
        assert target.engine_version is None
        # An explicitly configured engine has no discoverable credential.
        assert target.addon_credential is None

    async def test_stdio_no_token_raises(self, monkeypatch: Any) -> None:
        from ha_mcp.dashboard_screenshot import provision

        monkeypatch.setattr(
            config,
            "get_global_settings",
            lambda: SimpleNamespace(dashboard_screenshot_engine_url=""),
        )
        with pytest.raises(ToolError):
            await provision.resolve_engine()

    async def test_explicit_url_picks_up_addon_credential_in_addon_mode(
        self, monkeypatch: Any
    ) -> None:
        # An explicit engine URL on HA OS must not disable the Puppet theme
        # guard: the add-on credential is still discovered best-effort.
        from ha_mcp.dashboard_screenshot import provision
        from ha_mcp.dashboard_screenshot.theme_guard import EngineCredential

        monkeypatch.setenv("SUPERVISOR_TOKEN", "sup")
        monkeypatch.setattr(
            config,
            "get_global_settings",
            lambda: SimpleNamespace(
                dashboard_screenshot_engine_url="http://engine:10000"
            ),
        )
        _patch_supervisor(
            monkeypatch,
            {
                "/addons": {"data": {"addons": [{"slug": "def_puppet"}]}},
                "/addons/def_puppet/info": {
                    "data": _puppet_info(state="started", hostname="def-puppet")
                },
            },
        )
        target = await provision.resolve_engine()
        assert target.url == "http://engine:10000"
        assert target.addon_credential == EngineCredential(
            url="http://homeassistant:8123", token="secret"
        )

    async def test_explicit_url_credential_discovery_failure_is_silent(
        self, monkeypatch: Any
    ) -> None:
        import httpx

        from ha_mcp.dashboard_screenshot import provision

        monkeypatch.setenv("SUPERVISOR_TOKEN", "sup")
        monkeypatch.setattr(
            config,
            "get_global_settings",
            lambda: SimpleNamespace(
                dashboard_screenshot_engine_url="http://engine:10000"
            ),
        )
        _patch_supervisor(monkeypatch, {"/addons": httpx.ConnectError("boom")})
        target = await provision.resolve_engine()
        assert target.url == "http://engine:10000"
        assert target.addon_credential is None


class _FakeSupResponse:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._payload


class _FakeSupClient:
    """Async-context Supervisor httpx stand-in driven by a path->payload map."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self._routes = routes

    async def __aenter__(self) -> _FakeSupClient:
        return self

    async def __aexit__(self, *_a: Any) -> None:
        return None

    async def get(self, path: str) -> _FakeSupResponse:
        val = self._routes[path]
        if isinstance(val, Exception):
            raise val
        return _FakeSupResponse(val)


def _patch_supervisor(monkeypatch: Any, routes: dict[str, Any]) -> None:
    import ha_mcp.client.supervisor_client as sup_mod

    monkeypatch.setattr(
        sup_mod,
        "make_supervisor_httpx_client",
        lambda *_a, **_kw: _FakeSupClient(routes),
    )


class TestDiscoverEngineViaSupervisor:
    async def test_not_installed_raises_resource_not_found(
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import provision

        _patch_supervisor(
            monkeypatch,
            {"/addons": {"data": {"addons": [{"slug": "core_ssh"}, {"slug": "a_db"}]}}},
        )
        with pytest.raises(ToolError) as exc:
            await provision._discover_engine_via_supervisor()
        assert "not installed" in str(exc.value).lower()

    async def test_prefers_started_match(self, monkeypatch: Any) -> None:
        from ha_mcp.dashboard_screenshot import provision

        _patch_supervisor(
            monkeypatch,
            {
                "/addons": {
                    "data": {
                        "addons": [
                            {"slug": "abc_puppet"},  # stopped
                            {"slug": "def_puppet"},  # started
                        ]
                    }
                },
                "/addons/abc_puppet/info": {"data": _puppet_info(state="stopped")},
                "/addons/def_puppet/info": {
                    "data": _puppet_info(state="started", hostname="def-puppet")
                },
            },
        )
        target = await provision._discover_engine_via_supervisor()
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
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import provision

        _patch_supervisor(
            monkeypatch,
            {
                "/addons": {
                    "data": {
                        "addons": [
                            {"slug": "abc_puppet"},
                            {"slug": "def_puppet"},
                        ]
                    }
                },
                "/addons/abc_puppet/info": {
                    "data": _puppet_info(state="started", hostname="abc-puppet")
                },
                "/addons/def_puppet/info": {
                    "data": _puppet_info(state="started", hostname="def-puppet")
                },
            },
        )

        with pytest.raises(ToolError) as exc_info:
            await provision._discover_engine_via_supervisor()

        assert "ambiguous" in str(exc_info.value)

    async def test_started_without_hostname_raises(self, monkeypatch: Any) -> None:
        from ha_mcp.dashboard_screenshot import provision

        _patch_supervisor(
            monkeypatch,
            {
                "/addons": {"data": {"addons": [{"slug": "def_puppet"}]}},
                "/addons/def_puppet/info": {"data": _puppet_info(state="started")},
            },
        )
        with pytest.raises(ToolError) as exc:
            await provision._discover_engine_via_supervisor()
        assert "hostname" in str(exc.value).lower()

    async def test_installed_but_not_started_raises(self, monkeypatch: Any) -> None:
        from ha_mcp.dashboard_screenshot import provision

        _patch_supervisor(
            monkeypatch,
            {
                "/addons": {"data": {"addons": [{"slug": "def_puppet"}]}},
                "/addons/def_puppet/info": {"data": _puppet_info(state="stopped")},
            },
        )
        with pytest.raises(ToolError) as exc:
            await provision._discover_engine_via_supervisor()
        assert "not started" in str(exc.value).lower()

    async def test_supervisor_http_error_raises_connection_failed(
        self, monkeypatch: Any
    ) -> None:
        import httpx

        from ha_mcp.dashboard_screenshot import provision

        _patch_supervisor(monkeypatch, {"/addons": httpx.ConnectError("boom")})
        with pytest.raises(ToolError) as exc:
            await provision._discover_engine_via_supervisor()
        assert "supervisor" in str(exc.value).lower()

    @pytest.mark.parametrize(
        "payload",
        [
            None,
            {"data": None},
            {"data": {"addons": None}},
            {"data": {"addons": [None]}},
        ],
    )
    async def test_malformed_addon_listing_raises_structured_error(
        self, monkeypatch: Any, payload: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import provision

        _patch_supervisor(monkeypatch, {"/addons": payload})

        with pytest.raises(ToolError) as exc_info:
            await provision._discover_engine_via_supervisor()

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "CONNECTION_FAILED"
        assert "Supervisor" in error["error"]["message"]

    async def test_malformed_addon_info_raises_structured_error(
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import provision

        _patch_supervisor(
            monkeypatch,
            {
                "/addons": {"data": {"addons": [{"slug": "def_puppet"}]}},
                "/addons/def_puppet/info": {"data": None},
            },
        )

        with pytest.raises(ToolError) as exc_info:
            await provision._discover_engine_via_supervisor()

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "CONNECTION_FAILED"
        assert "Supervisor" in error["error"]["message"]
