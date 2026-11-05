"""Unit tests for the ``ha_mcp_tools`` capability gate (``component_api``).

Covers the negotiation the server does before routing a read tool through the
custom component: the single cached ``ha_mcp_tools/info`` probe, the
cache-on-failure taxonomy, and the ``code``-based ``unknown_command`` detector.
The WebSocket client is an ``AsyncMock`` whose ``send_command`` dispatches on
the command type, mirroring the live component's info/search surface.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from ha_mcp.client.rest_client import (
    HomeAssistantAdminRequiredError,
    HomeAssistantAuthError,
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ha_mcp.tools import component_api
from ha_mcp.tools.component_api import (
    SUPPORTED_SCHEMA_VERSION,
    ComponentCaps,
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)

_INFO_OK = {
    "schema_version": 1,
    "component_version": "1.1.0",
    "capabilities": ["search", "overview"],
    "limits": {"max_results": 500},
}


class _FakeClient:
    """Weakref-able credentialed client stand-in (real REST client is a class)."""

    def __init__(
        self,
        base_url: str = "http://ha.local:8123",
        token: str = "tok",
        verify_ssl: bool | None = None,
    ):
        self.base_url = base_url
        self.token = token
        self.verify_ssl = verify_ssl


class _BareClient:
    """Weakref-able client with no credentials (nothing to negotiate over)."""


def _client() -> Any:
    return _FakeClient()


def _make_ws(
    *,
    info_result: dict[str, Any] | None = None,
    info_exc: Exception | None = None,
) -> AsyncMock:
    ws = AsyncMock()

    async def _send(command_type: str, **kwargs: Any) -> dict[str, Any]:
        if command_type == "ha_mcp_tools/info":
            if info_exc is not None:
                raise info_exc
            return {"success": True, "result": info_result}
        raise AssertionError(f"unexpected command {command_type!r}")

    ws.send_command = AsyncMock(side_effect=_send)
    return ws


def _patch_ws(ws: AsyncMock) -> Any:
    return patch.object(
        component_api, "get_websocket_client", AsyncMock(return_value=ws)
    )


@pytest.mark.asyncio
async def test_info_probe_parses_capabilities() -> None:
    ws = _make_ws(info_result=_INFO_OK)
    with _patch_ws(ws):
        caps = await get_component_caps(_client())

    assert isinstance(caps, ComponentCaps)
    assert caps.capabilities == frozenset({"search", "overview"})
    assert caps.schema_version == 1
    assert caps.component_version == "1.1.0"
    assert caps.limits == {"max_results": 500}
    # _INFO_OK carries no timezone (a component too old to report it): None.
    assert caps.timezone is None


@pytest.mark.asyncio
async def test_info_probe_threads_verify_ssl() -> None:
    """The caps probe threads the client's ``verify_ssl`` into
    ``get_websocket_client`` so a ``verify_ssl=False`` client can establish (and key)
    its own pooled connection — otherwise every component capability is silently
    unreachable on an HTTPS + verify_ssl=False install."""
    ws = _make_ws(info_result=_INFO_OK)
    factory = AsyncMock(return_value=ws)
    with patch.object(component_api, "get_websocket_client", factory):
        caps = await get_component_caps(_FakeClient(verify_ssl=False))

    assert isinstance(caps, ComponentCaps)
    assert factory.await_args is not None
    assert factory.await_args.kwargs.get("verify_ssl") is False


@pytest.mark.asyncio
async def test_info_probe_parses_timezone_present() -> None:
    """An ``info`` payload carrying ``timezone`` (a newer component) is cached."""
    ws = _make_ws(info_result={**_INFO_OK, "timezone": "Europe/London"})
    with _patch_ws(ws):
        caps = await get_component_caps(_client())

    assert isinstance(caps, ComponentCaps)
    assert caps.timezone == "Europe/London"


@pytest.mark.asyncio
async def test_info_probe_non_string_timezone_is_none() -> None:
    """A non-string ``timezone`` (malformed / drift) is ignored (None)."""
    ws = _make_ws(info_result={**_INFO_OK, "timezone": 123})
    with _patch_ws(ws):
        caps = await get_component_caps(_client())

    assert caps.timezone is None


@pytest.mark.asyncio
async def test_caps_cached_one_probe_per_client() -> None:
    ws = _make_ws(info_result=_INFO_OK)
    client = _client()
    with _patch_ws(ws):
        first = await get_component_caps(client)
        second = await get_component_caps(client)

    assert first is second
    info_calls = [
        c for c in ws.send_command.call_args_list if c.args[0] == "ha_mcp_tools/info"
    ]
    assert len(info_calls) == 1


@pytest.mark.asyncio
async def test_unknown_command_caches_none_and_does_not_reprobe() -> None:
    ws = _make_ws(
        info_exc=HomeAssistantCommandError("Command failed: x", "unknown_command")
    )
    client = _client()
    with _patch_ws(ws):
        first = await get_component_caps(client)
        second = await get_component_caps(client)

    assert first is None
    assert second is None
    # Cached negative: the info command is probed exactly once.
    assert ws.send_command.await_count == 1


@pytest.mark.asyncio
async def test_connection_error_caches_short_transient_negative(monkeypatch) -> None:
    """A WS-down probe caches a SHORT transient negative: a repeat WITHIN the window
    skips the (slow) re-connect and serves ``None`` from cache (review-5 M8)."""
    clock = _clock()
    monkeypatch.setattr(component_api, "_monotonic", lambda: clock[0])
    ws = _make_ws(info_exc=HomeAssistantConnectionError("WebSocket not authenticated"))
    client = _client()
    with _patch_ws(ws):
        assert await get_component_caps(client) is None
        # Still inside the short transient window: no re-probe.
        clock[0] += component_api._TRANSIENT_NEGATIVE_CACHE_TTL_S - 1
        assert await get_component_caps(client) is None
    assert ws.send_command.await_count == 1


@pytest.mark.asyncio
async def test_expired_transient_negative_reprobes_and_adopts(monkeypatch) -> None:
    """Past the short transient TTL the negative expires: the next call re-probes
    and adopts a now-reachable component (self-healing)."""
    clock = _clock()
    monkeypatch.setattr(component_api, "_monotonic", lambda: clock[0])

    ws = AsyncMock()
    probe_count = _clock(0.0)

    async def _send(command_type: str, **kwargs: Any) -> dict[str, Any]:
        assert command_type == "ha_mcp_tools/info"
        probe_count[0] += 1
        if probe_count[0] == 1:
            raise HomeAssistantConnectionError("WebSocket not authenticated")
        return {"success": True, "result": _INFO_OK}

    ws.send_command = AsyncMock(side_effect=_send)
    client = _client()
    with _patch_ws(ws):
        assert await get_component_caps(client) is None  # transient cached at t0
        clock[0] += component_api._TRANSIENT_NEGATIVE_CACHE_TTL_S + 1  # expire it
        caps = await get_component_caps(client)

    assert isinstance(caps, ComponentCaps)
    assert ws.send_command.await_count == 2


@pytest.mark.asyncio
async def test_establish_failure_caches_short_transient_negative(monkeypatch) -> None:
    """The ``HomeAssistantConnectionError`` ``get_websocket_client()`` raises when
    it can't build the socket (review-5 M8's slow-connect case) also caches the
    short transient negative, so a repeat within the window skips the connect
    attempt entirely."""
    clock = _clock()
    monkeypatch.setattr(component_api, "_monotonic", lambda: clock[0])
    client = _client()
    factory = AsyncMock(
        side_effect=HomeAssistantConnectionError(
            "Failed to connect to Home Assistant WebSocket"
        )
    )
    with patch.object(component_api, "get_websocket_client", factory):
        assert await get_component_caps(client) is None
        clock[0] += component_api._TRANSIENT_NEGATIVE_CACHE_TTL_S - 1
        assert await get_component_caps(client) is None
    # The connect attempt ran exactly once — the second call used the cache.
    assert factory.await_count == 1


@pytest.mark.asyncio
async def test_transient_negative_shorter_than_absent_negative(monkeypatch) -> None:
    """A transport transient negative expires FAST while an absent-negative from the
    same instant is still live — the two windows are independent."""
    assert (
        component_api._TRANSIENT_NEGATIVE_CACHE_TTL_S
        < component_api._NEGATIVE_CACHE_TTL_S
    )
    clock = _clock()
    monkeypatch.setattr(component_api, "_monotonic", lambda: clock[0])
    ws = _make_ws(info_exc=HomeAssistantConnectionError("ws down"))
    client = _client()
    with _patch_ws(ws):
        assert await get_component_caps(client) is None
        # Between the transient TTL and the absent TTL: the transient has expired,
        # so this re-probes (unlike a definitive absent-negative, which would not).
        clock[0] += component_api._TRANSIENT_NEGATIVE_CACHE_TTL_S + 1
        assert await get_component_caps(client) is None
    assert ws.send_command.await_count == 2


@pytest.mark.asyncio
async def test_no_credentials_returns_none_without_probing() -> None:
    ws = _make_ws(info_result=_INFO_OK)
    bare_client = _BareClient()  # no base_url / token
    with _patch_ws(ws):
        caps = await get_component_caps(bare_client)

    assert caps is None
    ws.send_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_malformed_info_result_caches_none() -> None:
    ws = _make_ws(info_result=None)  # success but no result dict
    client = _client()
    with _patch_ws(ws):
        first = await get_component_caps(client)
        second = await get_component_caps(client)

    assert first is None
    assert second is None
    # Repeated reads inside the failure cooldown reuse the cached fallback.
    assert ws.send_command.await_count == 1


@pytest.mark.asyncio
async def test_invalidate_caps_forces_reprobe() -> None:
    ws = _make_ws(info_result=_INFO_OK)
    client = _client()
    with _patch_ws(ws):
        await get_component_caps(client)
        invalidate_caps(client)
        await get_component_caps(client)

    assert ws.send_command.await_count == 2


@pytest.mark.asyncio
async def test_supported_schema_version_routes() -> None:
    """A probe at the supported schema_version yields usable caps."""
    info = {**_INFO_OK, "schema_version": SUPPORTED_SCHEMA_VERSION}
    ws = _make_ws(info_result=info)
    with _patch_ws(ws):
        caps = await get_component_caps(_client())
    assert isinstance(caps, ComponentCaps)
    assert component_supports(caps, "search") is True


@pytest.mark.asyncio
async def test_unsupported_schema_version_caps_none_and_warns(caplog) -> None:
    """A probe at an unsupported schema_version is a cached negative (logged once)."""
    info = {**_INFO_OK, "schema_version": SUPPORTED_SCHEMA_VERSION + 1}
    ws = _make_ws(info_result=info)
    client = _client()
    with _patch_ws(ws), caplog.at_level(logging.WARNING, logger=component_api.__name__):
        first = await get_component_caps(client)
        second = await get_component_caps(client)

    assert first is None
    assert second is None
    # Negative is cached, so the probe (and the warning) happen exactly once.
    assert ws.send_command.await_count == 1
    schema_warnings = [r for r in caplog.records if "schema_version" in r.getMessage()]
    assert len(schema_warnings) == 1
    assert str(SUPPORTED_SCHEMA_VERSION + 1) in schema_warnings[0].getMessage()
    assert str(SUPPORTED_SCHEMA_VERSION) in schema_warnings[0].getMessage()


def _clock(start: float = 1000.0) -> list[float]:
    """A one-element mutable clock; tests advance ``holder[0]`` in place."""
    return [start]


@pytest.mark.asyncio
@pytest.mark.parametrize("absence", ["unknown_command", "unsupported_schema"])
async def test_fresh_negative_does_not_reprobe(monkeypatch, absence) -> None:
    """Definitive absence keeps the five-minute cache, not the failure cooldown."""
    clock = _clock()
    monkeypatch.setattr(component_api, "_monotonic", lambda: clock[0])
    ws = _make_ws(
        info_exc=HomeAssistantCommandError("Command failed: x", "unknown_command")
        if absence == "unknown_command"
        else None,
        info_result={**_INFO_OK, "schema_version": SUPPORTED_SCHEMA_VERSION + 1},
    )
    client = _client()
    with _patch_ws(ws):
        assert await get_component_caps(client) is None
        clock[0] += 31  # Past the transient cooldown; definitive absence stays cached.
        assert await get_component_caps(client, strict=True) is None
        clock[0] += 268  # At 299 seconds, still inside the five-minute window.
        assert await get_component_caps(client) is None
    assert ws.send_command.await_count == 1


@pytest.mark.asyncio
async def test_expired_negative_reprobes_and_adopts(monkeypatch) -> None:
    """Past the TTL the negative expires: the next call re-probes and can adopt."""
    clock = _clock()
    monkeypatch.setattr(component_api, "_monotonic", lambda: clock[0])

    ws = AsyncMock()
    probe_count = _clock(0.0)

    async def _send(command_type: str, **kwargs: Any) -> dict[str, Any]:
        assert command_type == "ha_mcp_tools/info"
        probe_count[0] += 1
        if probe_count[0] == 1:
            raise HomeAssistantCommandError("Command failed: x", "unknown_command")
        return {"success": True, "result": _INFO_OK}

    ws.send_command = AsyncMock(side_effect=_send)
    client = _client()
    with _patch_ws(ws):
        assert await get_component_caps(client) is None  # negative cached at t0
        clock[0] += component_api._NEGATIVE_CACHE_TTL_S + 1  # expire it
        caps = await get_component_caps(client)

    assert isinstance(caps, ComponentCaps)  # mid-session install adopted
    assert ws.send_command.await_count == 2


@pytest.mark.asyncio
async def test_positive_unaffected_by_ttl(monkeypatch) -> None:
    """A positive entry never expires on the negative TTL timer."""
    clock = _clock()
    monkeypatch.setattr(component_api, "_monotonic", lambda: clock[0])
    ws = _make_ws(info_result=_INFO_OK)
    client = _client()
    with _patch_ws(ws):
        first = await get_component_caps(client)
        clock[0] += component_api._NEGATIVE_CACHE_TTL_S * 10  # far past any TTL
        second = await get_component_caps(client)

    assert isinstance(first, ComponentCaps)
    assert first is second
    assert ws.send_command.await_count == 1


def _fake_pool(monkeypatch, pool: dict[str, Any]) -> None:
    """The pool holds ``pool[token]`` for ``_client()``'s URL with that token.

    Nothing connects. Other URLs or TLS modes find no connection, so a lookup
    with the wrong credentials reads as an empty slot and the re-probe tests
    fail.
    """

    def pooled_client(**credentials: Any) -> Any:
        if credentials["url"] != "http://ha.local:8123":
            return None
        if credentials["verify_ssl"] is not None:
            return None
        return pool.get(credentials["token"])

    monkeypatch.setattr(component_api.websocket_manager, "pooled_client", pooled_client)


@pytest.mark.asyncio
async def test_positive_reprobes_once_the_pool_reconnects(monkeypatch) -> None:
    """A Core restart replaces the pooled WS connection; caps probed over the old
    one are re-probed, so a component upgraded across that restart is adopted
    without restarting the server (#2967)."""
    old_ws = _make_ws(info_result=_INFO_OK)
    old_ws.is_connected = True
    upgraded = {**_INFO_OK, "capabilities": ["search", "automation_config"]}
    new_ws = _make_ws(info_result=upgraded)
    new_ws.is_connected = True
    pool = {"tok": old_ws}
    _fake_pool(monkeypatch, pool)
    client = _client()
    with _patch_ws(old_ws):
        first = await get_component_caps(client)
    pool["tok"] = new_ws
    with _patch_ws(new_ws):
        second = await get_component_caps(client)
        third = await get_component_caps(client)

    assert not component_supports(first, "automation_config")
    assert component_supports(second, "automation_config")
    assert third is second
    assert new_ws.send_command.await_count == 1


@pytest.mark.asyncio
async def test_positive_reprobes_when_its_connection_dropped(monkeypatch) -> None:
    """The connection the caps came from closed (Core restarting) and the pool
    has not replaced it yet; the next call re-probes rather than trusting caps
    from the component that went away."""
    ws = _make_ws(info_result=_INFO_OK)
    ws.is_connected = True
    _fake_pool(monkeypatch, {"tok": ws})
    client = _client()
    with _patch_ws(ws):
        await get_component_caps(client)
        ws.is_connected = False
        await get_component_caps(client)

    assert ws.send_command.await_count == 2


@pytest.mark.asyncio
async def test_positive_kept_while_its_connection_is_pooled(monkeypatch) -> None:
    ws = _make_ws(info_result=_INFO_OK)
    ws.is_connected = True
    _fake_pool(monkeypatch, {"tok": ws})
    client = _client()
    with _patch_ws(ws):
        first = await get_component_caps(client)
        second = await get_component_caps(client)

    assert first is second
    assert ws.send_command.await_count == 1


@pytest.mark.asyncio
async def test_a_failed_reprobe_keeps_the_working_caps(monkeypatch) -> None:
    """Right after a Core restart the re-probe can run before Core answers; the
    caps that worked stay in place instead of a cached failure refusing strict
    callers once Core is back."""
    old_ws = _make_ws(info_result=_INFO_OK)
    old_ws.is_connected = True
    pool = {"tok": old_ws}
    _fake_pool(monkeypatch, pool)
    client = _client()
    with _patch_ws(old_ws):
        working = await get_component_caps(client)

    old_ws.is_connected = False
    down = AsyncMock(side_effect=HomeAssistantConnectionError("Core restarting"))
    with patch.object(component_api, "get_websocket_client", down):
        during = await get_component_caps(client, strict=True)
    # The failed connect took the dead connection out of the pool; nothing
    # has opened a new one yet when the next caller asks.
    del pool["tok"]

    upgraded = {**_INFO_OK, "capabilities": ["search", "automation_config"]}
    back_ws = _make_ws(info_result=upgraded)
    back_ws.is_connected = True
    with _patch_ws(back_ws):
        after = await get_component_caps(client, strict=True)

    assert during is working
    assert component_supports(after, "automation_config")


@pytest.mark.asyncio
async def test_a_component_still_starting_is_probed_again_soon(monkeypatch) -> None:
    """After a Core restart Core answers before the component has registered
    its commands; a re-probe of working caps that meets ``unknown_command``
    there gets the short window, so the started component is found within it
    instead of after the five-minute absence window."""
    clock = _clock()
    monkeypatch.setattr(component_api, "_monotonic", lambda: clock[0])
    old_ws = _make_ws(info_result=_INFO_OK)
    old_ws.is_connected = True
    pool = {"tok": old_ws}
    _fake_pool(monkeypatch, pool)
    client = _client()
    with _patch_ws(old_ws):
        await get_component_caps(client)

    starting = _make_ws(
        info_exc=HomeAssistantCommandError("Unknown command.", "unknown_command")
    )
    starting.is_connected = True
    pool["tok"] = starting
    with _patch_ws(starting):
        during = await get_component_caps(client)

    clock[0] += component_api._TRANSIENT_NEGATIVE_CACHE_TTL_S + 1
    started = _make_ws(info_result=_INFO_OK)
    started.is_connected = True
    pool["tok"] = started
    with _patch_ws(started):
        after = await get_component_caps(client)

    assert during is None
    assert isinstance(after, ComponentCaps)


@pytest.mark.asyncio
async def test_a_command_refused_while_starting_also_gets_the_short_window(
    monkeypatch,
) -> None:
    """A component command sent in the same restart gap answers
    ``unknown_command`` and drops the caps; the probe that follows, still in
    the gap, gets the short window too rather than five minutes."""
    clock = _clock()
    monkeypatch.setattr(component_api, "_monotonic", lambda: clock[0])
    client = _client()
    with _patch_ws(_make_ws(info_result=_INFO_OK)):
        await get_component_caps(client)
    invalidate_caps(client)

    starting = _make_ws(
        info_exc=HomeAssistantCommandError("Unknown command.", "unknown_command")
    )
    with _patch_ws(starting):
        assert await get_component_caps(client) is None

    clock[0] += component_api._TRANSIENT_NEGATIVE_CACHE_TTL_S + 1
    with _patch_ws(_make_ws(info_result=_INFO_OK)):
        assert isinstance(await get_component_caps(client), ComponentCaps)


@pytest.mark.asyncio
async def test_a_failed_reprobe_never_hands_out_a_malformed_snapshot(
    monkeypatch,
) -> None:
    """Caps parsed from a malformed ``info`` reply are not caps that worked: a
    failed re-probe must not pass them to a strict caller."""
    clock = _clock()
    monkeypatch.setattr(component_api, "_monotonic", lambda: clock[0])
    malformed = _make_ws(info_result={**_INFO_OK, "schema_version": "1"})
    client = _client()
    with _patch_ws(malformed):
        await get_component_caps(client)

    clock[0] += component_api._TRANSIENT_NEGATIVE_CACHE_TTL_S + 1
    down = AsyncMock(side_effect=HomeAssistantConnectionError("down"))
    with (
        patch.object(component_api, "get_websocket_client", down),
        pytest.raises(component_api.ComponentDiscoveryError),
    ):
        await get_component_caps(client, strict=True)


class _OAuthProxy:
    """The OAuth proxy's shape: one object naming each request's own client."""

    def __init__(self, users: dict[str, _FakeClient]) -> None:
        self.users = users
        self.user = ""

    def current_client(self) -> _FakeClient:
        return self.users[self.user]

    def __getattr__(self, name: str) -> Any:
        return getattr(self.current_client(), name)


def _oauth_users(monkeypatch, infos: dict[str, Any]) -> tuple[_OAuthProxy, dict]:
    """A proxy over one client per token, each with its own pooled connection."""
    pool = {token: _make_ws(**info) for token, info in infos.items()}
    for ws in pool.values():
        ws.is_connected = True
    _fake_pool(monkeypatch, pool)

    async def connect(*, url: str, token: str, verify_ssl: Any) -> Any:
        return pool[token]

    monkeypatch.setattr(component_api, "get_websocket_client", connect)
    return _OAuthProxy({token: _FakeClient(token=token) for token in infos}), pool


@pytest.mark.asyncio
async def test_alternating_oauth_users_each_probe_once(monkeypatch) -> None:
    """The OAuth proxy is one object whose token changes per request; each
    user's caps are their own, so switching users does not re-probe."""
    ok = {"info_result": _INFO_OK}
    proxy, pool = _oauth_users(monkeypatch, {"alice": ok, "bob": ok})

    for user in ("alice", "bob", "alice", "bob", "alice"):
        proxy.user = user
        assert isinstance(await get_component_caps(proxy), ComponentCaps)

    assert sum(ws.send_command.await_count for ws in pool.values()) == 2


@pytest.mark.asyncio
async def test_a_non_admins_refused_probe_leaves_the_admins_caps(monkeypatch) -> None:
    """``info`` is admin-only: a non-admin OAuth user's refused probe must not
    replace an administrator's caps, or the administrator's strict reads fail
    for the failure window."""
    refused = HomeAssistantCommandError("Unauthorized", "unauthorized")
    proxy, _pool = _oauth_users(
        monkeypatch,
        {"admin": {"info_result": _INFO_OK}, "viewer": {"info_exc": refused}},
    )

    proxy.user = "admin"
    admin_caps = await get_component_caps(proxy, strict=True)
    proxy.user = "viewer"
    with pytest.raises(component_api.ComponentDiscoveryError):
        await get_component_caps(proxy, strict=True)
    proxy.user = "admin"

    assert await get_component_caps(proxy, strict=True) is admin_caps
    assert not component_api.component_probe_auth_failed(proxy)


def test_component_supports() -> None:
    caps = ComponentCaps(
        schema_version=1,
        component_version="1.1.0",
        capabilities=frozenset({"search"}),
        limits={},
    )
    assert component_supports(caps, "search") is True
    assert component_supports(caps, "overview") is False
    assert component_supports(None, "search") is False


def test_is_unknown_command_keys_off_code_not_message() -> None:
    # Structured code drives the decision.
    assert is_unknown_command(
        HomeAssistantCommandError("Command failed: nope", "unknown_command")
    )
    # A message that merely mentions "unknown command" but carries no code does
    # NOT match — routing must not key off the human-readable text.
    assert not is_unknown_command(
        HomeAssistantCommandError("Command failed: unknown command foo")
    )
    # Some other structured code does not match either.
    assert not is_unknown_command(
        HomeAssistantCommandError("Command failed: bad", "invalid_format")
    )
    assert not is_unknown_command(ValueError("boom"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    ["connection", "timeout", "command", "unexpected", "malformed", "malformed_caps"],
)
@pytest.mark.parametrize("strict_first", [False, True])
async def test_strict_discovery_recovers_after_cached_failure_expires(
    monkeypatch, failure, strict_first
):
    """Failed probes hold for 30 seconds, preserving reads, then allow recovery."""
    clock = _clock()
    monkeypatch.setattr(component_api, "_monotonic", lambda: clock[0])
    client = _client()
    info_exc = {
        "connection": HomeAssistantConnectionError("down"),
        "timeout": HomeAssistantCommandTimeout("slow info"),
        "command": HomeAssistantCommandError("broken info", "unknown_error"),
        "unexpected": RuntimeError("broken probe"),
    }.get(failure)
    ws = _make_ws(
        info_exc=info_exc,
        info_result={**_INFO_OK, "capabilities": ["search", 17]}
        if failure == "malformed_caps"
        else None,
    )
    with _patch_ws(ws):
        if strict_first:
            with pytest.raises(component_api.ComponentDiscoveryError) as fresh:
                await get_component_caps(client, strict=True)
            # The strict caller whose own probe failed gets that exception.
            assert fresh.value.__cause__ is info_exc
        first_read = await get_component_caps(client)
        assert component_supports(first_read, "search") is (failure == "malformed_caps")
        with pytest.raises(component_api.ComponentDiscoveryError) as cached:
            await get_component_caps(client, strict=True)
        # A failure read back from the cache carries only its text.
        assert cached.value.__cause__ is None
        assert ws.send_command.await_count == 1
        # Discovery has recovered, but callers still honor the short cooldown.
        ws.send_command.side_effect = None
        ws.send_command.return_value = {"success": True, "result": _INFO_OK}
        clock[0] += 29
        assert await get_component_caps(client) is first_read
        with pytest.raises(component_api.ComponentDiscoveryError):
            await get_component_caps(client, strict=True)
        assert ws.send_command.await_count == 1
        # Pin the recovery time, not whichever TTL the implementation uses.
        clock[0] += 1
        assert component_supports(
            await get_component_caps(client, strict=True), "search"
        )
        assert component_supports(
            await get_component_caps(client, strict=True), "search"
        )
        assert ws.send_command.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "absence", ["unknown_command", "old_capabilities", "unsupported_schema"]
)
async def test_strict_discovery_accepts_definitive_capability_absence(absence):
    """Confirmed incompatible or absent capability remains a valid fallback."""
    ws = _make_ws(
        info_exc=HomeAssistantCommandError("absent", "unknown_command")
        if absence == "unknown_command"
        else None,
        info_result={
            **_INFO_OK,
            "schema_version": 2 if absence == "unsupported_schema" else 1,
        },
    )
    client = _client()
    with _patch_ws(ws):
        for _ in range(2):
            assert not component_supports(
                await get_component_caps(client, strict=True), "dashboard_edit"
            )
    assert ws.send_command.await_count == 1


def test_auth_rejection_kind_distinguishes_permission_and_token():
    assert (
        component_api._auth_rejection_kind(
            HomeAssistantCommandError("Command failed: Unauthorized", "unauthorized")
        )
        == "permission"
    )
    assert (
        component_api._auth_rejection_kind(HomeAssistantAdminRequiredError("x"))
        == "permission"
    )
    assert (
        component_api._auth_rejection_kind(HomeAssistantAuthError("invalid auth"))
        == "token"
    )
    assert component_api._auth_rejection_kind(RuntimeError("boom")) is None


@pytest.mark.asyncio
async def test_probe_auth_marker_records_kind_and_clears_on_success():
    ws = _make_ws(
        info_exc=HomeAssistantCommandError(
            "Command failed: Unauthorized", "unauthorized"
        )
    )
    client = _client()
    with _patch_ws(ws):
        assert await get_component_caps(client) is None

    assert component_api.component_probe_auth_failed(client) is True
    assert component_api.component_probe_auth_kind(client) == "permission"

    invalidate_caps(client)
    with _patch_ws(_make_ws(info_result=_INFO_OK)):
        assert await get_component_caps(client) is not None

    assert component_api.component_probe_auth_failed(client) is False
