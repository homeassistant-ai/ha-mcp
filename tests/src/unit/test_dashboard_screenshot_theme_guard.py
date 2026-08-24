"""Unit tests for the dashboard-screenshot theme guard (issue #1909).

Puppet dispatches a ``settheme`` event on cold renders, and Home Assistant
persists it server-side on the engine token user's profile — flipping that
user's real sessions. The guard detects that change and reports it so the
agent can undo it with ``ha_manage_theme``; it never writes itself. These cover
credential resolution and detection semantics against a fake WebSocket
client, plus ``TestCaptureBracket``, which asserts that a capture reads but
issues no ``frontend/set_user_data`` at all — the property that keeps the
screenshot tools honestly ``readOnlyHint: True``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.dashboard_screenshot.provision import EngineTarget
from ha_mcp.dashboard_screenshot.theme_guard import (
    THEME_USER_DATA_KEY,
    EngineCredential,
    ThemeGuard,
    _client_credential,
    addon_credential_from_options,
)

_PNG = b"\x89PNG\r\n\x1a\nunit"
_DARK_THEME = {"theme": "default", "dark": True}
_CLOBBERED_THEME = {"theme": "", "dark": False}
_PUPPET_CREDENTIAL = EngineCredential(
    url="http://homeassistant:8123", token="puppet-token"
)


class _FakeWsClient:
    """Scriptable HomeAssistantWebSocketClient stand-in with a user-data store."""

    instances: ClassVar[list[_FakeWsClient]] = []
    user_data: ClassVar[dict[str, Any]] = {}
    connect_ok: ClassVar[bool] = True
    connect_error_reason: ClassVar[str | None] = "auth_invalid"
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
            self.last_connect_error = _FakeWsClient.connect_error_reason
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


def _set_calls() -> list[dict[str, Any]]:
    return [cmd for cmd in _all_commands() if cmd["type"] == "frontend/set_user_data"]


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch: Any) -> None:
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    # The real write-settle delay only matters against a live engine.
    monkeypatch.setattr(
        "ha_mcp.dashboard_screenshot.theme_guard.RESTORE_SETTLE_SECONDS", 0
    )
    monkeypatch.setattr(
        "ha_mcp.client.websocket_client.HomeAssistantWebSocketClient",
        _FakeWsClient,
    )
    _FakeWsClient.instances = []
    _FakeWsClient.user_data = {}
    _FakeWsClient.connect_ok = True
    _FakeWsClient.connect_error_reason = "auth_invalid"
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
    def test_addon_options_yield_engine_credential(self) -> None:
        assert addon_credential_from_options(_addon_options()) == _PUPPET_CREDENTIAL

    def test_addon_options_default_url_when_unset(self) -> None:
        cred = addon_credential_from_options(_addon_options(home_assistant_url=""))
        assert cred is not None
        assert cred.url == "http://homeassistant:8123"

    def test_addon_options_without_token_yield_nothing(self) -> None:
        assert addon_credential_from_options(_addon_options(access_token="")) is None
        assert addon_credential_from_options(_addon_options(access_token=None)) is None
        assert addon_credential_from_options(None) is None

    def test_client_credential_used_outside_addon_mode(self) -> None:
        cred = _client_credential(_client())
        assert cred == EngineCredential(url="http://ha.local:8123", token="own-token")

    def test_client_credential_refused_in_addon_mode(self, monkeypatch: Any) -> None:
        # The Supervisor proxy authenticates as the Supervisor system user,
        # not the engine's token user — protecting it would be a false no-op.
        monkeypatch.setenv("SUPERVISOR_TOKEN", "sup")
        assert _client_credential(_client()) is None

    def test_client_credential_allowed_in_embedded_mode(self, monkeypatch: Any) -> None:
        # Embedded mode runs inside the HA core container (which carries
        # SUPERVISOR_TOKEN) but authenticates as a plain admin client with a
        # real user token — the fallback must stay active there.
        monkeypatch.setenv("SUPERVISOR_TOKEN", "sup")
        monkeypatch.setenv("HA_MCP_EMBEDDED", "1")
        assert _client_credential(_client()) == EngineCredential(
            url="http://ha.local:8123", token="own-token"
        )

    def test_client_credential_requires_http_url_and_token(self) -> None:
        assert _client_credential(_client(base_url="oauth://pending")) is None
        assert _client_credential(_client(token="")) is None
        assert _client_credential(None) is None

    def test_for_capture_prefers_addon_credential_over_client(self) -> None:
        guard = ThemeGuard.for_capture(_PUPPET_CREDENTIAL, _client())
        assert guard.credential == _PUPPET_CREDENTIAL

    def test_for_capture_falls_back_to_client_credential(self) -> None:
        guard = ThemeGuard.for_capture(None, _client())
        assert guard.credential == EngineCredential(
            url="http://ha.local:8123", token="own-token"
        )

    def test_for_capture_without_any_credential_is_inactive(self) -> None:
        guard = ThemeGuard.for_capture(None, None)
        assert guard.credential is None


class TestSnapshotRestore:
    async def test_detect_reports_clobbered_theme_without_writing(self) -> None:
        """#1909, read-only form: the change is reported, never undone."""
        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_DARK_THEME)
        guard = ThemeGuard.for_capture(_PUPPET_CREDENTIAL, None)

        await guard.take_snapshot()

        # The engine's settheme dispatch persists light mode server-side.
        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_CLOBBERED_THEME)

        await guard.detect_change()

        # The guard issued no write: the clobber is still in place.
        assert _set_calls() == []
        assert _FakeWsClient.user_data[THEME_USER_DATA_KEY] == _CLOBBERED_THEME
        assert guard.changed_from == _DARK_THEME
        assert len(guard.warnings) == 1
        warning = guard.warnings[0]
        assert "ha_manage_theme" in warning
        assert "set_engine_theme" in warning
        assert "own Home Assistant user" in warning

    async def test_sessions_use_the_resolved_credential(self) -> None:
        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_DARK_THEME)
        guard = ThemeGuard.for_capture(_PUPPET_CREDENTIAL, None)
        await guard.take_snapshot()
        assert [(ws.url, ws.token) for ws in _FakeWsClient.instances] == [
            ("http://homeassistant:8123", "puppet-token")
        ]

    async def test_detect_is_silent_when_theme_unchanged(self) -> None:
        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_DARK_THEME)
        guard = ThemeGuard.for_capture(_PUPPET_CREDENTIAL, None)
        await guard.take_snapshot()
        await guard.detect_change()
        assert _set_calls() == []
        assert guard.warnings == []
        assert guard.changed_from is None

    @pytest.mark.parametrize(
        "response", [{"success": True, "result": None}, None, "not-a-dict"]
    )
    async def test_fetch_theme_tolerates_malformed_responses(
        self, response: Any
    ) -> None:
        class _MalformedWs:
            async def send_command(self, command_type: str, **kwargs: Any) -> Any:
                return response

        assert await ThemeGuard._fetch_theme(_MalformedWs()) is None  # type: ignore[arg-type]

    async def test_sessions_are_closed_after_each_phase(self) -> None:
        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_DARK_THEME)
        guard = ThemeGuard.for_capture(_PUPPET_CREDENTIAL, None)
        await guard.take_snapshot()
        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_CLOBBERED_THEME)
        await guard.detect_change()
        assert len(_FakeWsClient.instances) == 2
        assert all(ws.disconnected for ws in _FakeWsClient.instances)

    async def test_inactive_guard_touches_nothing(self) -> None:
        guard = ThemeGuard.for_capture(None, None)
        await guard.take_snapshot()
        await guard.detect_change()
        assert _FakeWsClient.instances == []
        assert guard.warnings == []

    async def test_snapshot_failure_warns_and_disables_detection(self) -> None:
        _FakeWsClient.fail_get = True
        guard = ThemeGuard.for_capture(_PUPPET_CREDENTIAL, None)

        await guard.take_snapshot()
        assert len(guard.warnings) == 1

        _FakeWsClient.fail_get = False
        await guard.detect_change()
        # Without a trustworthy snapshot there is nothing to compare against.
        assert _set_calls() == []
        assert guard.changed_from is None

    @pytest.mark.parametrize("reason", ["auth_invalid", None])
    async def test_connect_failure_warns_without_raising(
        self, reason: str | None
    ) -> None:
        _FakeWsClient.connect_ok = False
        _FakeWsClient.connect_error_reason = reason
        guard = ThemeGuard.for_capture(_PUPPET_CREDENTIAL, None)
        await guard.take_snapshot()
        assert len(guard.warnings) == 1
        await guard.detect_change()
        assert _set_calls() == []

    async def test_detection_waits_for_engine_write_to_settle(
        self, monkeypatch: Any
    ) -> None:
        """The post-capture read must not race Puppet's async user-data save."""
        from ha_mcp.dashboard_screenshot import theme_guard as guard_module

        monkeypatch.setattr(guard_module, "RESTORE_SETTLE_SECONDS", 1.5)
        sleeps: list[float] = []

        async def record_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr(guard_module.asyncio, "sleep", record_sleep)

        guard = ThemeGuard.for_capture(_PUPPET_CREDENTIAL, None)
        await guard.take_snapshot()
        await guard.detect_change()
        assert sleeps == [1.5]

    async def test_detection_read_failure_warns_without_raising(self) -> None:
        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_DARK_THEME)
        guard = ThemeGuard.for_capture(_PUPPET_CREDENTIAL, None)
        await guard.take_snapshot()

        _FakeWsClient.fail_get = True
        await guard.detect_change()
        assert len(guard.warnings) == 1
        assert "Could not check whether" in guard.warnings[0]
        assert _set_calls() == []


