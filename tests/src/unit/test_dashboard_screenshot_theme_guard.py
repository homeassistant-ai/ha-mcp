"""Unit tests for the dashboard-screenshot theme guard (issue #1909).

Stock Puppet dispatches a ``settheme`` event on cold renders, and Home
Assistant persists it server-side on the engine token user's profile —
flipping that user's real sessions to light mode. The guard snapshots the
saved theme before a capture batch and restores it afterwards.

Covers credential resolution (add-on options vs ha-mcp's own credentials),
snapshot/restore semantics against a fake WebSocket client, non-fatal
failure handling, and the restore bracket around ``capture_dashboard_images``
— including the regression scenario itself.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.dashboard_screenshot.provision import EngineTarget
from ha_mcp.dashboard_screenshot.theme_guard import (
    THEME_USER_DATA_KEY,
    ThemeGuard,
    _addon_credential,
    _client_credential,
)

_PNG = b"\x89PNG\r\n\x1a\nunit"
_DARK_THEME = {"theme": "default", "dark": True}
_CLOBBERED_THEME = {"theme": "", "dark": False}


class _FakeWsClient:
    """Scriptable HomeAssistantWebSocketClient stand-in with a user-data store."""

    instances: ClassVar[list[_FakeWsClient]] = []
    user_data: ClassVar[dict[str, Any]] = {}
    connect_ok: ClassVar[bool] = True
    fail_get: ClassVar[bool] = False
    fail_set: ClassVar[bool] = False

    def __init__(self, url: str, token: str, verify_ssl: Any = None) -> None:
        self.url = url
        self.token = token
        self.commands: list[dict[str, Any]] = []
        self.disconnected = False
        self.last_connect_error: str | None = None
        _FakeWsClient.instances.append(self)

    async def connect(self) -> bool:
        if not _FakeWsClient.connect_ok:
            self.last_connect_error = "auth_invalid"
        return _FakeWsClient.connect_ok

    async def disconnect(self) -> None:
        self.disconnected = True

    async def send_command(self, command_type: str, **kwargs: Any) -> dict[str, Any]:
        self.commands.append({"type": command_type, **kwargs})
        if command_type == "frontend/get_user_data":
            if _FakeWsClient.fail_get:
                raise RuntimeError("get_user_data boom")
            value = _FakeWsClient.user_data.get(kwargs["key"])
            return {"success": True, "result": {"value": value}}
        if command_type == "frontend/set_user_data":
            if _FakeWsClient.fail_set:
                raise RuntimeError("set_user_data boom")
            _FakeWsClient.user_data[kwargs["key"]] = kwargs["value"]
            return {"success": True, "result": None}
        raise AssertionError(f"unexpected command {command_type}")


def _all_commands() -> list[dict[str, Any]]:
    return [cmd for ws in _FakeWsClient.instances for cmd in ws.commands]


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch: Any) -> None:
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.setattr(
        "ha_mcp.client.websocket_client.HomeAssistantWebSocketClient",
        _FakeWsClient,
    )
    _FakeWsClient.instances = []
    _FakeWsClient.user_data = {}
    _FakeWsClient.connect_ok = True
    _FakeWsClient.fail_get = False
    _FakeWsClient.fail_set = False


def _addon_options(**overrides: Any) -> dict[str, Any]:
    options: dict[str, Any] = {
        "access_token": "puppet-token",
        "keep_browser_open": False,
        "home_assistant_url": "http://homeassistant:8123",
    }
    options.update(overrides)
    return options


def _client(base_url: str = "http://ha.local:8123", token: str = "own-token") -> Any:
    return SimpleNamespace(base_url=base_url, token=token)


class TestCredentialResolution:
    def test_addon_options_win(self) -> None:
        cred = _addon_credential(_addon_options())
        assert cred is not None
        assert cred.url == "http://homeassistant:8123"
        assert cred.token == "puppet-token"

    def test_addon_options_default_url_when_unset(self) -> None:
        cred = _addon_credential(_addon_options(home_assistant_url=""))
        assert cred is not None
        assert cred.url == "http://homeassistant:8123"

    def test_addon_options_without_token_yield_nothing(self) -> None:
        assert _addon_credential(_addon_options(access_token="")) is None
        assert _addon_credential(_addon_options(access_token=None)) is None
        assert _addon_credential(None) is None

    def test_client_credential_used_outside_addon_mode(self) -> None:
        cred = _client_credential(_client())
        assert cred is not None
        assert cred.url == "http://ha.local:8123"
        assert cred.token == "own-token"

    def test_client_credential_refused_in_addon_mode(self, monkeypatch: Any) -> None:
        # The Supervisor proxy authenticates as the Supervisor system user,
        # not the engine's token user — protecting it would be a false no-op.
        monkeypatch.setenv("SUPERVISOR_TOKEN", "sup")
        assert _client_credential(_client()) is None

    def test_client_credential_requires_http_url_and_token(self) -> None:
        assert _client_credential(_client(base_url="oauth://pending")) is None
        assert _client_credential(_client(token="")) is None
        assert _client_credential(None) is None

    def test_for_capture_prefers_addon_options_over_client(self) -> None:
        guard = ThemeGuard.for_capture(_addon_options(), _client())
        assert guard.credential is not None
        assert guard.credential.token == "puppet-token"

    def test_for_capture_without_any_credential_is_inactive(self) -> None:
        guard = ThemeGuard.for_capture(None, None)
        assert guard.credential is None


class TestSnapshotRestore:
    async def test_restore_writes_back_clobbered_theme(self) -> None:
        """Regression #1909: an engine write is undone by the restore."""
        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_DARK_THEME)
        guard = ThemeGuard.for_capture(_addon_options(), None)

        await guard.take_snapshot()
        assert guard.snapshot == _DARK_THEME

        # The engine's settheme dispatch persists light mode server-side.
        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_CLOBBERED_THEME)

        await guard.restore()
        assert _FakeWsClient.user_data[THEME_USER_DATA_KEY] == _DARK_THEME
        assert guard.warnings == []

    async def test_restore_skips_write_when_unchanged(self) -> None:
        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_DARK_THEME)
        guard = ThemeGuard.for_capture(_addon_options(), None)

        await guard.take_snapshot()
        await guard.restore()

        set_calls = [
            cmd for cmd in _all_commands() if cmd["type"] == "frontend/set_user_data"
        ]
        assert set_calls == []

    async def test_never_configured_theme_round_trips_as_none(self) -> None:
        guard = ThemeGuard.for_capture(_addon_options(), None)

        await guard.take_snapshot()
        assert guard.snapshot is None
        assert guard.snapshot_taken is True

        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_CLOBBERED_THEME)
        await guard.restore()
        assert _FakeWsClient.user_data[THEME_USER_DATA_KEY] is None

    async def test_sessions_are_closed_after_each_phase(self) -> None:
        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_DARK_THEME)
        guard = ThemeGuard.for_capture(_addon_options(), None)
        await guard.take_snapshot()
        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_CLOBBERED_THEME)
        await guard.restore()
        assert len(_FakeWsClient.instances) == 2
        assert all(ws.disconnected for ws in _FakeWsClient.instances)

    async def test_inactive_guard_touches_nothing(self) -> None:
        guard = ThemeGuard.for_capture(None, None)
        await guard.take_snapshot()
        await guard.restore()
        assert _FakeWsClient.instances == []
        assert guard.warnings == []

    async def test_snapshot_failure_warns_and_disables_restore(self) -> None:
        _FakeWsClient.fail_get = True
        guard = ThemeGuard.for_capture(_addon_options(), None)

        await guard.take_snapshot()
        assert guard.snapshot_taken is False
        assert len(guard.warnings) == 1

        _FakeWsClient.fail_get = False
        await guard.restore()
        # Without a trustworthy snapshot the guard must not write anything.
        set_calls = [
            cmd for cmd in _all_commands() if cmd["type"] == "frontend/set_user_data"
        ]
        assert set_calls == []

    async def test_connect_failure_warns_without_raising(self) -> None:
        _FakeWsClient.connect_ok = False
        guard = ThemeGuard.for_capture(_addon_options(), None)
        await guard.take_snapshot()
        assert guard.snapshot_taken is False
        assert len(guard.warnings) == 1

    async def test_restore_failure_warns_without_raising(self) -> None:
        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_DARK_THEME)
        guard = ThemeGuard.for_capture(_addon_options(), None)
        await guard.take_snapshot()

        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_CLOBBERED_THEME)
        _FakeWsClient.fail_set = True
        await guard.restore()
        assert len(guard.warnings) == 1
        assert "restoring it failed" in guard.warnings[0]


