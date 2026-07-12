"""Unit tests for the opt-in dashboard-screenshot feature (ha-mcp side).

Covers everything that runs inside ha-mcp itself — no container, no engine:
- the ``enable_dashboard_screenshot`` flag + ``dashboard_screenshot_engine_url``
  setting (defaults, env, registry membership)
- tool-registration gating
- engine-URL resolution (explicit / stdio branches)
- the capture HTTP client (URL/param building, PNG return, error -> ToolError)
- the graceful get/set screenshot helper (feature-off / failure -> warning)

The Supervisor auto-discovery branch + addon lifecycle are exercised end to end
against a mock engine in the HAOS inaddon lane
(tests/src/e2e/haos_only/test_dashboard_screenshot_addon.py).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, ClassVar, Literal
from unittest.mock import AsyncMock

import pytest
from fastmcp.exceptions import ToolError
from pydantic import ValidationError

import ha_mcp.config as config

_PNG = b"\x89PNG\r\n\x1a\nunit"
_JPEG = b"\xff\xd8\xffunit"
_WEBP = b"RIFF\x04\x00\x00\x00WEBPunit"
_BMP = b"BM12"
_PUPPET_SCHEMA = [
    {"name": "access_token"},
    {"name": "keep_browser_open"},
    {"name": "home_assistant_url"},
]


def _puppet_options(*, keep_browser_open: bool) -> dict[str, Any]:
    return {
        "access_token": "secret",
        "keep_browser_open": keep_browser_open,
        "home_assistant_url": "http://homeassistant:8123",
    }


def _puppet_info(*, state: str, hostname: str | None = None) -> dict[str, Any]:
    info: dict[str, Any] = {
        "name": "Puppet",
        "state": state,
        "schema": _PUPPET_SCHEMA,
        "options": _puppet_options(keep_browser_open=False),
    }
    if hostname is not None:
        info["hostname"] = hostname
    return info


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: Any) -> None:
    for var in (
        "HAMCP_ENABLE_DASHBOARD_SCREENSHOT",
        "HAMCP_DASHBOARD_SCREENSHOT_ENGINE_URL",
        "SUPERVISOR_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class TestSettings:
    def test_flag_default_disabled(self) -> None:
        assert config.Settings().enable_dashboard_screenshot is False

    def test_flag_enabled_via_env(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("HAMCP_ENABLE_DASHBOARD_SCREENSHOT", "true")
        assert config.Settings().enable_dashboard_screenshot is True

    def test_flag_empty_string_means_false(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("HAMCP_ENABLE_DASHBOARD_SCREENSHOT", "")
        assert config.Settings().enable_dashboard_screenshot is False

    def test_engine_url_default_empty(self) -> None:
        assert config.Settings().dashboard_screenshot_engine_url == ""

    def test_engine_url_from_env(self, monkeypatch: Any) -> None:
        monkeypatch.setenv(
            "HAMCP_DASHBOARD_SCREENSHOT_ENGINE_URL", "http://engine:10000"
        )
        assert (
            config.Settings().dashboard_screenshot_engine_url == "http://engine:10000"
        )

    @pytest.mark.parametrize("bad", ["ftp://engine:10000", "engine:10000", "//engine"])
    def test_engine_url_rejects_non_http(self, monkeypatch: Any, bad: str) -> None:
        # The validator must fail loudly at startup on a scheme-less / wrong
        # scheme URL instead of letting it 0-byte-fail at render time.
        monkeypatch.setenv("HAMCP_DASHBOARD_SCREENSHOT_ENGINE_URL", bad)
        with pytest.raises(ValidationError):
            config.Settings()

    def test_engine_url_validator_strips_trailing_slash(self, monkeypatch: Any) -> None:
        # The field validator (not just resolve_engine_url) normalizes the URL.
        monkeypatch.setenv(
            "HAMCP_DASHBOARD_SCREENSHOT_ENGINE_URL", "http://engine:10000/"
        )
        assert (
            config.Settings().dashboard_screenshot_engine_url == "http://engine:10000"
        )

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


# ---------------------------------------------------------------------------
# Tool registration gating
# ---------------------------------------------------------------------------


class _RecordingMcp:
    def __init__(self) -> None:
        self.added: list[Any] = []

    def add_tool(self, tool: Any) -> None:
        self.added.append(tool)


class TestRegistrationGate:
    def test_gate_off_registers_nothing(self, monkeypatch: Any) -> None:
        from ha_mcp.tools import tools_dashboard_screenshot as mod

        monkeypatch.setattr(
            config,
            "get_global_settings",
            lambda: SimpleNamespace(enable_dashboard_screenshot=False),
        )
        mcp = _RecordingMcp()
        mod.register_dashboard_screenshot_tools(mcp, client=None)
        assert mcp.added == []

    def test_gate_on_registers_tool(self, monkeypatch: Any) -> None:
        from ha_mcp.tools import tools_dashboard_screenshot as mod

        monkeypatch.setattr(
            config,
            "get_global_settings",
            lambda: SimpleNamespace(enable_dashboard_screenshot=True),
        )
        mcp = _RecordingMcp()
        mod.register_dashboard_screenshot_tools(mcp, client=None)
        assert len(mcp.added) == 1


class TestStandaloneScreenshotTool:
    async def test_structured_target_returns_ordered_images_and_metadata(
        self, monkeypatch: Any
    ) -> None:
        from fastmcp.tools.tool import ToolResult

        from ha_mcp.dashboard_screenshot.paths import DashboardRenderTarget
        from ha_mcp.tools import tools_dashboard_screenshot as mod

        async def resolve(*_a: Any, **_kw: Any) -> DashboardRenderTarget:
            return DashboardRenderTarget(
                dashboard_url_path="wall-panel",
                view_path="home",
                render_path="wall-panel/home",
                view_index=0,
                stable=True,
            )

        async def capture(*_a: Any, **_kw: Any) -> list[Any]:
            return [
                _fake_dashboard_capture(width=390, height=844, preset="mobile"),
                _fake_dashboard_capture(width=1280, height=800, preset="desktop"),
            ]

        monkeypatch.setattr(mod, "resolve_dashboard_render_target", resolve)
        monkeypatch.setattr(mod, "capture_dashboard_images", capture)

        result = await mod.DashboardScreenshotTools(
            object()
        ).ha_get_dashboard_screenshot(
            dashboard_url_path="wall-panel",
            view_path="home",
            viewport_presets=["mobile", "desktop"],
        )

        assert isinstance(result, ToolResult)
        assert len(result.content) == 2
        assert result.structured_content["render_path"] == "wall-panel/home"
        assert result.structured_content["screenshot_count"] == 2
        assert [
            item["content_index"] for item in result.structured_content["screenshots"]
        ] == [0, 1]

    async def test_legacy_full_page_fallback_surfaces_warning(
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot.paths import DashboardRenderTarget
        from ha_mcp.tools import tools_dashboard_screenshot as mod

        async def resolve(*_a: Any, **_kw: Any) -> DashboardRenderTarget:
            return DashboardRenderTarget(
                dashboard_url_path="wall-panel",
                view_path="home",
                render_path="wall-panel/home",
                view_index=0,
                stable=True,
            )

        async def capture(*_a: Any, **_kw: Any) -> list[Any]:
            return [
                _fake_dashboard_capture(height=4096, legacy_full_page_fallback=True)
            ]

        monkeypatch.setattr(mod, "resolve_dashboard_render_target", resolve)
        monkeypatch.setattr(mod, "capture_dashboard_images", capture)

        result = await mod.DashboardScreenshotTools(
            object()
        ).ha_get_dashboard_screenshot(
            dashboard_url_path="wall-panel", view_path="home", full_page=True
        )

        assert any(
            "4096" in warning for warning in result.structured_content["warnings"]
        )
        assert (
            result.structured_content["screenshots"][0]["local_capture_options"][
                "legacy_full_page_fallback"
            ]
            is True
        )

    async def test_partial_batch_returns_image_and_failure_metadata(
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot.paths import DashboardRenderTarget
        from ha_mcp.tools import tools_dashboard_screenshot as mod

        async def resolve(*_a: Any, **_kw: Any) -> DashboardRenderTarget:
            return DashboardRenderTarget(
                dashboard_url_path="wall-panel",
                view_path="home",
                render_path="wall-panel/home",
                view_index=0,
                stable=True,
            )

        async def capture(*_a: Any, **kwargs: Any) -> list[Any]:
            kwargs["partial_failures"].append(
                {
                    "success": False,
                    "error": {"code": "TIMEOUT_API_REQUEST", "message": "timeout"},
                    "capture_index": 1,
                    "preset": "desktop",
                }
            )
            return [_fake_dashboard_capture(width=390, height=844, preset="mobile")]

        monkeypatch.setattr(mod, "resolve_dashboard_render_target", resolve)
        monkeypatch.setattr(mod, "capture_dashboard_images", capture)

        result = await mod.DashboardScreenshotTools(
            object()
        ).ha_get_dashboard_screenshot(
            dashboard_url_path="wall-panel",
            view_path="home",
            viewport_presets=["mobile", "desktop"],
        )

        assert len(result.content) == 1
        assert result.structured_content["partial"] is True
        assert result.structured_content["screenshot_failures"][0]["preset"] == (
            "desktop"
        )

    async def test_puppet_restart_only_returns_management_success(
        self, monkeypatch: Any
    ) -> None:
        from fastmcp.tools.tool import ToolResult

        from ha_mcp.dashboard_screenshot import provision
        from ha_mcp.tools import tools_dashboard_screenshot as mod

        async def configure(*_a: Any, **_kw: Any) -> dict[str, Any]:
            return {
                "slug": "abc_puppet",
                "settings_changed": False,
                "restart_requested": True,
                "restart_verified": True,
                "status": "restarted",
            }

        monkeypatch.setattr(provision, "configure_puppet_addon", configure)

        result = await mod.DashboardScreenshotTools(
            object()
        ).ha_get_dashboard_screenshot(puppet_restart=True)

        assert isinstance(result, ToolResult)
        assert result.content == []
        assert result.structured_content["action"] == "configure_puppet"
        assert result.structured_content["screenshot_count"] == 0

    async def test_management_only_rejects_render_options_before_change(
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import provision
        from ha_mcp.tools import tools_dashboard_screenshot as mod

        configure = AsyncMock()
        monkeypatch.setattr(provision, "configure_puppet_addon", configure)

        with pytest.raises(ToolError) as exc_info:
            await mod.DashboardScreenshotTools(object()).ha_get_dashboard_screenshot(
                puppet_keep_browser_open=True,
                theme="Test Theme",
            )

        assert "require a dashboard target" in str(exc_info.value)
        configure.assert_not_awaited()

    async def test_invalid_capture_options_do_not_change_puppet(
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import provision
        from ha_mcp.dashboard_screenshot.paths import DashboardRenderTarget
        from ha_mcp.tools import tools_dashboard_screenshot as mod

        async def resolve(*_a: Any, **_kw: Any) -> DashboardRenderTarget:
            return DashboardRenderTarget(
                dashboard_url_path="wall-panel",
                view_path="home",
                render_path="wall-panel/home",
                view_index=0,
                stable=True,
            )

        configure = AsyncMock()
        monkeypatch.setattr(mod, "resolve_dashboard_render_target", resolve)
        monkeypatch.setattr(provision, "configure_puppet_addon", configure)

        with pytest.raises(ToolError):
            await mod.DashboardScreenshotTools(object()).ha_get_dashboard_screenshot(
                dashboard_url_path="wall-panel",
                view_path="home",
                viewport_presets=["mobile", "mobile"],
                puppet_keep_browser_open=True,
            )

        configure.assert_not_awaited()

    async def test_capture_failure_reports_applied_puppet_configuration(
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import provision
        from ha_mcp.dashboard_screenshot.paths import DashboardRenderTarget
        from ha_mcp.tools import tools_dashboard_screenshot as mod

        async def resolve(*_a: Any, **_kw: Any) -> DashboardRenderTarget:
            return DashboardRenderTarget(
                dashboard_url_path="wall-panel",
                view_path="home",
                render_path="wall-panel/home",
                view_index=0,
                stable=True,
            )

        applied = {
            "slug": "abc_puppet",
            "settings_changed": True,
            "restart_requested": False,
            "status": "pending_restart",
        }

        async def configure(*_a: Any, **_kw: Any) -> dict[str, Any]:
            return applied

        async def capture(*_a: Any, **_kw: Any) -> list[Any]:
            raise ToolError(
                json.dumps(
                    {
                        "success": False,
                        "error": {"code": "CONNECTION_FAILED", "message": "boom"},
                    }
                )
            )

        monkeypatch.setattr(mod, "resolve_dashboard_render_target", resolve)
        monkeypatch.setattr(mod, "capture_dashboard_images", capture)
        monkeypatch.setattr(provision, "configure_puppet_addon", configure)

        with pytest.raises(ToolError) as exc_info:
            await mod.DashboardScreenshotTools(object()).ha_get_dashboard_screenshot(
                dashboard_url_path="wall-panel",
                view_path="home",
                puppet_keep_browser_open=True,
            )

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "CONNECTION_FAILED"
        assert error["puppet_configuration_applied"] == applied

    async def test_raw_capture_failure_reports_applied_puppet_configuration(
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import provision
        from ha_mcp.dashboard_screenshot.paths import DashboardRenderTarget
        from ha_mcp.tools import tools_dashboard_screenshot as mod

        async def resolve(*_a: Any, **_kw: Any) -> DashboardRenderTarget:
            return DashboardRenderTarget(
                dashboard_url_path="wall-panel",
                view_path="home",
                render_path="wall-panel/home",
                view_index=0,
                stable=True,
            )

        applied = {"slug": "abc_puppet", "settings_changed": True}

        async def configure(*_a: Any, **_kw: Any) -> dict[str, Any]:
            return applied

        async def capture(*_a: Any, **_kw: Any) -> list[Any]:
            raise RuntimeError("unexpected capture failure")

        monkeypatch.setattr(mod, "resolve_dashboard_render_target", resolve)
        monkeypatch.setattr(mod, "capture_dashboard_images", capture)
        monkeypatch.setattr(provision, "configure_puppet_addon", configure)

        with pytest.raises(ToolError) as exc_info:
            await mod.DashboardScreenshotTools(object()).ha_get_dashboard_screenshot(
                dashboard_url_path="wall-panel",
                view_path="home",
                puppet_keep_browser_open=True,
            )

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "INTERNAL_ERROR"
        assert error["puppet_configuration_applied"] == applied

    async def test_metadata_failure_reports_applied_puppet_configuration(
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import provision
        from ha_mcp.dashboard_screenshot.paths import DashboardRenderTarget
        from ha_mcp.tools import tools_dashboard_screenshot as mod

        async def resolve(*_a: Any, **_kw: Any) -> DashboardRenderTarget:
            return DashboardRenderTarget(
                dashboard_url_path="wall-panel",
                view_path="home",
                render_path="wall-panel/home",
                view_index=0,
                stable=True,
            )

        applied = {"slug": "abc_puppet", "settings_changed": True}

        async def configure(*_a: Any, **_kw: Any) -> dict[str, Any]:
            return applied

        async def capture(*_a: Any, **_kw: Any) -> list[Any]:
            return [_fake_dashboard_capture()]

        def fail_metadata(*_a: Any, **_kw: Any) -> list[dict[str, Any]]:
            raise RuntimeError("metadata failed")

        monkeypatch.setattr(mod, "resolve_dashboard_render_target", resolve)
        monkeypatch.setattr(mod, "capture_dashboard_images", capture)
        monkeypatch.setattr(mod, "dashboard_screenshot_metadata", fail_metadata)
        monkeypatch.setattr(provision, "configure_puppet_addon", configure)

        with pytest.raises(ToolError) as exc_info:
            await mod.DashboardScreenshotTools(object()).ha_get_dashboard_screenshot(
                dashboard_url_path="wall-panel",
                view_path="home",
                puppet_keep_browser_open=True,
            )

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "IMAGE_SERIALIZATION_FAILED"
        assert error["puppet_configuration_applied"] == applied


# ---------------------------------------------------------------------------
# Engine-URL resolution
# ---------------------------------------------------------------------------


class TestResolveEngineUrl:
    async def test_explicit_url_strips_trailing_slash(self, monkeypatch: Any) -> None:
        from ha_mcp.dashboard_screenshot import provision

        monkeypatch.setattr(
            config,
            "get_global_settings",
            lambda: SimpleNamespace(
                dashboard_screenshot_engine_url="http://engine:10000/"
            ),
        )
        assert await provision.resolve_engine_url() == "http://engine:10000"

    async def test_stdio_no_token_raises(self, monkeypatch: Any) -> None:
        from ha_mcp.dashboard_screenshot import provision

        monkeypatch.setattr(
            config,
            "get_global_settings",
            lambda: SimpleNamespace(dashboard_screenshot_engine_url=""),
        )
        with pytest.raises(ToolError):
            await provision.resolve_engine_url()


# ---------------------------------------------------------------------------
# Supervisor engine discovery (mode 2)
# ---------------------------------------------------------------------------


class _FakeSupResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
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
            await provision._discover_engine_url_via_supervisor()
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
        url = await provision._discover_engine_url_via_supervisor()
        assert url == "http://def-puppet:10000"

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
            await provision._discover_engine_url_via_supervisor()

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
            await provision._discover_engine_url_via_supervisor()
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
            await provision._discover_engine_url_via_supervisor()
        assert "not started" in str(exc.value).lower()

    async def test_supervisor_http_error_raises_connection_failed(
        self, monkeypatch: Any
    ) -> None:
        import httpx

        from ha_mcp.dashboard_screenshot import provision

        _patch_supervisor(monkeypatch, {"/addons": httpx.ConnectError("boom")})
        with pytest.raises(ToolError) as exc:
            await provision._discover_engine_url_via_supervisor()
        assert "supervisor" in str(exc.value).lower()


class TestConfigurePuppetAddon:
    @pytest.fixture(autouse=True)
    def _supervisor_environment(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("SUPERVISOR_TOKEN", "test-supervisor-token")

    async def test_no_supervisor_token_fails_before_api_call(
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import provision
        from ha_mcp.tools import tools_addons

        supervisor_call = AsyncMock()
        monkeypatch.delenv("SUPERVISOR_TOKEN")
        monkeypatch.setattr(
            config,
            "get_global_settings",
            lambda: SimpleNamespace(dashboard_screenshot_engine_url=""),
        )
        monkeypatch.setattr(tools_addons, "_supervisor_api_call", supervisor_call)

        with pytest.raises(ToolError) as exc_info:
            await provision.configure_puppet_addon(
                object(), keep_browser_open=True, restart=False
            )

        assert "auto-discovery" in str(exc_info.value)
        supervisor_call.assert_not_awaited()

    async def test_explicit_engine_fails_before_api_call(
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import provision
        from ha_mcp.tools import tools_addons

        supervisor_call = AsyncMock()
        monkeypatch.setattr(
            config,
            "get_global_settings",
            lambda: SimpleNamespace(
                dashboard_screenshot_engine_url="http://sidecar:10000"
            ),
        )
        monkeypatch.setattr(tools_addons, "_supervisor_api_call", supervisor_call)

        with pytest.raises(ToolError) as exc_info:
            await provision.configure_puppet_addon(
                object(), keep_browser_open=True, restart=False
            )

        assert "auto-discovery" in str(exc_info.value)
        supervisor_call.assert_not_awaited()

    async def test_multiple_started_puppets_fail_closed(self, monkeypatch: Any) -> None:
        from ha_mcp.dashboard_screenshot import provision
        from ha_mcp.tools import tools_addons

        calls: list[tuple[str, str]] = []

        async def supervisor_call(
            _client: Any,
            endpoint: str,
            method: str = "GET",
            **_kwargs: Any,
        ) -> dict[str, Any]:
            calls.append((endpoint, method))
            if endpoint == "/addons":
                return {
                    "success": True,
                    "result": {
                        "addons": [
                            {"slug": "abc_puppet"},
                            {"slug": "def_puppet"},
                        ]
                    },
                }
            return {
                "success": True,
                "result": {
                    "name": "Puppet",
                    "state": "started",
                    "schema": _PUPPET_SCHEMA,
                    "options": _puppet_options(keep_browser_open=False),
                },
            }

        monkeypatch.setattr(tools_addons, "_supervisor_api_call", supervisor_call)

        with pytest.raises(ToolError) as exc_info:
            await provision.configure_puppet_addon(
                object(), keep_browser_open=True, restart=False
            )

        assert "ambiguous" in str(exc_info.value)
        assert all(method == "GET" for _, method in calls)

    async def test_incomplete_options_fail_before_write(self, monkeypatch: Any) -> None:
        from ha_mcp.dashboard_screenshot import provision
        from ha_mcp.tools import tools_addons

        calls: list[tuple[str, str]] = []

        async def supervisor_call(
            _client: Any,
            endpoint: str,
            method: str = "GET",
            **_kwargs: Any,
        ) -> dict[str, Any]:
            calls.append((endpoint, method))
            if endpoint == "/addons":
                return {
                    "success": True,
                    "result": {"addons": [{"slug": "abc_puppet"}]},
                }
            return {
                "success": True,
                "result": {
                    "name": "Puppet",
                    "state": "started",
                    "schema": _PUPPET_SCHEMA,
                    "options": {"keep_browser_open": False},
                },
            }

        monkeypatch.setattr(tools_addons, "_supervisor_api_call", supervisor_call)

        with pytest.raises(ToolError) as exc_info:
            await provision.configure_puppet_addon(
                object(), keep_browser_open=True, restart=False
            )

        error = json.loads(str(exc_info.value))
        assert error["missing_options"] == ["access_token", "home_assistant_url"]
        assert all(method == "GET" for _, method in calls)

    @pytest.mark.parametrize(
        ("options", "expected_text"),
        [
            (None, "complete options object"),
            (
                {
                    "access_token": 123,
                    "home_assistant_url": None,
                    "keep_browser_open": "false",
                },
                "unexpected Puppet option types",
            ),
        ],
    )
    async def test_malformed_options_fail_before_write(
        self, monkeypatch: Any, options: Any, expected_text: str
    ) -> None:
        from ha_mcp.dashboard_screenshot import provision
        from ha_mcp.tools import tools_addons

        calls: list[tuple[str, str]] = []

        async def supervisor_call(
            _client: Any,
            endpoint: str,
            method: str = "GET",
            **_kwargs: Any,
        ) -> dict[str, Any]:
            calls.append((endpoint, method))
            if endpoint == "/addons":
                return {
                    "success": True,
                    "result": {"addons": [{"slug": "abc_puppet"}]},
                }
            return {
                "success": True,
                "result": {
                    "name": "Puppet",
                    "state": "started",
                    "schema": _PUPPET_SCHEMA,
                    "options": options,
                },
            }

        monkeypatch.setattr(tools_addons, "_supervisor_api_call", supervisor_call)

        with pytest.raises(ToolError) as exc_info:
            await provision.configure_puppet_addon(
                object(), keep_browser_open=True, restart=False
            )

        assert expected_text in str(exc_info.value)
        assert all(method == "GET" for _, method in calls)

    async def test_setting_update_is_merged_and_hard_scoped_to_puppet(
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import provision
        from ha_mcp.tools import tools_addons

        calls: list[dict[str, Any]] = []

        async def supervisor_call(
            _client: Any,
            endpoint: str,
            method: str = "GET",
            data: dict[str, Any] | None = None,
            timeout: int | None = None,
        ) -> dict[str, Any]:
            calls.append(
                {
                    "endpoint": endpoint,
                    "method": method,
                    "data": data,
                    "timeout": timeout,
                }
            )
            if endpoint == "/addons":
                return {
                    "success": True,
                    "result": {
                        "addons": [
                            {"slug": "other_addon", "state": "started"},
                            {"slug": "abc_puppet", "state": "started"},
                        ]
                    },
                }
            if endpoint == "/addons/abc_puppet/info":
                return {
                    "success": True,
                    "result": {
                        "name": "Puppet",
                        "state": "started",
                        "schema": _PUPPET_SCHEMA,
                        "options": _puppet_options(keep_browser_open=False),
                    },
                }
            assert endpoint == "/addons/abc_puppet/options"
            return {"success": True, "result": {}}

        monkeypatch.setattr(tools_addons, "_supervisor_api_call", supervisor_call)

        result = await provision.configure_puppet_addon(
            object(), keep_browser_open=True, restart=False
        )

        assert result["settings_changed"] is True
        assert result["status"] == "pending_restart"
        assert all("other_addon" not in call["endpoint"] for call in calls)
        post = calls[-1]
        assert post["endpoint"] == "/addons/abc_puppet/options"
        assert post["method"] == "POST"
        assert post["data"] == {
            "options": {
                "access_token": "secret",
                "keep_browser_open": True,
                "home_assistant_url": "http://homeassistant:8123",
            }
        }
        assert "access_token" not in result
        assert "home_assistant_url" not in result

    async def test_restart_is_verified_started(self, monkeypatch: Any) -> None:
        from ha_mcp.dashboard_screenshot import provision
        from ha_mcp.tools import tools_addons

        calls: list[tuple[str, str, int | None]] = []
        info_calls = 0

        async def supervisor_call(
            _client: Any,
            endpoint: str,
            method: str = "GET",
            data: dict[str, Any] | None = None,
            timeout: int | None = None,
        ) -> dict[str, Any]:
            nonlocal info_calls
            calls.append((endpoint, method, timeout))
            if endpoint == "/addons":
                return {
                    "success": True,
                    "result": {"addons": [{"slug": "abc_puppet", "state": "started"}]},
                }
            if endpoint == "/addons/abc_puppet/info":
                info_calls += 1
                return {
                    "success": True,
                    "result": {
                        "name": "Puppet",
                        "state": "stopped" if info_calls == 2 else "started",
                        "hostname": "puppet-engine",
                        "schema": _PUPPET_SCHEMA,
                        "options": _puppet_options(keep_browser_open=False),
                    },
                }
            assert endpoint == "/addons/abc_puppet/restart"
            return {"success": True, "result": {}}

        monkeypatch.setattr(tools_addons, "_supervisor_api_call", supervisor_call)
        monkeypatch.setattr(provision.asyncio, "sleep", AsyncMock())
        ready = AsyncMock()
        monkeypatch.setattr(provision, "_wait_for_puppet_engine_ready", ready)

        result = await provision.configure_puppet_addon(
            object(), keep_browser_open=None, restart=True
        )

        assert result["restart_verified"] is True
        assert result["status"] == "restarted"
        assert ("/addons/abc_puppet/restart", "POST", 120) in calls
        assert info_calls == 3
        ready.assert_awaited_once_with("puppet-engine")

    async def test_restart_waits_until_engine_root_is_ready(
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import provision
        from ha_mcp.tools import tools_addons

        async def supervisor_call(
            _client: Any,
            endpoint: str,
            method: str = "GET",
            **_kwargs: Any,
        ) -> dict[str, Any]:
            if endpoint == "/addons":
                return {"success": True, "result": {"addons": [{"slug": "abc_puppet"}]}}
            if endpoint == "/addons/abc_puppet/info":
                return {
                    "success": True,
                    "result": {
                        "name": "Puppet",
                        "state": "started",
                        "hostname": "puppet-engine",
                        "schema": _PUPPET_SCHEMA,
                        "options": _puppet_options(keep_browser_open=False),
                    },
                }
            assert endpoint == "/addons/abc_puppet/restart"
            return {"success": True, "result": {}}

        outcomes: list[Exception | int] = [
            provision.httpx.ConnectError("not listening"),
            503,
            200,
        ]

        class ProbeClient:
            async def __aenter__(self) -> ProbeClient:
                return self

            async def __aexit__(self, *_args: Any) -> None:
                return None

            async def get(self, _url: str) -> Any:
                outcome = outcomes.pop(0)
                if isinstance(outcome, Exception):
                    raise outcome
                return SimpleNamespace(status_code=outcome)

        monkeypatch.setattr(tools_addons, "_supervisor_api_call", supervisor_call)
        monkeypatch.setattr(
            provision.httpx, "AsyncClient", lambda **_kwargs: ProbeClient()
        )
        sleep = AsyncMock()
        monkeypatch.setattr(provision.asyncio, "sleep", sleep)

        result = await provision.configure_puppet_addon(
            object(), keep_browser_open=None, restart=True
        )

        assert result["restart_verified"] is True
        assert outcomes == []
        assert sleep.await_count == 2
        sleep.assert_awaited_with(provision._PUPPET_ENGINE_READY_POLL_INTERVAL_SECONDS)

    async def test_restart_readiness_timeout_preserves_applied_context(
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import provision
        from ha_mcp.tools import tools_addons

        async def supervisor_call(
            _client: Any,
            endpoint: str,
            method: str = "GET",
            **_kwargs: Any,
        ) -> dict[str, Any]:
            if endpoint == "/addons":
                return {"success": True, "result": {"addons": [{"slug": "abc_puppet"}]}}
            if endpoint == "/addons/abc_puppet/info":
                return {
                    "success": True,
                    "result": {
                        "name": "Puppet",
                        "state": "started",
                        "hostname": "puppet-engine",
                        "schema": _PUPPET_SCHEMA,
                        "options": _puppet_options(keep_browser_open=False),
                    },
                }
            if endpoint == "/addons/abc_puppet/options":
                return {"success": True, "result": {}}
            assert endpoint == "/addons/abc_puppet/restart"
            return {"success": True, "result": {}}

        monkeypatch.setattr(tools_addons, "_supervisor_api_call", supervisor_call)

        class UnreadyClient:
            async def __aenter__(self) -> UnreadyClient:
                return self

            async def __aexit__(self, *_args: Any) -> None:
                return None

            async def get(self, _url: str) -> Any:
                raise provision.httpx.ConnectError("not listening")

        monkeypatch.setattr(provision, "_PUPPET_ENGINE_READY_TIMEOUT_SECONDS", 0)
        monkeypatch.setattr(
            provision.httpx, "AsyncClient", lambda **_kwargs: UnreadyClient()
        )
        monkeypatch.setattr(provision.asyncio, "sleep", AsyncMock())

        with pytest.raises(ToolError) as exc_info:
            await provision.configure_puppet_addon(
                object(), keep_browser_open=True, restart=True
            )

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "CONNECTION_FAILED"
        assert error["engine_ready"] is False
        assert error["settings_changed"] is True
        assert error["restart_requested"] is True

    async def test_schema_mismatch_fails_before_any_write(
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import provision
        from ha_mcp.tools import tools_addons

        calls: list[str] = []

        async def supervisor_call(
            _client: Any,
            endpoint: str,
            method: str = "GET",
            data: dict[str, Any] | None = None,
            timeout: int | None = None,
        ) -> dict[str, Any]:
            calls.append(endpoint)
            if endpoint == "/addons":
                return {
                    "success": True,
                    "result": {"addons": [{"slug": "fake_puppet", "state": "started"}]},
                }
            return {
                "success": True,
                "result": {
                    "name": "Not Puppet",
                    "schema": [{"name": "keep_browser_open"}],
                    "options": {},
                },
            }

        monkeypatch.setattr(tools_addons, "_supervisor_api_call", supervisor_call)

        with pytest.raises(ToolError) as exc_info:
            await provision.configure_puppet_addon(
                object(), keep_browser_open=True, restart=True
            )

        assert "CONFIG_VALIDATION_FAILED" in str(exc_info.value)
        assert calls == ["/addons", "/addons/fake_puppet/info"]

    async def test_unchanged_value_skips_options_post(self, monkeypatch: Any) -> None:
        from ha_mcp.dashboard_screenshot import provision
        from ha_mcp.tools import tools_addons

        calls: list[tuple[str, str]] = []

        async def supervisor_call(
            _client: Any,
            endpoint: str,
            method: str = "GET",
            data: dict[str, Any] | None = None,
            timeout: int | None = None,
        ) -> dict[str, Any]:
            calls.append((endpoint, method))
            if endpoint == "/addons":
                return {
                    "success": True,
                    "result": {"addons": [{"slug": "abc_puppet", "state": "started"}]},
                }
            assert endpoint == "/addons/abc_puppet/info"
            return {
                "success": True,
                "result": {
                    "name": "Puppet",
                    "state": "started",
                    "schema": _PUPPET_SCHEMA,
                    "options": _puppet_options(keep_browser_open=True),
                },
            }

        monkeypatch.setattr(tools_addons, "_supervisor_api_call", supervisor_call)

        result = await provision.configure_puppet_addon(
            object(), keep_browser_open=True, restart=False
        )

        assert result["settings_changed"] is False
        assert result["status"] == "unchanged"
        assert calls == [("/addons", "GET"), ("/addons/abc_puppet/info", "GET")]

    async def test_restart_timeout_reports_prior_setting_change(
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import provision
        from ha_mcp.tools import tools_addons

        info_calls = 0

        async def supervisor_call(
            _client: Any,
            endpoint: str,
            method: str = "GET",
            data: dict[str, Any] | None = None,
            timeout: int | None = None,
        ) -> dict[str, Any]:
            nonlocal info_calls
            if endpoint == "/addons":
                return {
                    "success": True,
                    "result": {"addons": [{"slug": "abc_puppet", "state": "started"}]},
                }
            if endpoint == "/addons/abc_puppet/info":
                info_calls += 1
                return {
                    "success": True,
                    "result": {
                        "name": "Puppet",
                        "state": "started" if info_calls == 1 else "stopped",
                        "hostname": "puppet-engine",
                        "schema": _PUPPET_SCHEMA,
                        "options": _puppet_options(keep_browser_open=False),
                    },
                }
            assert endpoint in {
                "/addons/abc_puppet/options",
                "/addons/abc_puppet/restart",
            }
            return {"success": True, "result": {}}

        monkeypatch.setattr(tools_addons, "_supervisor_api_call", supervisor_call)
        monkeypatch.setattr(provision.asyncio, "sleep", AsyncMock())

        with pytest.raises(ToolError) as exc_info:
            await provision.configure_puppet_addon(
                object(), keep_browser_open=True, restart=True
            )

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "SERVICE_CALL_FAILED"
        assert error["settings_changed"] is True
        assert error["restart_requested"] is True
        assert info_calls == 21

    async def test_restart_poll_tool_error_preserves_restart_context(
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import provision
        from ha_mcp.tools import tools_addons

        info_calls = 0

        async def supervisor_call(
            _client: Any,
            endpoint: str,
            method: str = "GET",
            **_kwargs: Any,
        ) -> dict[str, Any]:
            nonlocal info_calls
            if endpoint == "/addons":
                return {
                    "success": True,
                    "result": {"addons": [{"slug": "abc_puppet"}]},
                }
            if endpoint == "/addons/abc_puppet/info":
                info_calls += 1
                if info_calls > 1:
                    raise ToolError(
                        json.dumps(
                            {
                                "success": False,
                                "error": {
                                    "code": "CONNECTION_FAILED",
                                    "message": "poll disconnected",
                                },
                            }
                        )
                    )
                return {
                    "success": True,
                    "result": {
                        "name": "Puppet",
                        "state": "started",
                        "hostname": "puppet-engine",
                        "schema": _PUPPET_SCHEMA,
                        "options": _puppet_options(keep_browser_open=False),
                    },
                }
            assert endpoint == "/addons/abc_puppet/restart"
            return {"success": True, "result": {}}

        monkeypatch.setattr(tools_addons, "_supervisor_api_call", supervisor_call)

        with pytest.raises(ToolError) as exc_info:
            await provision.configure_puppet_addon(
                object(), keep_browser_open=None, restart=True
            )

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "CONNECTION_FAILED"
        assert error["settings_changed"] is False
        assert error["restart_requested"] is True
        assert info_calls == 2

    async def test_restart_without_hostname_fails_before_options_write(
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import provision
        from ha_mcp.tools import tools_addons

        calls: list[tuple[str, str]] = []

        async def supervisor_call(
            _client: Any,
            endpoint: str,
            method: str = "GET",
            **_kwargs: Any,
        ) -> dict[str, Any]:
            calls.append((endpoint, method))
            if endpoint == "/addons":
                return {"success": True, "result": {"addons": [{"slug": "abc_puppet"}]}}
            assert endpoint == "/addons/abc_puppet/info"
            return {
                "success": True,
                "result": {
                    "name": "Puppet",
                    "state": "started",
                    "schema": _PUPPET_SCHEMA,
                    "options": _puppet_options(keep_browser_open=False),
                },
            }

        monkeypatch.setattr(tools_addons, "_supervisor_api_call", supervisor_call)

        with pytest.raises(ToolError) as exc_info:
            await provision.configure_puppet_addon(
                object(), keep_browser_open=True, restart=True
            )

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "SERVICE_CALL_FAILED"
        assert error["engine_ready"] is False
        assert calls == [("/addons", "GET"), ("/addons/abc_puppet/info", "GET")]


# ---------------------------------------------------------------------------
# Capture HTTP client
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(
        self,
        status_code: int,
        content: bytes,
        text: str = "",
        content_type: str = "image/png",
        content_length: int | None = None,
        chunks: list[bytes] | None = None,
    ) -> None:
        self.status_code = status_code
        self.content = content
        self.text = text
        self.headers = {"content-type": content_type}
        if content_length is not None:
            self.headers["content-length"] = str(content_length)
        self._chunks = chunks
        self.iterated = False

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        self.iterated = True
        chunks = self._chunks if self._chunks is not None else [self.content]
        for chunk in chunks:
            yield chunk


class _FakeStreamContext:
    def __init__(self, response: _FakeResponse | Exception) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeResponse:
        if isinstance(self._response, Exception):
            raise self._response
        return self._response

    async def __aexit__(self, *_a: Any) -> None:
        return None


class _FakeAsyncClient:
    """Minimal async-context httpx.AsyncClient stand-in."""

    last_get: ClassVar[dict[str, Any]] = {}
    gets: ClassVar[list[dict[str, Any]]] = []
    _next: ClassVar[_FakeResponse | Exception | list[_FakeResponse | Exception]]

    def __init__(self, *_a: Any, **_kw: Any) -> None:
        pass

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *_a: Any) -> None:
        return None

    def stream(
        self, method: str, url: str, params: dict[str, Any] | None = None
    ) -> _FakeStreamContext:
        _FakeAsyncClient.last_get = {
            "url": url,
            "params": dict(params) if params is not None else None,
        }
        _FakeAsyncClient.gets.append(_FakeAsyncClient.last_get)
        next_response = _FakeAsyncClient._next
        if isinstance(next_response, list):
            next_response = next_response.pop(0)
        return _FakeStreamContext(next_response)


class TestCapture:
    async def test_builds_url_and_returns_png(self, monkeypatch: Any) -> None:
        from ha_mcp.dashboard_screenshot import capture

        async def fake_resolve() -> str:
            return "http://engine:10000"

        monkeypatch.setattr(capture, "resolve_engine_url", fake_resolve)
        png = b"\x89PNG\r\n\x1a\nfake"
        _FakeAsyncClient._next = _FakeResponse(200, png)
        monkeypatch.setattr(capture.httpx, "AsyncClient", _FakeAsyncClient)

        out = await capture.capture_dashboard_png(
            "lovelace/0", width=640, height=480, zoom=1.5, wait_ms=2000
        )
        assert out == png
        got = _FakeAsyncClient.last_get
        assert got["url"] == "http://engine:10000/lovelace/0"
        assert got["params"]["viewport"] == "640x480"
        assert got["params"]["zoom"] == "1.5"
        assert got["params"]["wait"] == "2000"
        assert got["params"]["format"] == "png"

    async def test_full_page_uses_native_auto_height(self, monkeypatch: Any) -> None:
        """full_page=True uses Puppet's content-sized viewport request."""
        from ha_mcp.dashboard_screenshot import capture

        async def fake_resolve() -> str:
            return "http://engine:10000"

        monkeypatch.setattr(capture, "resolve_engine_url", fake_resolve)
        _FakeAsyncClient._next = _FakeResponse(200, b"\x89PNG\r\n\x1a\nfake")
        monkeypatch.setattr(capture.httpx, "AsyncClient", _FakeAsyncClient)

        await capture.capture_dashboard_png(
            "lovelace/0", width=1024, height=480, full_page=True
        )
        got = _FakeAsyncClient.last_get
        assert got["params"]["viewport"] == "1024xauto"

    async def test_auto_height_and_full_page_are_true_custom_aliases(
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture

        async def fake_resolve() -> str:
            return "http://engine:10000"

        monkeypatch.setattr(capture, "resolve_engine_url", fake_resolve)
        _FakeAsyncClient.gets = []
        _FakeAsyncClient._next = _FakeResponse(200, _PNG)
        monkeypatch.setattr(capture.httpx, "AsyncClient", _FakeAsyncClient)

        explicit = await capture.capture_dashboard_images(
            "lovelace/0", width=700, height="auto"
        )
        alias_a = await capture.capture_dashboard_images(
            "lovelace/0", width=700, height=480, full_page=True
        )
        alias_b = await capture.capture_dashboard_images(
            "lovelace/0", width=700, height=1600, full_page=True
        )

        assert [request["params"]["viewport"] for request in _FakeAsyncClient.gets] == [
            "700xauto",
            "700xauto",
            "700xauto",
        ]
        assert [(item.width, item.height, item.orientation) for item in explicit] == [
            (700, "auto", None)
        ]
        assert [(item.width, item.height, item.orientation) for item in alias_a] == [
            (700, "auto", None)
        ]
        assert [(item.width, item.height, item.orientation) for item in alias_b] == [
            (700, "auto", None)
        ]

    async def test_custom_auto_height_rejects_orientation(self) -> None:
        from ha_mcp.dashboard_screenshot import capture

        with pytest.raises(ToolError) as exc_info:
            await capture.capture_dashboard_images(
                "lovelace/0", height="auto", orientation="landscape"
            )

        assert "custom auto-height viewport" in str(exc_info.value)

    async def test_preset_full_page_keeps_orientation_support(
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture

        async def fake_resolve() -> str:
            return "http://engine:10000"

        monkeypatch.setattr(capture, "resolve_engine_url", fake_resolve)
        _FakeAsyncClient._next = _FakeResponse(200, _PNG)
        monkeypatch.setattr(capture.httpx, "AsyncClient", _FakeAsyncClient)

        result = await capture.capture_dashboard_images(
            "lovelace/0",
            viewport_presets=["mobile"],
            orientation="landscape",
            full_page=True,
        )

        assert _FakeAsyncClient.last_get["params"]["viewport"] == "844xauto"
        assert (result[0].width, result[0].height, result[0].orientation) == (
            844,
            "auto",
            "landscape",
        )

    async def test_full_page_retries_legacy_fixed_height_on_empty_400(
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture

        async def fake_resolve() -> str:
            return "http://engine:10000"

        monkeypatch.setattr(capture, "resolve_engine_url", fake_resolve)
        _FakeAsyncClient.gets = []
        _FakeAsyncClient._next = [
            _FakeResponse(400, b"", content_type="text/plain"),
            _FakeResponse(200, _PNG),
        ]
        monkeypatch.setattr(capture.httpx, "AsyncClient", _FakeAsyncClient)

        result = await capture.capture_dashboard_images(
            "lovelace/0", width=900, full_page=True
        )

        assert [request["params"]["viewport"] for request in _FakeAsyncClient.gets] == [
            "900xauto",
            "900x4096",
        ]
        assert result[0].height == 4096
        assert result[0].requested["legacy_full_page_fallback"] is True

    async def test_explicit_auto_does_not_use_legacy_full_page_fallback(
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture

        async def fake_resolve() -> str:
            return "http://engine:10000"

        monkeypatch.setattr(capture, "resolve_engine_url", fake_resolve)
        _FakeAsyncClient.gets = []
        _FakeAsyncClient._next = _FakeResponse(400, b"", content_type="text/plain")
        monkeypatch.setattr(capture.httpx, "AsyncClient", _FakeAsyncClient)

        with pytest.raises(ToolError):
            await capture.capture_dashboard_images("lovelace/0", height="auto")

        assert len(_FakeAsyncClient.gets) == 1

    async def test_declared_oversize_rejected_before_body_read(
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture

        async def fake_resolve() -> str:
            return "http://engine:10000"

        response = _FakeResponse(200, b"12345", content_length=5)
        monkeypatch.setattr(capture, "resolve_engine_url", fake_resolve)
        monkeypatch.setattr(capture, "MAX_IMAGE_PAYLOAD_BYTES", 4)
        monkeypatch.setattr(capture, "MAX_BATCH_PAYLOAD_BYTES", 8)
        _FakeAsyncClient._next = response
        monkeypatch.setattr(capture.httpx, "AsyncClient", _FakeAsyncClient)

        with pytest.raises(ToolError) as exc_info:
            await capture.capture_dashboard_images("lovelace/0")

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "IMAGE_PAYLOAD_TOO_LARGE"
        assert error["limit_kind"] == "image"
        assert response.iterated is False

    async def test_chunked_oversize_stops_at_limit(self, monkeypatch: Any) -> None:
        from ha_mcp.dashboard_screenshot import capture

        async def fake_resolve() -> str:
            return "http://engine:10000"

        monkeypatch.setattr(capture, "resolve_engine_url", fake_resolve)
        monkeypatch.setattr(capture, "MAX_IMAGE_PAYLOAD_BYTES", 4)
        monkeypatch.setattr(capture, "MAX_BATCH_PAYLOAD_BYTES", 8)
        _FakeAsyncClient._next = _FakeResponse(200, b"", chunks=[b"123", b"45"])
        monkeypatch.setattr(capture.httpx, "AsyncClient", _FakeAsyncClient)

        with pytest.raises(ToolError) as exc_info:
            await capture.capture_dashboard_images("lovelace/0")

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "IMAGE_PAYLOAD_TOO_LARGE"
        assert error["received_bytes"] == 5

    async def test_exact_payload_boundary_succeeds(self, monkeypatch: Any) -> None:
        from ha_mcp.dashboard_screenshot import capture

        async def fake_resolve() -> str:
            return "http://engine:10000"

        monkeypatch.setattr(capture, "resolve_engine_url", fake_resolve)
        monkeypatch.setattr(capture, "MAX_IMAGE_PAYLOAD_BYTES", 4)
        monkeypatch.setattr(capture, "MAX_BATCH_PAYLOAD_BYTES", 8)
        _FakeAsyncClient._next = _FakeResponse(
            200, _BMP, content_type="image/bmp", content_length=4
        )
        monkeypatch.setattr(capture.httpx, "AsyncClient", _FakeAsyncClient)

        result = await capture.capture_dashboard_images(
            "lovelace/0", image_format="bmp"
        )

        assert result[0].data == _BMP

    async def test_batch_limit_identifies_second_capture(
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture

        async def fake_resolve() -> str:
            return "http://engine:10000"

        monkeypatch.setattr(capture, "resolve_engine_url", fake_resolve)
        monkeypatch.setattr(capture, "MAX_IMAGE_PAYLOAD_BYTES", 4)
        monkeypatch.setattr(capture, "MAX_BATCH_PAYLOAD_BYTES", 6)
        _FakeAsyncClient._next = _FakeResponse(
            200, _BMP, content_type="image/bmp", content_length=4
        )
        monkeypatch.setattr(capture.httpx, "AsyncClient", _FakeAsyncClient)

        with pytest.raises(ToolError) as exc_info:
            await capture.capture_dashboard_images(
                "lovelace/0",
                viewport_presets=["mobile", "desktop"],
                image_format="bmp",
            )

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "IMAGE_PAYLOAD_TOO_LARGE"
        assert error["limit_kind"] == "batch"
        assert error["capture_index"] == 1
        assert error["completed_count"] == 1
        assert error["preset"] == "desktop"

    async def test_partial_batch_retains_success_and_structured_failure(
        self, monkeypatch: Any
    ) -> None:
        import httpx

        from ha_mcp.dashboard_screenshot import capture

        async def fake_resolve() -> str:
            return "http://engine:10000"

        monkeypatch.setattr(capture, "resolve_engine_url", fake_resolve)
        _FakeAsyncClient._next = [
            _FakeResponse(200, _PNG),
            httpx.ReadTimeout("desktop timed out"),
        ]
        monkeypatch.setattr(capture.httpx, "AsyncClient", _FakeAsyncClient)
        failures: list[dict[str, Any]] = []

        result = await capture.capture_dashboard_images(
            "lovelace/0",
            viewport_presets=["mobile", "desktop"],
            partial_failures=failures,
        )

        assert [item.preset for item in result] == ["mobile"]
        assert failures[0]["error"]["code"] == "TIMEOUT_API_REQUEST"
        assert failures[0]["capture_index"] == 1
        assert failures[0]["completed_count"] == 1
        assert failures[0]["preset"] == "desktop"

    async def test_partial_batch_continues_after_first_failure(
        self, monkeypatch: Any
    ) -> None:
        import httpx

        from ha_mcp.dashboard_screenshot import capture

        async def fake_resolve() -> str:
            return "http://engine:10000"

        monkeypatch.setattr(capture, "resolve_engine_url", fake_resolve)
        _FakeAsyncClient._next = [
            httpx.ReadTimeout("mobile timed out"),
            _FakeResponse(200, _PNG),
        ]
        monkeypatch.setattr(capture.httpx, "AsyncClient", _FakeAsyncClient)
        failures: list[dict[str, Any]] = []

        result = await capture.capture_dashboard_images(
            "lovelace/0",
            viewport_presets=["mobile", "desktop"],
            partial_failures=failures,
        )

        assert [item.preset for item in result] == ["desktop"]
        assert failures[0]["capture_index"] == 0
        assert failures[0]["completed_count"] == 0
        assert failures[0]["preset"] == "mobile"

    async def test_all_failed_batch_raises_ordered_aggregate(
        self, monkeypatch: Any
    ) -> None:
        import httpx

        from ha_mcp.dashboard_screenshot import capture

        async def fake_resolve() -> str:
            return "http://engine:10000"

        monkeypatch.setattr(capture, "resolve_engine_url", fake_resolve)
        _FakeAsyncClient._next = [
            httpx.ReadTimeout("mobile timed out"),
            httpx.ReadTimeout("desktop timed out"),
        ]
        monkeypatch.setattr(capture.httpx, "AsyncClient", _FakeAsyncClient)
        failures: list[dict[str, Any]] = []

        with pytest.raises(ToolError) as exc_info:
            await capture.capture_dashboard_images(
                "lovelace/0",
                viewport_presets=["mobile", "desktop"],
                partial_failures=failures,
            )

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "TIMEOUT_API_REQUEST"
        assert error["all_captures_failed"] is True
        assert error["failure_count"] == 2
        assert [
            failure["capture_index"] for failure in error["screenshot_failures"]
        ] == [0, 1]
        assert [failure["preset"] for failure in error["screenshot_failures"]] == [
            "mobile",
            "desktop",
        ]

    async def test_batch_cap_returns_prior_capture_as_partial(
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture

        async def fake_resolve() -> str:
            return "http://engine:10000"

        monkeypatch.setattr(capture, "resolve_engine_url", fake_resolve)
        monkeypatch.setattr(capture, "MAX_IMAGE_PAYLOAD_BYTES", 4)
        monkeypatch.setattr(capture, "MAX_BATCH_PAYLOAD_BYTES", 6)
        _FakeAsyncClient._next = _FakeResponse(
            200, _BMP, content_type="image/bmp", content_length=4
        )
        monkeypatch.setattr(capture.httpx, "AsyncClient", _FakeAsyncClient)
        failures: list[dict[str, Any]] = []

        result = await capture.capture_dashboard_images(
            "lovelace/0",
            viewport_presets=["mobile", "desktop"],
            image_format="bmp",
            partial_failures=failures,
        )

        assert [item.preset for item in result] == ["mobile"]
        assert failures[0]["error"]["code"] == "IMAGE_PAYLOAD_TOO_LARGE"
        assert failures[0]["limit_kind"] == "batch"
        assert failures[0]["capture_index"] == 1

    async def test_batch_presets_forward_deterministic_context(
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture

        async def fake_resolve() -> str:
            return "http://engine:10000"

        monkeypatch.setattr(capture, "resolve_engine_url", fake_resolve)
        _FakeAsyncClient.gets = []
        _FakeAsyncClient._next = _FakeResponse(200, _JPEG, content_type="image/jpeg")
        monkeypatch.setattr(capture.httpx, "AsyncClient", _FakeAsyncClient)

        captures = await capture.capture_dashboard_images(
            "wall-panel/home",
            viewport_presets=["mobile", "desktop"],
            orientation="landscape",
            zoom=1.25,
            wait_ms=1234,
            theme="Gerry Dark",
            dark_mode=True,
            language="de",
            image_format="jpeg",
            render_timeout_seconds=90,
        )

        assert [item.preset for item in captures] == ["mobile", "desktop"]
        assert [(item.width, item.height) for item in captures] == [
            (844, 390),
            (1280, 800),
        ]
        assert all(item.mime_type == "image/jpeg" for item in captures)
        assert all(item.requested["theme"] == "Gerry Dark" for item in captures)
        assert [request["params"]["viewport"] for request in _FakeAsyncClient.gets] == [
            "844x390",
            "1280x800",
        ]
        for request in _FakeAsyncClient.gets:
            assert request["params"] == {
                "viewport": request["params"]["viewport"],
                "zoom": "1.25",
                "wait": "1234",
                "format": "jpeg",
                "theme": "Gerry Dark",
                "dark": "",
                "lang": "de",
            }

    async def test_rejects_unexpected_content_type(self, monkeypatch: Any) -> None:
        from ha_mcp.dashboard_screenshot import capture

        async def fake_resolve() -> str:
            return "http://engine:10000"

        monkeypatch.setattr(capture, "resolve_engine_url", fake_resolve)
        _FakeAsyncClient._next = _FakeResponse(
            200, b"not a jpeg", content_type="image/png"
        )
        monkeypatch.setattr(capture.httpx, "AsyncClient", _FakeAsyncClient)

        with pytest.raises(ToolError) as exc_info:
            await capture.capture_dashboard_images("lovelace/0", image_format="jpeg")
        assert "unexpected content type" in str(exc_info.value).lower()

    @pytest.mark.parametrize(
        ("image_format", "mime_type", "body"),
        [
            ("png", "image/png", _PNG),
            ("jpeg", "image/jpeg", _JPEG),
            ("webp", "image/webp", _WEBP),
            ("bmp", "image/bmp", _BMP),
        ],
    )
    async def test_accepts_matching_image_signatures(
        self,
        monkeypatch: Any,
        image_format: Literal["png", "jpeg", "webp", "bmp"],
        mime_type: str,
        body: bytes,
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture

        async def fake_resolve() -> str:
            return "http://engine:10000"

        monkeypatch.setattr(capture, "resolve_engine_url", fake_resolve)
        _FakeAsyncClient._next = _FakeResponse(200, body, content_type=mime_type)
        monkeypatch.setattr(capture.httpx, "AsyncClient", _FakeAsyncClient)

        result = await capture.capture_dashboard_images(
            "lovelace/0", image_format=image_format
        )

        assert result[0].data == body

    @pytest.mark.parametrize(
        ("image_format", "mime_type"),
        [
            ("png", "image/png"),
            ("jpeg", "image/jpeg"),
            ("webp", "image/webp"),
            ("bmp", "image/bmp"),
        ],
    )
    async def test_rejects_mislabeled_non_image_bytes(
        self,
        monkeypatch: Any,
        image_format: Literal["png", "jpeg", "webp", "bmp"],
        mime_type: str,
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture

        async def fake_resolve() -> str:
            return "http://engine:10000"

        monkeypatch.setattr(capture, "resolve_engine_url", fake_resolve)
        _FakeAsyncClient._next = _FakeResponse(
            200, b"<html>login</html>", content_type=mime_type
        )
        monkeypatch.setattr(capture.httpx, "AsyncClient", _FakeAsyncClient)

        with pytest.raises(ToolError) as exc_info:
            await capture.capture_dashboard_images(
                "lovelace/0", image_format=image_format
            )

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "SERVICE_CALL_FAILED"
        assert error["requested_format"] == image_format
        assert error["received_signature_hex"]

    async def test_timeout_has_distinct_structured_error(
        self, monkeypatch: Any
    ) -> None:
        import httpx

        from ha_mcp.dashboard_screenshot import capture

        async def fake_resolve() -> str:
            return "http://engine:10000"

        monkeypatch.setattr(capture, "resolve_engine_url", fake_resolve)
        _FakeAsyncClient._next = httpx.ReadTimeout("render too slow")
        monkeypatch.setattr(capture.httpx, "AsyncClient", _FakeAsyncClient)

        with pytest.raises(ToolError) as exc_info:
            await capture.capture_dashboard_images(
                "lovelace/0", render_timeout_seconds=1
            )
        assert "TIMEOUT_API_REQUEST" in str(exc_info.value)

    async def test_rejects_duplicate_viewport_presets(self) -> None:
        from ha_mcp.dashboard_screenshot import capture

        with pytest.raises(ToolError) as exc_info:
            await capture.capture_dashboard_images(
                "lovelace/0", viewport_presets=["mobile", "mobile"]
            )
        assert "without duplicate presets" in str(exc_info.value)

    async def test_http_error_raises_toolerror(self, monkeypatch: Any) -> None:
        from ha_mcp.dashboard_screenshot import capture

        async def fake_resolve() -> str:
            return "http://engine:10000"

        monkeypatch.setattr(capture, "resolve_engine_url", fake_resolve)
        _FakeAsyncClient._next = _FakeResponse(502, b"", text="bad gateway")
        monkeypatch.setattr(capture.httpx, "AsyncClient", _FakeAsyncClient)

        with pytest.raises(ToolError):
            await capture.capture_dashboard_png("lovelace/0")

    async def test_empty_body_raises_toolerror(self, monkeypatch: Any) -> None:
        from ha_mcp.dashboard_screenshot import capture

        async def fake_resolve() -> str:
            return "http://engine:10000"

        monkeypatch.setattr(capture, "resolve_engine_url", fake_resolve)
        _FakeAsyncClient._next = _FakeResponse(200, b"")
        monkeypatch.setattr(capture.httpx, "AsyncClient", _FakeAsyncClient)

        with pytest.raises(ToolError):
            await capture.capture_dashboard_png("lovelace/0")

    @pytest.mark.parametrize("path", ["/", "/.", "", "  ", "//"])
    async def test_root_or_empty_path_rejected(self, path: str) -> None:
        """A root/empty path would make the engine serve its config/UI HTML
        instead of a dashboard PNG — reject it rather than fail silently."""
        from ha_mcp.dashboard_screenshot import capture

        with pytest.raises(ToolError):
            capture._validate_dashboard_path(path)

    @pytest.mark.parametrize(
        "path",
        [
            "http://evil/x",  # scheme injection
            "lovelace/0?x=://evil",  # query + scheme
            "lovelace/../admin",  # traversal
            "user@host/x",  # authority
            "lovelace\\0",  # backslash
            "lovelace/0?a=b",  # query string
            "lovelace/0#frag",  # fragment
            "lovelace/\x01ctrl",  # control character
        ],
    )
    async def test_attack_vector_paths_rejected(self, path: str) -> None:
        """Each path that could re-point the credentialed engine at an
        arbitrary URL / admin route must raise, not just root/empty."""
        from ha_mcp.dashboard_screenshot import capture

        with pytest.raises(ToolError):
            capture._validate_dashboard_path(path)

    async def test_path_segments_are_percent_encoded(self, monkeypatch: Any) -> None:
        """A valid segment with an encodable char reaches the engine
        percent-encoded (defense-in-depth), not raw."""
        from ha_mcp.dashboard_screenshot import capture

        async def fake_resolve() -> str:
            return "http://engine:10000"

        monkeypatch.setattr(capture, "resolve_engine_url", fake_resolve)
        _FakeAsyncClient._next = _FakeResponse(200, b"\x89PNG\r\n\x1a\nfake")
        monkeypatch.setattr(capture.httpx, "AsyncClient", _FakeAsyncClient)

        await capture.capture_dashboard_png("my dash/0")
        assert _FakeAsyncClient.last_get["url"] == "http://engine:10000/my%20dash/0"


# ---------------------------------------------------------------------------
# Graceful get/set screenshot helper
# ---------------------------------------------------------------------------


def _fake_dashboard_capture(
    *,
    width: int = 1280,
    height: int | Literal["auto"] = 800,
    preset: Literal["mobile", "tablet", "desktop"] | None = None,
    image_format: Literal["png", "jpeg", "webp", "bmp"] = "png",
    mime_type: str = "image/png",
    data: bytes = b"\x89PNG\r\n\x1a\nfake",
    legacy_full_page_fallback: bool = False,
) -> Any:
    from ha_mcp.dashboard_screenshot.capture import DashboardImageCapture

    return DashboardImageCapture(
        data=data,
        width=width,
        height=height,
        preset=preset,
        orientation="landscape",
        image_format=image_format,
        mime_type=mime_type,
        size_bytes=len(data),
        requested={
            "zoom": 1.0,
            "wait_ms": 2500,
            "full_page": height == "auto",
            "orientation": None,
            "theme": None,
            "dark_mode": False,
            "language": None,
            "render_timeout_seconds": 60.0,
            "legacy_full_page_fallback": legacy_full_page_fallback,
        },
    )


def test_multiple_capture_content_and_metadata_stay_ordered() -> None:
    from ha_mcp.dashboard_screenshot.content import (
        dashboard_image_content,
        dashboard_screenshot_metadata,
    )

    captures = [
        _fake_dashboard_capture(width=390, height=844, preset="mobile"),
        _fake_dashboard_capture(width=1280, height=800, preset="desktop"),
    ]

    content = dashboard_image_content(captures)
    metadata = dashboard_screenshot_metadata(captures, "wall-panel/home")

    assert [block.mimeType for block in content] == ["image/png", "image/png"]
    assert [item["content_index"] for item in metadata] == [0, 1]
    assert [item["viewport"]["preset"] for item in metadata] == [
        "mobile",
        "desktop",
    ]
    assert all(item["render_path"] == "wall-panel/home" for item in metadata)
    assert all(len(item["image"]["sha256"]) == 64 for item in metadata)
    assert metadata[0]["engine_request"]["viewport"] == "390x844"
    assert metadata[0]["engine_request"]["format"] == "png"
    assert metadata[0]["local_capture_options"] == {
        "full_page": False,
        "render_timeout_seconds": 60.0,
        "legacy_full_page_fallback": False,
    }


@pytest.mark.parametrize(
    ("image_format", "mime_type", "data"),
    [
        ("jpeg", "image/jpeg", b"jpeg"),
        ("webp", "image/webp", b"webp"),
        ("bmp", "image/bmp", b"bmp"),
    ],
)
def test_non_png_native_content_preserves_mime(
    image_format: Literal["jpeg", "webp", "bmp"],
    mime_type: str,
    data: bytes,
) -> None:
    from ha_mcp.dashboard_screenshot.content import dashboard_image_content

    content = dashboard_image_content(
        [
            _fake_dashboard_capture(
                image_format=image_format,
                mime_type=mime_type,
                data=data,
            )
        ]
    )

    assert content[0].mimeType == mime_type


class TestMaybeAttachScreenshot:
    async def test_skips_when_not_requested(self) -> None:
        from ha_mcp.tools.tools_config_dashboards import _maybe_attach_screenshot

        result = {"success": True}
        out = await _maybe_attach_screenshot(result, "default", requested=False)
        assert out is result

    async def test_feature_off_warns(self, monkeypatch: Any) -> None:
        from ha_mcp.tools.tools_config_dashboards import _maybe_attach_screenshot

        monkeypatch.setattr(
            config,
            "get_global_settings",
            lambda: SimpleNamespace(enable_dashboard_screenshot=False),
        )
        result: dict[str, Any] = {"success": True}
        out = await _maybe_attach_screenshot(result, "default", requested=True)
        assert out is result
        assert any("disabled" in w.lower() for w in result.get("warnings", []))

    async def test_capture_failure_warns(self, monkeypatch: Any) -> None:
        from ha_mcp.dashboard_screenshot import capture
        from ha_mcp.tools.tools_config_dashboards import _maybe_attach_screenshot

        monkeypatch.setattr(
            config,
            "get_global_settings",
            lambda: SimpleNamespace(enable_dashboard_screenshot=True),
        )

        async def boom(*_a: Any, **_kw: Any) -> list[Any]:
            raise ToolError("engine unreachable")

        monkeypatch.setattr(capture, "capture_dashboard_images", boom)

        result: dict[str, Any] = {"success": True}
        out = await _maybe_attach_screenshot(result, "my-dash", requested=True)
        assert out is result
        assert any("unavailable" in w.lower() for w in result.get("warnings", []))

    async def test_full_page_passed_through(self, monkeypatch: Any) -> None:
        """_maybe_attach_screenshot forwards full_page to the capture call."""
        from ha_mcp.dashboard_screenshot import capture
        from ha_mcp.tools.tools_config_dashboards import _maybe_attach_screenshot

        monkeypatch.setattr(
            config,
            "get_global_settings",
            lambda: SimpleNamespace(enable_dashboard_screenshot=True),
        )

        seen: dict[str, Any] = {}

        async def record(_path: str, **kw: Any) -> list[Any]:
            seen["full_page"] = kw.get("full_page")
            seen["path"] = _path
            return [_fake_dashboard_capture(height="auto")]

        monkeypatch.setattr(capture, "capture_dashboard_images", record)

        await _maybe_attach_screenshot(
            {"success": True}, "my-dash", requested=True, full_page=True
        )
        assert seen["full_page"] is True
        # A concrete url_path passes through unchanged to the engine path.
        assert seen["path"] == "my-dash"

    async def test_success_returns_toolresult_with_image(
        self, monkeypatch: Any
    ) -> None:
        from fastmcp.tools.tool import ToolResult

        from ha_mcp.dashboard_screenshot import capture
        from ha_mcp.tools.tools_config_dashboards import _maybe_attach_screenshot

        monkeypatch.setattr(
            config,
            "get_global_settings",
            lambda: SimpleNamespace(enable_dashboard_screenshot=True),
        )

        async def ok(*_a: Any, **_kw: Any) -> list[Any]:
            return [_fake_dashboard_capture()]

        monkeypatch.setattr(capture, "capture_dashboard_images", ok)

        result: dict[str, Any] = {"success": True}
        out = await _maybe_attach_screenshot(result, "my-dash", requested=True)
        # Success returns a ToolResult so structured_content (the dict) is
        # present on both the screenshot and no-screenshot paths, plus the PNG
        # as an image content block.
        assert isinstance(out, ToolResult)
        assert out.structured_content["success"] is True
        assert len(out.content) == 1
        assert out.content[0].type == "image"
        assert out.structured_content["screenshot_render_path"] == "my-dash"
        assert out.structured_content["screenshots"][0]["content_index"] == 0
        assert (
            out.structured_content["screenshots"][0]["frontend_context_confirmed"]
            is False
        )
        assert "screenshots" not in result

    async def test_legacy_fallback_warning_is_preserved_on_config_path(
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture
        from ha_mcp.tools.tools_config_dashboards import _maybe_attach_screenshot

        monkeypatch.setattr(
            config,
            "get_global_settings",
            lambda: SimpleNamespace(enable_dashboard_screenshot=True),
        )

        async def ok(*_a: Any, **_kw: Any) -> list[Any]:
            return [
                _fake_dashboard_capture(height=4096, legacy_full_page_fallback=True)
            ]

        monkeypatch.setattr(capture, "capture_dashboard_images", ok)

        out = await _maybe_attach_screenshot(
            {"success": True}, "my-dash", requested=True, full_page=True
        )

        assert any("4096" in warning for warning in out.structured_content["warnings"])
        assert (
            out.structured_content["screenshots"][0]["local_capture_options"][
                "legacy_full_page_fallback"
            ]
            is True
        )

    async def test_named_view_and_batch_options_pass_through(
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture
        from ha_mcp.tools.tools_config_dashboards import (
            _DashboardScreenshotOptions,
            _maybe_attach_screenshot,
        )

        monkeypatch.setattr(
            config,
            "get_global_settings",
            lambda: SimpleNamespace(enable_dashboard_screenshot=True),
        )
        seen: dict[str, Any] = {}

        async def record(path: str, **kwargs: Any) -> list[Any]:
            seen["path"] = path
            seen["kwargs"] = kwargs
            return [
                _fake_dashboard_capture(width=390, height=844, preset="mobile"),
                _fake_dashboard_capture(width=1280, height=800, preset="desktop"),
            ]

        monkeypatch.setattr(capture, "capture_dashboard_images", record)
        result: dict[str, Any] = {"success": True}
        out = await _maybe_attach_screenshot(
            result,
            "wall-panel",
            requested=True,
            config={"views": [{"title": "Home", "path": "home"}]},
            options=_DashboardScreenshotOptions(
                view_path="home",
                viewport_presets=["mobile", "desktop"],
                theme="Gerry Dark",
                dark_mode=True,
                language="de",
            ),
        )

        assert seen["path"] == "wall-panel/home"
        assert seen["kwargs"]["viewport_presets"] == ["mobile", "desktop"]
        assert seen["kwargs"]["theme"] == "Gerry Dark"
        assert len(out.content) == 2
        assert out.structured_content["screenshot_render_path"] == "wall-panel/home"

    async def test_partial_batch_metadata_is_preserved_on_config_path(
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture
        from ha_mcp.tools.tools_config_dashboards import _maybe_attach_screenshot

        monkeypatch.setattr(
            config,
            "get_global_settings",
            lambda: SimpleNamespace(enable_dashboard_screenshot=True),
        )

        async def partial(*_args: Any, **kwargs: Any) -> list[Any]:
            kwargs["partial_failures"].append(
                {
                    "success": False,
                    "error": {"code": "TIMEOUT_API_REQUEST", "message": "timeout"},
                    "capture_index": 1,
                    "preset": "desktop",
                }
            )
            return [_fake_dashboard_capture(width=390, height=844, preset="mobile")]

        monkeypatch.setattr(capture, "capture_dashboard_images", partial)

        out = await _maybe_attach_screenshot(
            {"success": True}, "wall-panel", requested=True
        )

        assert len(out.content) == 1
        assert out.structured_content["screenshot_partial"] is True
        assert out.structured_content["screenshot_failures"][0]["preset"] == ("desktop")

    @pytest.mark.parametrize("raw_error", ["null", "[]"])
    async def test_non_object_tool_error_never_breaks_committed_result(
        self, monkeypatch: Any, raw_error: str
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture
        from ha_mcp.tools.tools_config_dashboards import _maybe_attach_screenshot

        monkeypatch.setattr(
            config,
            "get_global_settings",
            lambda: SimpleNamespace(enable_dashboard_screenshot=True),
        )

        async def fail(*_args: Any, **_kwargs: Any) -> list[Any]:
            raise ToolError(raw_error)

        monkeypatch.setattr(capture, "capture_dashboard_images", fail)
        result: dict[str, Any] = {"success": True, "write_committed": True}

        out = await _maybe_attach_screenshot(result, "wall-panel", requested=True)

        assert out is result
        assert out["success"] is True
        assert out["screenshot_error"]["code"] == "INTERNAL_ERROR"

    async def test_serialization_failure_does_not_publish_false_image_metadata(
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture
        from ha_mcp.tools import tools_config_dashboards as dashboard_tools

        monkeypatch.setattr(
            config,
            "get_global_settings",
            lambda: SimpleNamespace(enable_dashboard_screenshot=True),
        )

        async def ok(*_args: Any, **_kwargs: Any) -> list[Any]:
            return [_fake_dashboard_capture()]

        def serialization_failure(*_args: Any, **_kwargs: Any) -> list[Any]:
            raise ToolError(
                json.dumps(
                    {
                        "success": False,
                        "error": {
                            "code": "IMAGE_SERIALIZATION_FAILED",
                            "message": "encode failed",
                        },
                    }
                )
            )

        monkeypatch.setattr(capture, "capture_dashboard_images", ok)
        monkeypatch.setattr(
            dashboard_tools, "dashboard_image_content", serialization_failure
        )
        result: dict[str, Any] = {"success": True, "write_committed": True}

        out = await dashboard_tools._maybe_attach_screenshot(
            result, "wall-panel", requested=True
        )

        assert out is result
        assert out["screenshot_error"]["code"] == "IMAGE_SERIALIZATION_FAILED"
        assert "screenshots" not in out
        assert "screenshot_render_path" not in out

    async def test_base_route_warning_is_preserved_when_config_is_available(
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture
        from ha_mcp.tools.tools_config_dashboards import _maybe_attach_screenshot

        monkeypatch.setattr(
            config,
            "get_global_settings",
            lambda: SimpleNamespace(enable_dashboard_screenshot=True),
        )

        async def ok(*_args: Any, **_kwargs: Any) -> list[Any]:
            return [_fake_dashboard_capture()]

        monkeypatch.setattr(capture, "capture_dashboard_images", ok)
        result: dict[str, Any] = {"success": True}

        await _maybe_attach_screenshot(
            result,
            "wall-panel",
            requested=True,
            config={"views": [{"title": "Home", "path": "home"}]},
        )

        assert any("currently first" in warning for warning in result["warnings"])

    async def test_get_path_raises_on_capture_failure(self, monkeypatch: Any) -> None:
        """raise_on_failure (the get path) propagates the engine error instead
        of demoting it to a warning the caller may never read."""
        from ha_mcp.dashboard_screenshot import capture
        from ha_mcp.tools.tools_config_dashboards import _maybe_attach_screenshot

        monkeypatch.setattr(
            config,
            "get_global_settings",
            lambda: SimpleNamespace(enable_dashboard_screenshot=True),
        )

        async def boom(*_a: Any, **_kw: Any) -> list[Any]:
            raise ToolError("engine unreachable")

        monkeypatch.setattr(capture, "capture_dashboard_images", boom)

        with pytest.raises(ToolError):
            await _maybe_attach_screenshot(
                {"success": True}, "my-dash", requested=True, raise_on_failure=True
            )

    async def test_full_page_without_request_warns(self) -> None:
        """full_page is meaningless without a screenshot flag — make the
        dropped request observable rather than a silent no-op."""
        from ha_mcp.tools.tools_config_dashboards import _maybe_attach_screenshot

        result: dict[str, Any] = {"success": True}
        out = await _maybe_attach_screenshot(
            result, "my-dash", requested=False, full_page=True
        )
        assert out is result
        assert any(
            "render options are ignored" in w for w in result.get("warnings", [])
        )


async def test_post_write_render_path_fetch_failure_is_only_a_warning(
    monkeypatch: Any,
) -> None:
    from ha_mcp.tools import tools_config_dashboards as dashboard_tools

    async def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("websocket disconnected")

    monkeypatch.setattr(dashboard_tools, "_get_dashboard_config_internal", boom)
    result: dict[str, Any] = {"success": True, "action": "update"}

    config_result = await dashboard_tools._attach_dashboard_render_paths_after_write(
        object(), result, "wall-panel"
    )

    assert config_result is None
    assert result["success"] is True
    assert "render_paths" not in result
    assert any("websocket disconnected" in warning for warning in result["warnings"])


async def test_post_write_render_paths_use_authoritative_readback(
    monkeypatch: Any,
) -> None:
    from ha_mcp.tools import tools_config_dashboards as dashboard_tools

    submitted = {"views": [{"title": "Home", "path": "submitted"}]}
    authoritative = {"views": [{"title": "Home", "path": "normalized"}]}

    async def readback(*_args: Any, **_kwargs: Any) -> tuple[dict[str, Any], str]:
        return authoritative, "hash"

    monkeypatch.setattr(dashboard_tools, "_get_dashboard_config_internal", readback)
    result: dict[str, Any] = {"success": True, "action": "update"}

    config_result = await dashboard_tools._attach_dashboard_render_paths_after_write(
        object(), result, "wall-panel", submitted
    )

    assert config_result is authoritative
    assert result["render_paths"][0]["render_path"] == "wall-panel/normalized"


async def test_post_write_readback_failure_keeps_only_screenshot_fallback(
    monkeypatch: Any,
) -> None:
    from ha_mcp.tools import tools_config_dashboards as dashboard_tools

    fallback = {"views": [{"title": "Home", "path": "submitted"}]}

    async def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("readback failed")

    monkeypatch.setattr(dashboard_tools, "_get_dashboard_config_internal", boom)
    result: dict[str, Any] = {"success": True, "action": "update"}

    config_result = await dashboard_tools._attach_dashboard_render_paths_after_write(
        object(), result, "wall-panel", fallback
    )

    assert config_result is fallback
    assert "render_paths" not in result
    assert any("readback failed" in warning for warning in result["warnings"])


async def test_python_transform_can_return_screenshot_from_post_save_config(
    monkeypatch: Any,
) -> None:
    from ha_mcp.tools import tools_config_dashboards as dashboard_tools

    tools = dashboard_tools.DashboardConfigTools(object())
    current = {"views": [{"title": "Old", "path": "home"}]}
    transformed = {"views": [{"title": "New", "path": "home"}]}
    seen: dict[str, Any] = {}

    async def fetch_and_verify(_url_path: str, _config_hash: str) -> dict[str, Any]:
        return current

    def apply_transform(
        _url_path: str, _expression: str, _config: dict[str, Any]
    ) -> dict[str, Any]:
        return transformed

    async def save_transform(
        _url_path: str, _config: dict[str, Any]
    ) -> tuple[dict[str, Any], str, str | None]:
        return transformed, "new-hash", None

    async def attach(
        result: dict[str, Any],
        url_path: str,
        requested: bool,
        **kwargs: Any,
    ) -> dict[str, Any]:
        seen.update(
            result=result,
            url_path=url_path,
            requested=requested,
            config=kwargs["config"],
        )
        return result

    monkeypatch.setattr(tools, "_fetch_and_verify_dashboard_hash", fetch_and_verify)
    monkeypatch.setattr(tools, "_apply_dashboard_python_transform", apply_transform)
    monkeypatch.setattr(tools, "_save_dashboard_python_transform", save_transform)
    monkeypatch.setattr(dashboard_tools, "_maybe_attach_screenshot", attach)

    result = await tools._run_dashboard_python_transform(
        "wall-panel",
        "old-hash",
        "config['views'][0]['title'] = 'New'",
        None,
        False,
        return_screenshot=True,
        screenshot_options=dashboard_tools._DashboardScreenshotOptions(
            view_path="home"
        ),
    )

    assert result["config_hash"] == "new-hash"
    assert result["render_paths"][0]["render_path"] == "wall-panel/home"
    assert seen == {
        "result": result,
        "url_path": "wall-panel",
        "requested": True,
        "config": transformed,
    }


async def test_python_transform_post_save_read_failure_reports_committed_write(
    monkeypatch: Any,
) -> None:
    from ha_mcp.tools import tools_config_dashboards as dashboard_tools

    tools = dashboard_tools.DashboardConfigTools(object())
    transformed = {"views": [{"title": "New", "path": "home"}]}

    async def fetch_and_verify(_url_path: str, _config_hash: str) -> dict[str, Any]:
        return {"views": [{"title": "Old", "path": "home"}]}

    def apply_transform(
        _url_path: str, _expression: str, _config: dict[str, Any]
    ) -> dict[str, Any]:
        return transformed

    async def save_transform(
        _url_path: str, _config: dict[str, Any]
    ) -> tuple[dict[str, Any], str | None, str | None]:
        return transformed, None, "authoritative reload failed"

    monkeypatch.setattr(tools, "_fetch_and_verify_dashboard_hash", fetch_and_verify)
    monkeypatch.setattr(tools, "_apply_dashboard_python_transform", apply_transform)
    monkeypatch.setattr(tools, "_save_dashboard_python_transform", save_transform)

    result = await tools._run_dashboard_python_transform(
        "wall-panel",
        "old-hash",
        "config['views'][0]['title'] = 'New'",
        None,
        False,
        return_screenshot=False,
        screenshot_options=dashboard_tools._DashboardScreenshotOptions(),
    )

    assert result["success"] is True
    assert result["write_committed"] is True
    assert result["post_write_verified"] is False
    assert result["config_hash"] is None
    assert "authoritative reload failed" in result["warnings"][0]
    assert "render_paths" not in result


class TestDashboardFrontendPath:
    @pytest.mark.parametrize(
        ("url_path", "expected"),
        [(None, "lovelace"), ("default", "lovelace"), ("my-dash", "my-dash")],
    )
    def test_maps_default_and_passes_through(
        self, url_path: str | None, expected: str
    ) -> None:
        from ha_mcp.tools.tools_config_dashboards import _dashboard_frontend_path

        assert _dashboard_frontend_path(url_path) == expected


class TestNoteScreenshotIgnored:
    def test_warns_when_screenshot_requested(self) -> None:
        from ha_mcp.tools.tools_config_dashboards import _note_screenshot_ignored

        result: dict[str, Any] = {"success": True}
        _note_screenshot_ignored(
            result, include_screenshot=True, full_page=False, mode="list"
        )
        assert any("ignored in list mode" in w for w in result["warnings"])

    def test_warns_when_only_full_page_set(self) -> None:
        from ha_mcp.tools.tools_config_dashboards import _note_screenshot_ignored

        result: dict[str, Any] = {"success": True}
        _note_screenshot_ignored(
            result, include_screenshot=False, full_page=True, mode="search"
        )
        assert any("ignored in search mode" in w for w in result["warnings"])

    def test_silent_when_neither_set(self) -> None:
        from ha_mcp.tools.tools_config_dashboards import _note_screenshot_ignored

        result: dict[str, Any] = {"success": True}
        _note_screenshot_ignored(
            result, include_screenshot=False, full_page=False, mode="list"
        )
        assert "warnings" not in result

    def test_warns_when_non_default_render_option_is_set(self) -> None:
        from ha_mcp.tools.tools_config_dashboards import (
            _DashboardScreenshotOptions,
            _note_screenshot_ignored,
        )

        result: dict[str, Any] = {"success": True}
        _note_screenshot_ignored(
            result,
            include_screenshot=False,
            options=_DashboardScreenshotOptions(language="de"),
            mode="search",
        )
        assert any("ignored in search mode" in w for w in result["warnings"])


def test_public_screenshot_option_names_stay_in_parity() -> None:
    import inspect

    from ha_mcp.tools.tools_config_dashboards import DashboardConfigTools
    from ha_mcp.tools.tools_dashboard_screenshot import DashboardScreenshotTools

    shared = {
        "view_path",
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

    assert shared <= standalone
    assert shared <= get_options
    assert shared <= set_options