class TestEngineThemeHelpers:
    """The write path lives behind ha_manage_theme, not the capture path."""

    async def test_read_engine_theme_returns_saved_value(self) -> None:
        from ha_mcp.dashboard_screenshot.theme_guard import read_engine_theme

        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_DARK_THEME)
        assert await read_engine_theme(_PUPPET_CREDENTIAL) == _DARK_THEME

    async def test_write_engine_theme_sets_the_value(self) -> None:
        from ha_mcp.dashboard_screenshot.theme_guard import write_engine_theme

        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_CLOBBERED_THEME)
        # The guard is on by default now, so an unconditional write is
        # explicit: omitting expected_current means "expect no stored theme".
        await write_engine_theme(_PUPPET_CREDENTIAL, dict(_DARK_THEME), force=True)
        assert _FakeWsClient.user_data[THEME_USER_DATA_KEY] == _DARK_THEME
        assert len(_set_calls()) == 1


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
    clobber: ClassVar[bool] = True

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
        if type(self).clobber:
            _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_CLOBBERED_THEME)
        return _FakeStreamContext(_FakeResponse(type(self).status_code, _PNG))


def _patch_engine(monkeypatch: Any, addon_credential: EngineCredential | None) -> None:
    from ha_mcp.dashboard_screenshot import capture

    async def fake_resolve() -> EngineTarget:
        return EngineTarget(
            url="http://engine:10000", addon_credential=addon_credential
        )

    monkeypatch.setattr(capture, "resolve_engine", fake_resolve)
    monkeypatch.setattr(capture.httpx, "AsyncClient", _ClobberingEngineClient)
    _ClobberingEngineClient.status_code = 200
    _ClobberingEngineClient.clobber = True


