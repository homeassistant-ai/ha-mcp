"""Unit tests for the dashboard-screenshot theme guard (issue #1909).

Stock Puppet dispatched a ``settheme`` event on cold renders, and Home
Assistant persisted it server-side on the engine token user's profile —
flipping that user's real sessions to light mode. The guard snapshots the
saved theme before a capture batch and restores it afterwards.

These cover the guard's credential resolution and snapshot/restore semantics
against a fake WebSocket client, plus ``TestCaptureBracket``, which asserts
``capture.py`` actually brackets each batch — guarding against the bracket
being silently disabled again.
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
        self.verify_ssl = verify_ssl
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
    async def test_restore_writes_back_clobbered_theme(self) -> None:
        """Regression #1909: an engine write is undone by the restore."""
        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_DARK_THEME)
        guard = ThemeGuard.for_capture(_PUPPET_CREDENTIAL, None)

        await guard.take_snapshot()

        # The engine's settheme dispatch persists light mode server-side.
        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_CLOBBERED_THEME)

        await guard.restore()
        assert _FakeWsClient.user_data[THEME_USER_DATA_KEY] == _DARK_THEME
        assert guard.warnings == []

    async def test_sessions_use_the_resolved_credential(self) -> None:
        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_DARK_THEME)
        guard = ThemeGuard.for_capture(_PUPPET_CREDENTIAL, None)
        await guard.take_snapshot()
        assert [(ws.url, ws.token) for ws in _FakeWsClient.instances] == [
            ("http://homeassistant:8123", "puppet-token")
        ]
        await guard.restore()

    async def test_restore_skips_write_when_unchanged(self) -> None:
        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_DARK_THEME)
        guard = ThemeGuard.for_capture(_PUPPET_CREDENTIAL, None)

        await guard.take_snapshot()
        await guard.restore()

        assert _set_calls() == []

    async def test_unconfigured_theme_stays_unwritten_when_unchanged(self) -> None:
        guard = ThemeGuard.for_capture(_PUPPET_CREDENTIAL, None)
        await guard.take_snapshot()
        await guard.restore()
        assert _set_calls() == []

    async def test_never_configured_theme_restores_as_empty_settings(self) -> None:
        # Live frontend sessions ignore a null subscription push, so a
        # never-configured baseline restores as {} — equivalent "no explicit
        # selection" semantics that subscribed sessions actually re-apply.
        guard = ThemeGuard.for_capture(_PUPPET_CREDENTIAL, None)

        await guard.take_snapshot()

        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_CLOBBERED_THEME)
        await guard.restore()
        assert _FakeWsClient.user_data[THEME_USER_DATA_KEY] == {}

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
        guard = ThemeGuard.for_capture(_PUPPET_CREDENTIAL, None)

        await guard.take_snapshot()
        assert len(guard.warnings) == 1

        _FakeWsClient.fail_get = False
        await guard.restore()
        # Without a trustworthy snapshot the guard must not write anything.
        assert _set_calls() == []

    @pytest.mark.parametrize("reason", ["auth_invalid", None])
    async def test_connect_failure_warns_without_raising(
        self, reason: str | None
    ) -> None:
        _FakeWsClient.connect_ok = False
        _FakeWsClient.connect_error_reason = reason
        guard = ThemeGuard.for_capture(_PUPPET_CREDENTIAL, None)
        await guard.take_snapshot()
        assert len(guard.warnings) == 1
        await guard.restore()
        assert _set_calls() == []

    async def test_restore_waits_for_engine_write_to_settle(
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
        await guard.restore()
        assert sleeps == [1.5]

    async def test_restore_failure_warns_without_raising(self) -> None:
        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_DARK_THEME)
        guard = ThemeGuard.for_capture(_PUPPET_CREDENTIAL, None)
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


def _patch_engine(monkeypatch: Any, addon_credential: EngineCredential | None) -> None:
    from ha_mcp.dashboard_screenshot import capture

    async def fake_resolve() -> EngineTarget:
        return EngineTarget(
            url="http://engine:10000", addon_credential=addon_credential
        )

    monkeypatch.setattr(capture, "resolve_engine", fake_resolve)
    monkeypatch.setattr(capture.httpx, "AsyncClient", _ClobberingEngineClient)
    _ClobberingEngineClient.status_code = 200


