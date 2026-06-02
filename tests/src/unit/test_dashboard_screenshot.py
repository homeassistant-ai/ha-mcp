"""Unit tests for the opt-in dashboard-screenshot feature (ha-mcp side).

Covers everything that runs inside ha-mcp itself — no container, no engine:
- the ``enable_dashboard_screenshot`` flag + ``dashboard_screenshot_engine_url``
  setting (defaults, env, registry membership)
- tool-registration gating
- engine-URL resolution (explicit / stdio branches)
- the capture HTTP client (URL/param building, PNG return, error -> ToolError)
- the graceful get/set screenshot helper (feature-off / failure -> warning)

The headless-Chromium engine is exercised end to end in the HAOS lane
(tests/src/e2e/haos_only/test_dashboard_screenshot_addon.py).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from fastmcp.exceptions import ToolError
from pydantic import ValidationError

from ha_mcp.config import BETA_FEATURE_FIELDS, FEATURE_FLAG_FIELDS, Settings


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
        assert Settings().enable_dashboard_screenshot is False

    def test_flag_enabled_via_env(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("HAMCP_ENABLE_DASHBOARD_SCREENSHOT", "true")
        assert Settings().enable_dashboard_screenshot is True

    def test_flag_empty_string_means_false(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("HAMCP_ENABLE_DASHBOARD_SCREENSHOT", "")
        assert Settings().enable_dashboard_screenshot is False

    def test_engine_url_default_empty(self) -> None:
        assert Settings().dashboard_screenshot_engine_url == ""

    def test_engine_url_from_env(self, monkeypatch: Any) -> None:
        monkeypatch.setenv(
            "HAMCP_DASHBOARD_SCREENSHOT_ENGINE_URL", "http://engine:10000"
        )
        assert Settings().dashboard_screenshot_engine_url == "http://engine:10000"

    @pytest.mark.parametrize("bad", ["ftp://engine:10000", "engine:10000", "//engine"])
    def test_engine_url_rejects_non_http(self, monkeypatch: Any, bad: str) -> None:
        # The validator must fail loudly at startup on a scheme-less / wrong
        # scheme URL instead of letting it 0-byte-fail at render time.
        monkeypatch.setenv("HAMCP_DASHBOARD_SCREENSHOT_ENGINE_URL", bad)
        with pytest.raises(ValidationError):
            Settings()

    def test_engine_url_validator_strips_trailing_slash(self, monkeypatch: Any) -> None:
        # The field validator (not just resolve_engine_url) normalizes the URL.
        monkeypatch.setenv(
            "HAMCP_DASHBOARD_SCREENSHOT_ENGINE_URL", "http://engine:10000/"
        )
        assert Settings().dashboard_screenshot_engine_url == "http://engine:10000"

    def test_flag_in_feature_flag_fields(self) -> None:
        assert "enable_dashboard_screenshot" in {f.field for f in FEATURE_FLAG_FIELDS}

    def test_flag_is_beta_gated(self) -> None:
        assert "enable_dashboard_screenshot" in BETA_FEATURE_FIELDS

    def test_engine_url_not_a_feature_flag(self) -> None:
        # Connection string, deliberately not a web-editable beta toggle.
        names = {f.field for f in FEATURE_FLAG_FIELDS}
        assert "dashboard_screenshot_engine_url" not in names
        assert "dashboard_screenshot_engine_url" not in BETA_FEATURE_FIELDS


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
        import ha_mcp.config as config
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
        import ha_mcp.config as config
        from ha_mcp.tools import tools_dashboard_screenshot as mod

        monkeypatch.setattr(
            config,
            "get_global_settings",
            lambda: SimpleNamespace(enable_dashboard_screenshot=True),
        )
        mcp = _RecordingMcp()
        mod.register_dashboard_screenshot_tools(mcp, client=None)
        assert len(mcp.added) == 1


# ---------------------------------------------------------------------------
# Engine-URL resolution
# ---------------------------------------------------------------------------


class TestResolveEngineUrl:
    async def test_explicit_url_strips_trailing_slash(self, monkeypatch: Any) -> None:
        import ha_mcp.config as config
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
        import ha_mcp.config as config
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
                            {"slug": "abc_ha_mcp_screenshot"},  # legacy, stopped
                            {"slug": "def_puppet"},  # stock, started
                        ]
                    }
                },
                "/addons/abc_ha_mcp_screenshot/info": {"data": {"state": "stopped"}},
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
    def __init__(self, status_code: int, content: bytes, text: str = "") -> None:
        self.status_code = status_code
        self.content = content
        self.text = text


class _FakeAsyncClient:
    """Minimal async-context httpx.AsyncClient stand-in."""

    last_get: ClassVar[dict[str, Any]] = {}
    _next: ClassVar[_FakeResponse]

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

    async def test_full_page_overrides_height(self, monkeypatch: Any) -> None:
        """full_page=True asks the engine for a tall viewport (FULL_PAGE_HEIGHT),
        ignoring the requested height, so content below the fold is captured."""
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
        assert got["params"]["viewport"] == f"1024x{capture.FULL_PAGE_HEIGHT}"

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


class TestMaybeAttachScreenshot:
    async def test_skips_when_not_requested(self) -> None:
        from ha_mcp.tools.tools_config_dashboards import _maybe_attach_screenshot

        result = {"success": True}
        out = await _maybe_attach_screenshot(result, "default", requested=False)
        assert out is result

    async def test_feature_off_warns(self, monkeypatch: Any) -> None:
        import ha_mcp.config as config
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
        import ha_mcp.config as config
        from ha_mcp.dashboard_screenshot import capture
        from ha_mcp.tools.tools_config_dashboards import _maybe_attach_screenshot

        monkeypatch.setattr(
            config,
            "get_global_settings",
            lambda: SimpleNamespace(enable_dashboard_screenshot=True),
        )

        async def boom(*_a: Any, **_kw: Any) -> bytes:
            raise ToolError("engine unreachable")

        monkeypatch.setattr(capture, "capture_dashboard_png", boom)

        result: dict[str, Any] = {"success": True}
        out = await _maybe_attach_screenshot(result, "my-dash", requested=True)
        assert out is result
        assert any("unavailable" in w.lower() for w in result.get("warnings", []))

    async def test_full_page_passed_through(self, monkeypatch: Any) -> None:
        """_maybe_attach_screenshot forwards full_page to the capture call."""
        import ha_mcp.config as config
        from ha_mcp.dashboard_screenshot import capture
        from ha_mcp.tools.tools_config_dashboards import _maybe_attach_screenshot

        monkeypatch.setattr(
            config,
            "get_global_settings",
            lambda: SimpleNamespace(enable_dashboard_screenshot=True),
        )

        seen: dict[str, Any] = {}

        async def record(_path: str, **kw: Any) -> bytes:
            seen["full_page"] = kw.get("full_page")
            seen["path"] = _path
            return b"\x89PNG\r\n\x1a\nfake"

        monkeypatch.setattr(capture, "capture_dashboard_png", record)

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

        import ha_mcp.config as config
        from ha_mcp.dashboard_screenshot import capture
        from ha_mcp.tools.tools_config_dashboards import _maybe_attach_screenshot

        monkeypatch.setattr(
            config,
            "get_global_settings",
            lambda: SimpleNamespace(enable_dashboard_screenshot=True),
        )

        async def ok(*_a: Any, **_kw: Any) -> bytes:
            return b"\x89PNG\r\n\x1a\nfake"

        monkeypatch.setattr(capture, "capture_dashboard_png", ok)

        result: dict[str, Any] = {"success": True}
        out = await _maybe_attach_screenshot(result, "my-dash", requested=True)
        # Success returns a ToolResult so structured_content (the dict) is
        # present on both the screenshot and no-screenshot paths, plus the PNG
        # as an image content block.
        assert isinstance(out, ToolResult)
        assert out.structured_content == result
        assert len(out.content) == 1
        assert out.content[0].type == "image"

    async def test_get_path_raises_on_capture_failure(self, monkeypatch: Any) -> None:
        """raise_on_failure (the get path) propagates the engine error instead
        of demoting it to a warning the caller may never read."""
        import ha_mcp.config as config
        from ha_mcp.dashboard_screenshot import capture
        from ha_mcp.tools.tools_config_dashboards import _maybe_attach_screenshot

        monkeypatch.setattr(
            config,
            "get_global_settings",
            lambda: SimpleNamespace(enable_dashboard_screenshot=True),
        )

        async def boom(*_a: Any, **_kw: Any) -> bytes:
            raise ToolError("engine unreachable")

        monkeypatch.setattr(capture, "capture_dashboard_png", boom)

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
        assert any("full_page is ignored" in w for w in result.get("warnings", []))


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