class TestCaptureBracket:
    """Capture reads the engine user's theme and reports changes, never writes.

    The no-write property is what keeps the screenshot and dashboard-get tools
    honestly ``readOnlyHint: True`` (#1991, PR #2014) while still surfacing the
    engine's #1909 clobber to the agent.
    """

    async def test_capture_reports_the_clobber_and_writes_nothing(
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture

        _patch_engine(monkeypatch, _PUPPET_CREDENTIAL)
        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_DARK_THEME)
        capture_warnings: list[str] = []

        captures = await capture.capture_dashboard_images(
            "lovelace/0", capture_warnings=capture_warnings
        )

        assert captures[0].data == _PNG
        # Two read sessions (before + after); no write of any kind.
        assert len(_FakeWsClient.instances) == 2
        assert _set_calls() == []
        # The fake engine clobbered the theme and it stays clobbered.
        assert _FakeWsClient.user_data[THEME_USER_DATA_KEY] == _CLOBBERED_THEME
        assert len(capture_warnings) == 1
        assert "ha_manage_theme" in capture_warnings[0]

    async def test_unchanged_theme_produces_no_warning(self, monkeypatch: Any) -> None:
        """A non-clobbering engine must not nag the agent."""
        from ha_mcp.dashboard_screenshot import capture

        _patch_engine(monkeypatch, _PUPPET_CREDENTIAL)
        _ClobberingEngineClient.clobber = False
        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_DARK_THEME)
        capture_warnings: list[str] = []

        await capture.capture_dashboard_images(
            "lovelace/0", capture_warnings=capture_warnings
        )

        assert capture_warnings == []
        assert _set_calls() == []
        assert _FakeWsClient.user_data[THEME_USER_DATA_KEY] == _DARK_THEME

    async def test_client_credential_fallback_detects_the_clobber(
        self, monkeypatch: Any
    ) -> None:
        """Sidecar/standalone mode: the client credential drives detection."""
        from ha_mcp.dashboard_screenshot import capture

        _patch_engine(monkeypatch, None)
        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_DARK_THEME)

        captures = await capture.capture_dashboard_images(
            "lovelace/0", client=_client()
        )

        assert captures[0].data == _PNG
        assert [(ws.url, ws.token) for ws in _FakeWsClient.instances] == [
            ("http://ha.local:8123", "own-token"),
            ("http://ha.local:8123", "own-token"),
        ]
        assert _set_calls() == []

    async def test_capture_failure_still_reports_the_clobber(
        self, monkeypatch: Any
    ) -> None:
        """A failed render still leaves the theme flipped -- say so."""
        import json

        from ha_mcp.dashboard_screenshot import capture

        _patch_engine(monkeypatch, _PUPPET_CREDENTIAL)
        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_DARK_THEME)
        _ClobberingEngineClient.status_code = 500

        with pytest.raises(ToolError) as exc_info:
            await capture.capture_dashboard_images("lovelace/0")

        assert _set_calls() == []
        payload = json.loads(str(exc_info.value))
        assert any(
            "ha_manage_theme" in warning for warning in payload.get("warnings", [])
        )


