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

from types import SimpleNamespace
from typing import Any, ClassVar, Literal

import pytest
from fastmcp.exceptions import ToolError
from pydantic import ValidationError

import ha_mcp.config as config


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
                "/addons/abc_puppet/info": {"data": {"state": "stopped"}},
                "/addons/def_puppet/info": {
                    "data": {"state": "started", "hostname": "def-puppet"}
                },
            },
        )
        url = await provision._discover_engine_url_via_supervisor()
        assert url == "http://def-puppet:10000"

    async def test_started_without_hostname_raises(self, monkeypatch: Any) -> None:
        from ha_mcp.dashboard_screenshot import provision

        _patch_supervisor(
            monkeypatch,
            {
                "/addons": {"data": {"addons": [{"slug": "def_puppet"}]}},
                "/addons/def_puppet/info": {"data": {"state": "started"}},
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
                "/addons/def_puppet/info": {"data": {"state": "stopped"}},
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
    ) -> None:
        self.status_code = status_code
        self.content = content
        self.text = text
        self.headers = {"content-type": content_type}


class _FakeAsyncClient:
    """Minimal async-context httpx.AsyncClient stand-in."""

    last_get: ClassVar[dict[str, Any]] = {}
    gets: ClassVar[list[dict[str, Any]]] = []
    _next: ClassVar[_FakeResponse | Exception]

    def __init__(self, *_a: Any, **_kw: Any) -> None:
        pass

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *_a: Any) -> None:
        return None

    async def get(
        self, url: str, params: dict[str, Any] | None = None
    ) -> _FakeResponse:
        _FakeAsyncClient.last_get = {"url": url, "params": params}
        _FakeAsyncClient.gets.append(_FakeAsyncClient.last_get)
        if isinstance(_FakeAsyncClient._next, Exception):
            raise _FakeAsyncClient._next
        return _FakeAsyncClient._next


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

    async def test_batch_presets_forward_deterministic_context(
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture

        async def fake_resolve() -> str:
            return "http://engine:10000"

        monkeypatch.setattr(capture, "resolve_engine_url", fake_resolve)
        _FakeAsyncClient.gets = []
        _FakeAsyncClient._next = _FakeResponse(200, b"jpeg", content_type="image/jpeg")
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
) -> Any:
    from ha_mcp.dashboard_screenshot.capture import DashboardImageCapture

    return DashboardImageCapture(
        data=b"\x89PNG\r\n\x1a\nfake",
        width=width,
        height=height,
        preset=preset,
        orientation="landscape",
        image_format="png",
        mime_type="image/png",
        size_bytes=12,
        requested={
            "zoom": 1.0,
            "wait_ms": 2500,
            "full_page": height == "auto",
            "orientation": None,
            "theme": None,
            "dark_mode": False,
            "language": None,
            "render_timeout_seconds": 60.0,
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
    }


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
        assert out.structured_content == result
        assert len(out.content) == 1
        assert out.content[0].type == "image"
        assert result["screenshot_render_path"] == "my-dash"
        assert result["screenshots"][0]["content_index"] == 0
        assert result["screenshots"][0]["frontend_context_confirmed"] is False

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
        assert result["screenshot_render_path"] == "wall-panel/home"

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
    assert any("websocket disconnected" in warning for warning in result["warnings"])


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
    ) -> tuple[dict[str, Any], str]:
        return transformed, "new-hash"

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