class _FakeResponse:
    def __init__(self, status_code: int, content: bytes) -> None:
        self.status_code = status_code
        self.content = content
        self.headers = {"content-type": "image/png"}

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        yield self.content


class _FakeStreamContext:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeResponse:
        return self._response

    async def __aexit__(self, *_a: Any) -> None:
        return None


class _ClobberingEngineClient:
    """Fake httpx.AsyncClient whose render flips the stored theme to light."""

    status_code: ClassVar[int] = 200

    def __init__(self, *_a: Any, **_kw: Any) -> None:
        pass

    async def __aenter__(self) -> _ClobberingEngineClient:
        return self

    async def __aexit__(self, *_a: Any) -> None:
        return None

    def stream(
        self, method: str, url: str, params: dict[str, Any] | None = None
    ) -> _FakeStreamContext:
        # Rendering makes the engine's frontend session persist light mode,
        # exactly like Puppet's cold-browser settheme dispatch does.
        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_CLOBBERED_THEME)
        return _FakeStreamContext(_FakeResponse(type(self).status_code, _PNG))


class TestCaptureBracket:
    """The guard brackets capture_dashboard_images end to end."""

    @pytest.fixture(autouse=True)
    def _engine(self, monkeypatch: Any) -> None:
        from ha_mcp.dashboard_screenshot import capture

        async def fake_resolve() -> EngineTarget:
            return EngineTarget(
                url="http://engine:10000", addon_options=_addon_options()
            )

        monkeypatch.setattr(capture, "resolve_engine", fake_resolve)
        monkeypatch.setattr(capture.httpx, "AsyncClient", _ClobberingEngineClient)
        _ClobberingEngineClient.status_code = 200

    async def test_default_render_clobber_is_restored_issue_1909(self) -> None:
        from ha_mcp.dashboard_screenshot import capture

        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_DARK_THEME)
        capture_warnings: list[str] = []

        captures = await capture.capture_dashboard_images(
            "lovelace/0", capture_warnings=capture_warnings
        )

        assert captures[0].data == _PNG
        assert _FakeWsClient.user_data[THEME_USER_DATA_KEY] == _DARK_THEME
        assert capture_warnings == []

    async def test_restore_runs_even_when_capture_fails(self) -> None:
        from ha_mcp.dashboard_screenshot import capture

        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_DARK_THEME)
        _ClobberingEngineClient.status_code = 500
        capture_warnings: list[str] = []

        with pytest.raises(ToolError):
            await capture.capture_dashboard_images(
                "lovelace/0", capture_warnings=capture_warnings
            )

        assert _FakeWsClient.user_data[THEME_USER_DATA_KEY] == _DARK_THEME

    async def test_restore_failure_surfaces_as_capture_warning(self) -> None:
        from ha_mcp.dashboard_screenshot import capture

        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_DARK_THEME)
        _FakeWsClient.fail_set = True
        capture_warnings: list[str] = []

        captures = await capture.capture_dashboard_images(
            "lovelace/0", capture_warnings=capture_warnings
        )

        assert captures[0].data == _PNG
        assert len(capture_warnings) == 1
        assert "restoring it failed" in capture_warnings[0]