class TestCompareAndSetWrite:
    """The restore is a compare-and-set, not a blind overwrite."""

    async def test_write_refuses_when_current_does_not_match(self) -> None:
        from ha_mcp.dashboard_screenshot.theme_guard import (
            ThemeChangedError,
            write_engine_theme,
        )

        # Someone changed the theme after the warning was emitted.
        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = {"theme": "nord"}
        with pytest.raises(ThemeChangedError) as exc_info:
            await write_engine_theme(
                _PUPPET_CREDENTIAL,
                dict(_DARK_THEME),
                expected_current=dict(_CLOBBERED_THEME),
            )

        assert exc_info.value.actual == {"theme": "nord"}
        assert _set_calls() == []
        assert _FakeWsClient.user_data[THEME_USER_DATA_KEY] == {"theme": "nord"}

    async def test_write_proceeds_when_current_matches(self) -> None:
        from ha_mcp.dashboard_screenshot.theme_guard import write_engine_theme

        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_CLOBBERED_THEME)
        await write_engine_theme(
            _PUPPET_CREDENTIAL,
            dict(_DARK_THEME),
            expected_current=dict(_CLOBBERED_THEME),
        )
        assert _FakeWsClient.user_data[THEME_USER_DATA_KEY] == _DARK_THEME

    async def test_warning_carries_both_values_for_the_guard(self) -> None:
        """The agent needs expected_current, not just the value to restore."""
        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_DARK_THEME)
        guard = ThemeGuard.for_capture(_PUPPET_CREDENTIAL, None)
        await guard.take_snapshot()
        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_CLOBBERED_THEME)
        await guard.detect_change()

        warning = guard.warnings[0]
        assert "value=" in warning
        assert "expected_current=" in warning


class TestSessionCleanup:
    """A cancelled guard session must not strand its WebSocket."""

    async def test_cancelled_connect_still_disconnects(self, monkeypatch: Any) -> None:
        """SESSION_TIMEOUT_SECONDS cancels via CancelledError, a BaseException.

        connect() sits inside the session's try/finally precisely so that
        cancellation there still runs disconnect(); leaving it outside
        stranded the socket and its background reader task.
        """
        started = asyncio.Event()

        async def hang_connect(self: Any) -> bool:
            started.set()
            await asyncio.sleep(3600)
            return True

        with monkeypatch.context() as patched:
            patched.setattr(_FakeWsClient, "connect", hang_connect)
            guard = ThemeGuard.for_capture(_PUPPET_CREDENTIAL, None)
            task = asyncio.ensure_future(guard.take_snapshot())
            await asyncio.wait_for(started.wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                _ = await task

        assert len(_FakeWsClient.instances) == 1
        assert _FakeWsClient.instances[0].disconnected is True


class TestNullExpectedCurrentGuard:
    """An explicit null expected_current must guard, not mean 'unguarded'."""

    async def test_null_expected_current_refuses_when_a_theme_appeared(
        self,
    ) -> None:
        from ha_mcp.dashboard_screenshot.theme_guard import (
            ThemeChangedError,
            write_engine_theme,
        )

        # Warning observed a never-configured theme (None); by the time the
        # agent restores, the user has picked one.
        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_DARK_THEME)
        with pytest.raises(ThemeChangedError):
            await write_engine_theme(
                _PUPPET_CREDENTIAL, dict(_CLOBBERED_THEME), expected_current=None
            )
        assert _set_calls() == []

    async def test_null_expected_current_writes_when_still_null(self) -> None:
        from ha_mcp.dashboard_screenshot.theme_guard import write_engine_theme

        await write_engine_theme(
            _PUPPET_CREDENTIAL, dict(_DARK_THEME), expected_current=None
        )
        assert _FakeWsClient.user_data[THEME_USER_DATA_KEY] == _DARK_THEME

    async def test_force_skips_the_guard(self) -> None:
        from ha_mcp.dashboard_screenshot.theme_guard import write_engine_theme

        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_DARK_THEME)
        await write_engine_theme(
            _PUPPET_CREDENTIAL,
            dict(_CLOBBERED_THEME),
            expected_current=None,
            force=True,
        )
        assert _FakeWsClient.user_data[THEME_USER_DATA_KEY] == _CLOBBERED_THEME