class TestCaptureBracket:
    """The ThemeGuard bracket is armed only for renders that request a theme.

    Puppet writes the engine token user's saved theme when a render asks for
    one (#1909); upstream stopped only the no-parameter dispatch
    (balloob/home-assistant-addons#89), so themed renders still write on every
    engine version. Unthemed captures are therefore left unbracketed and issue
    no writes at all, which is what keeps ``readOnlyHint: True`` honest on the
    screenshot tools (#1991, PR #2014).
    """

    async def test_unthemed_capture_opens_no_guard_sessions(
        self, monkeypatch: Any
    ) -> None:
        """The read-only guarantee: no theme requested means no writes."""
        from ha_mcp.dashboard_screenshot import capture

        _patch_engine(monkeypatch, _PUPPET_CREDENTIAL)
        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_DARK_THEME)
        capture_warnings: list[str] = []

        captures = await capture.capture_dashboard_images(
            "lovelace/0", capture_warnings=capture_warnings
        )

        assert captures[0].data == _PNG
        assert _FakeWsClient.instances == []
        assert _set_calls() == []
        assert capture_warnings == []

    async def test_dark_mode_capture_restores_the_clobbered_theme(
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture

        _patch_engine(monkeypatch, _PUPPET_CREDENTIAL)
        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_DARK_THEME)
        capture_warnings: list[str] = []

        captures = await capture.capture_dashboard_images(
            "lovelace/0", dark_mode=True, capture_warnings=capture_warnings
        )

        assert captures[0].data == _PNG
        # Snapshot and restore each opened a session as the engine user.
        assert [(ws.url, ws.token) for ws in _FakeWsClient.instances] == [
            ("http://homeassistant:8123", "puppet-token"),
            ("http://homeassistant:8123", "puppet-token"),
        ]
        assert _FakeWsClient.user_data[THEME_USER_DATA_KEY] == _DARK_THEME
        assert capture_warnings == []

    async def test_explicit_theme_capture_restores_the_clobbered_theme(
        self, monkeypatch: Any
    ) -> None:
        from ha_mcp.dashboard_screenshot import capture

        _patch_engine(monkeypatch, _PUPPET_CREDENTIAL)
        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_DARK_THEME)

        captures = await capture.capture_dashboard_images("lovelace/0", theme="shadow")

        assert captures[0].data == _PNG
        assert _FakeWsClient.user_data[THEME_USER_DATA_KEY] == _DARK_THEME

    async def test_client_credential_fallback_restores_theme(
        self, monkeypatch: Any
    ) -> None:
        """Sidecar/standalone mode: the client credential drives the bracket."""
        from ha_mcp.dashboard_screenshot import capture

        _patch_engine(monkeypatch, None)
        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_DARK_THEME)

        captures = await capture.capture_dashboard_images(
            "lovelace/0", dark_mode=True, client=_client()
        )

        assert captures[0].data == _PNG
        assert [(ws.url, ws.token) for ws in _FakeWsClient.instances] == [
            ("http://ha.local:8123", "own-token"),
            ("http://ha.local:8123", "own-token"),
        ]
        assert _FakeWsClient.user_data[THEME_USER_DATA_KEY] == _DARK_THEME

    async def test_capture_failure_still_restores_theme(self, monkeypatch: Any) -> None:
        """A failed render must not leave the engine user's theme clobbered."""
        import json

        from ha_mcp.dashboard_screenshot import capture

        _patch_engine(monkeypatch, _PUPPET_CREDENTIAL)
        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_DARK_THEME)
        _ClobberingEngineClient.status_code = 500

        with pytest.raises(ToolError) as exc_info:
            await capture.capture_dashboard_images("lovelace/0", dark_mode=True)

        # The restore in the ``finally`` ran before the error surfaced.
        assert _FakeWsClient.user_data[THEME_USER_DATA_KEY] == _DARK_THEME
        payload = json.loads(str(exc_info.value))
        assert not any(
            "restoring it failed" in warning for warning in payload.get("warnings", [])
        )


class TestGuardHardening:
    """Bounded waits, TLS passthrough, and per-engine serialization."""

    def test_client_credential_carries_the_tls_override(self) -> None:
        """A self-signed direct client must not fall back to the global default."""
        client = SimpleNamespace(
            base_url="http://ha.local:8123", token="own-token", verify_ssl=False
        )
        credential = _client_credential(client)
        assert credential is not None
        assert credential.verify_ssl is False

    def test_client_credential_leaves_tls_unset_when_absent(self) -> None:
        credential = _client_credential(_client())
        assert credential is not None
        assert credential.verify_ssl is None

    async def test_sessions_pass_the_tls_override_through(self) -> None:
        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_DARK_THEME)
        guard = ThemeGuard.for_capture(
            EngineCredential(
                url="https://ha.local:8123", token="tok", verify_ssl=False
            ),
            None,
        )
        await guard.take_snapshot()
        assert [ws.verify_ssl for ws in _FakeWsClient.instances] == [False]
        await guard.restore()

    async def test_guard_commands_are_time_bounded(self) -> None:
        """The guard must never inherit send_command's 30s default wait."""
        from ha_mcp.dashboard_screenshot import theme_guard as guard_module

        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_DARK_THEME)
        guard = ThemeGuard.for_capture(_PUPPET_CREDENTIAL, None)
        await guard.take_snapshot()
        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_CLOBBERED_THEME)
        await guard.restore()

        waits = [cmd.get("_wait_timeout") for cmd in _all_commands()]
        assert waits and all(
            wait == guard_module.COMMAND_TIMEOUT_SECONDS for wait in waits
        )

    async def test_unarmed_guard_is_inert(self) -> None:
        guard = ThemeGuard.for_capture(_PUPPET_CREDENTIAL, None, armed=False)
        assert guard.credential is None
        await guard.take_snapshot()
        await guard.restore()
        assert _FakeWsClient.instances == []

    async def test_overlapping_batches_are_serialized_per_engine_user(self) -> None:
        """Regression: interleaved brackets must not persist a clobbered value."""
        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_DARK_THEME)
        first = ThemeGuard.for_capture(_PUPPET_CREDENTIAL, None)
        second = ThemeGuard.for_capture(_PUPPET_CREDENTIAL, None)

        await first.take_snapshot()
        # The second batch must block until the first releases, so it cannot
        # snapshot the value the first batch's render is about to clobber.
        pending = asyncio.ensure_future(second.take_snapshot())
        await asyncio.sleep(0)
        assert not pending.done()

        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_CLOBBERED_THEME)
        await first.restore()
        await pending
        await second.restore()

        assert _FakeWsClient.user_data[THEME_USER_DATA_KEY] == _DARK_THEME

    async def test_lock_timeout_takes_no_snapshot(self) -> None:
        """A contended bracket must not silently render unserialized."""
        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_DARK_THEME)
        holder = ThemeGuard.for_capture(_PUPPET_CREDENTIAL, None)
        await holder.take_snapshot()

        blocked = ThemeGuard.for_capture(_PUPPET_CREDENTIAL, None)
        await blocked.take_snapshot(lock_timeout=0.01)

        assert blocked.lock_timed_out is True
        assert blocked._snapshot_taken is False
        # It never opened a session, so it cannot restore a transient value.
        assert len(_FakeWsClient.instances) == 1
        await holder.restore()

    async def test_timed_out_batch_is_refused_rather_than_rendered(
        self, monkeypatch: Any
    ) -> None:
        """Regression: the saved theme survives a contended themed capture."""
        from ha_mcp.dashboard_screenshot import capture

        _patch_engine(monkeypatch, _PUPPET_CREDENTIAL)
        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_DARK_THEME)
        holder = ThemeGuard.for_capture(_PUPPET_CREDENTIAL, None)
        await holder.take_snapshot()

        # MIN_RENDER_TIMEOUT_SECONDS is 1.0, so this is the shortest budget
        # the validator accepts; anything lower would fail validation instead
        # and make this test pass for the wrong reason.
        with pytest.raises(ToolError) as exc_info:
            await capture.capture_dashboard_images(
                "lovelace/0", dark_mode=True, render_timeout_seconds=1.0
            )
        assert "still in progress" in str(exc_info.value)

        # The refused batch never rendered, so nothing clobbered the theme.
        assert _FakeWsClient.user_data[THEME_USER_DATA_KEY] == _DARK_THEME
        await holder.restore()
        assert _FakeWsClient.user_data[THEME_USER_DATA_KEY] == _DARK_THEME

    async def test_cancelled_snapshot_releases_the_lock(self, monkeypatch: Any) -> None:
        """CancelledError is a BaseException and must not strand the lock."""
        started = asyncio.Event()

        async def hang(_ws: Any) -> Any:
            started.set()
            await asyncio.sleep(3600)

        monkeypatch.setattr(ThemeGuard, "_fetch_theme", staticmethod(hang))
        guard = ThemeGuard.for_capture(_PUPPET_CREDENTIAL, None)
        task = asyncio.ensure_future(guard.take_snapshot())
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        monkeypatch.undo()
        _FakeWsClient.user_data[THEME_USER_DATA_KEY] = dict(_DARK_THEME)
        other = ThemeGuard.for_capture(_PUPPET_CREDENTIAL, None)
        await asyncio.wait_for(other.take_snapshot(lock_timeout=1), timeout=2)
        assert other.lock_timed_out is False
        await other.restore()

    async def test_failed_snapshot_does_not_hold_the_lock(self) -> None:
        _FakeWsClient.fail_get = True
        guard = ThemeGuard.for_capture(_PUPPET_CREDENTIAL, None)
        await guard.take_snapshot()
        assert guard.warnings

        _FakeWsClient.fail_get = False
        other = ThemeGuard.for_capture(_PUPPET_CREDENTIAL, None)
        await asyncio.wait_for(other.take_snapshot(), timeout=1)
        await other.restore()
