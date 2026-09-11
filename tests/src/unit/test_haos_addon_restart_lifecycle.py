"""Disruptive HAOS cleanup must prove process replacement using fresh clients.

All HTTP/MCP peers are fake: a restart acknowledgment deliberately leaves the
old process healthy until a clock-controlled disconnect and replacement.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from tests.src.e2e.haos_only import (
    test_addon_debug_log_level as debug_level,
)
from tests.src.e2e.haos_only import (
    test_inaddon_startup_nudge as startup_nudge,
)
from tests.src.e2e.utilities import addon_restart

_BASE = "http://addon.test/api/settings"
_ADVANCED = f"{_BASE}/advanced"
_RESTART = f"{_BASE}/restart"
_INFO = f"{_BASE}/info"
_MCP = "http://addon.test/mcp"


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        assert seconds >= 0
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    clock = _Clock()
    monkeypatch.setattr(addon_restart, "time", clock)
    monkeypatch.setattr(
        addon_restart,
        "asyncio",
        SimpleNamespace(timeout=asyncio.timeout, sleep=clock.sleep),
    )
    return clock


class _Addon:
    """A scheduled restart, including a healthy old process and brief outage."""

    def __init__(self, clock: _Clock) -> None:
        self.clock = clock
        self.disconnect_at: float | None = 3.0
        self.replace_at: float | None = 6.0
        self.mcp_failures = 0
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.reads: list[tuple[float, str]] = []
        self.mcp_calls: list[tuple[float, str, str]] = []
        self.clients: list[Any] = []
        self.request_errors: dict[str, list[Exception]] = {}
        self.response_bodies: dict[str, list[str]] = {}

    def raise_request_error(self, url: str) -> None:
        errors = self.request_errors.get(url)
        if errors:
            raise errors.pop(0)

    def instance(self) -> str:
        if self.replace_at is not None and self.clock.now >= self.replace_at:
            return "replacement-process"
        if self.disconnect_at is not None and self.clock.now >= self.disconnect_at:
            raise httpx.ConnectError("old process closed its listener")
        return "old-process"

    def http(self, **kwargs: Any) -> Any:
        owner = self

        class HTTP:
            async def __aenter__(self) -> Any:
                return self

            async def __aexit__(self, *args: Any) -> None:
                pass

            async def get(self, url: str) -> httpx.Response:
                owner.raise_request_error(url)
                instance = owner.instance()
                owner.reads.append((owner.clock.now, instance))
                if bodies := owner.response_bodies.get(url):
                    return httpx.Response(
                        200, content=bodies.pop(0), request=httpx.Request("GET", url)
                    )
                return httpx.Response(
                    200,
                    json={"instance_id": instance},
                    request=httpx.Request("GET", url),
                )

            async def post(self, url: str, *, json: dict[str, Any]) -> httpx.Response:
                owner.posts.append((url, json))
                owner.raise_request_error(url)
                if bodies := owner.response_bodies.get(url):
                    return httpx.Response(
                        200, content=bodies.pop(0), request=httpx.Request("POST", url)
                    )
                return httpx.Response(
                    200,
                    json={"restart_required": True},
                    request=httpx.Request("POST", url),
                )

        return HTTP()

    def mcp(self, *args: Any, **kwargs: Any) -> Any:
        owner = self

        class MCP:
            async def __aenter__(self) -> Any:
                return self

            async def __aexit__(self, *args: Any) -> None:
                pass

            async def call_tool(self, tool: str, args: dict[str, Any]) -> Any:
                owner.mcp_calls.append((owner.clock.now, owner.instance(), tool))
                if owner.mcp_failures:
                    owner.mcp_failures -= 1
                    raise httpx.ConnectError("replacement MCP transport is warming")
                return {"success": True}

        client = MCP()
        self.clients.append(client)
        return client


@pytest.fixture
def addon(monkeypatch: pytest.MonkeyPatch, clock: _Clock) -> _Addon:
    addon = _Addon(clock)
    monkeypatch.setattr(addon_restart.httpx, "AsyncClient", addon.http)
    monkeypatch.setattr(addon_restart, "Client", addon.mcp)
    return addon


@pytest.mark.parametrize(
    "test_function",
    [
        debug_level.test_web_ui_debug_log_level_reaches_addon_log,
        startup_nudge.test_addon_launcher_schedules_the_startup_nudge,
    ],
)
def test_disruptive_tests_do_not_request_the_shared_mcp_session(test_function):
    """A test that kills its server must not initialize the session client."""
    assert "mcp_client" not in inspect.signature(test_function).parameters


async def test_restore_waits_for_old_process_exit_and_fresh_mcp_readiness(addon):
    addon.mcp_failures = 1

    await addon_restart.restore_info_level(
        _ADVANCED,
        _RESTART,
        _INFO,
        _MCP,
        restore_timeout=10.0,
        ready_timeout=15.0,
        poll_interval=3.0,
    )

    assert addon.posts == [(_ADVANCED, {"log_level": "INFO"}), (_RESTART, {})]
    # One pre-submission baseline plus the old process's healthy response
    # after POST 200; neither may satisfy the replacement/readiness gate.
    assert addon.reads[:2] == [(0.0, "old-process"), (0.0, "old-process")]
    assert addon.clock.now == pytest.approx(9.0)
    assert [call[:2] for call in addon.mcp_calls] == [
        (6.0, "replacement-process"),
        (9.0, "replacement-process"),
    ]
    assert len(addon.clients) == 2
    assert addon.clients[0] is not addon.clients[1]


async def test_accepted_restart_is_not_replayed_when_old_process_stays_healthy(addon):
    addon.disconnect_at = addon.replace_at = None

    with pytest.raises(AssertionError):
        await addon_restart.restore_info_level(
            _ADVANCED,
            _RESTART,
            _INFO,
            _MCP,
            restore_timeout=10.0,
            ready_timeout=10.0,
            poll_interval=3.0,
        )

    assert addon.posts == [(_ADVANCED, {"log_level": "INFO"}), (_RESTART, {})]
    assert addon.clock.now == pytest.approx(10.0)
    assert addon.mcp_calls == []


async def test_changed_instance_retries_transient_mcp_using_a_new_client(addon):
    addon.replace_at = 0.0
    addon.mcp_failures = 1

    result = await addon_restart.wait_for_addon_replacement(
        _INFO, _MCP, "old-process", timeout=10.0, poll_interval=3.0
    )

    assert result == "replacement-process"
    assert addon.clock.now == pytest.approx(3.0)
    assert len(addon.clients) == 2
    assert addon.posts == []


@pytest.mark.parametrize("error_type", [httpx.ReadError, TimeoutError])
async def test_lost_restart_response_waits_for_replacement_without_replay(
    addon, error_type, caplog
):
    """An accepted restart may lose its response before the old listener exits."""
    addon.disconnect_at = None
    addon.request_errors[_RESTART] = [error_type("response lost")] * 5
    completion_error = None

    try:
        await addon_restart.restore_info_level(
            _ADVANCED,
            _RESTART,
            _INFO,
            _MCP,
            restore_timeout=10.0,
            ready_timeout=10.0,
            poll_interval=3.0,
        )
    except AssertionError as error:
        completion_error = error

    assert [url for url, _ in addon.posts].count(_RESTART) == 1
    assert completion_error is None, completion_error
    assert addon.clock.now == pytest.approx(6.0)
    assert addon.reads[:2] == [(0.0, "old-process"), (0.0, "old-process")]
    assert addon.mcp_calls[0][1] == "replacement-process"
    assert any(
        record.levelno == logging.WARNING and "response lost" in record.message
        for record in caplog.records
    )


@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadError("response lost"),
        TimeoutError("response lost"),
        httpx.HTTPStatusError(
            "500 Internal Server Error",
            request=httpx.Request("POST", _RESTART),
            response=httpx.Response(500),
        ),
    ],
    ids=["read-error", "timeout", "http-500"],
)
async def test_uncertain_restart_without_replacement_times_out_without_replay(
    addon, error, caplog
):
    addon.disconnect_at = addon.replace_at = None
    addon.request_errors[_RESTART] = [error] * 5

    with pytest.raises(AssertionError) as failure:
        await addon_restart.restore_info_level(
            _ADVANCED,
            _RESTART,
            _INFO,
            _MCP,
            restore_timeout=10.0,
            ready_timeout=10.0,
            poll_interval=3.0,
        )

    assert [url for url, _ in addon.posts].count(_RESTART) == 1
    assert addon.clock.now == pytest.approx(10.0)
    assert addon.mcp_calls == []
    assert repr(error) in str(failure.value)
    assert "still reached pre-restart process old-process" in str(failure.value)
    assert any(
        record.levelno == logging.WARNING and str(error) in record.message
        for record in caplog.records
    )


@pytest.mark.parametrize("endpoint", [_INFO, _ADVANCED])
async def test_truncated_json_before_restart_submission_is_retried(addon, endpoint):
    addon.disconnect_at = None
    addon.replace_at = 9.0
    addon.response_bodies[endpoint] = ['{"truncated":']

    await addon_restart.restore_info_level(
        _ADVANCED,
        _RESTART,
        _INFO,
        _MCP,
        restore_timeout=10.0,
        ready_timeout=10.0,
        poll_interval=3.0,
    )

    assert not addon.response_bodies[endpoint]
    assert [url for url, _ in addon.posts].count(_RESTART) == 1
    assert addon.clock.now == pytest.approx(9.0)


async def test_truncated_json_during_replacement_readiness_is_retried(addon):
    addon.replace_at = 0.0
    addon.response_bodies[_INFO] = ['{"instance_id":']

    result = await addon_restart.wait_for_addon_replacement(
        _INFO, _MCP, "old-process", timeout=10.0, poll_interval=3.0
    )

    assert result == "replacement-process"
    assert addon.clock.now == pytest.approx(3.0)
    assert len(addon.clients) == 1
    assert addon.posts == []


@pytest.mark.parametrize("endpoint", [_INFO, _ADVANCED])
async def test_transient_before_restart_submission_is_retried(addon, endpoint):
    addon.disconnect_at = None
    addon.replace_at = 9.0
    addon.request_errors[endpoint] = [httpx.ReadError("temporarily unavailable")]

    await addon_restart.restore_info_level(
        _ADVANCED,
        _RESTART,
        _INFO,
        _MCP,
        restore_timeout=10.0,
        ready_timeout=10.0,
        poll_interval=3.0,
    )

    assert not addon.request_errors[endpoint]
    assert [url for url, _ in addon.posts].count(_RESTART) == 1
    expected_advanced_posts = 1 if endpoint == _INFO else 2
    assert [url for url, _ in addon.posts].count(_ADVANCED) == expected_advanced_posts
    assert addon.clock.now == pytest.approx(9.0)
    assert addon.mcp_calls[0][1] == "replacement-process"


@pytest.mark.parametrize("error_type", [TypeError, ValueError, AssertionError])
@pytest.mark.parametrize("endpoint", [_INFO, _ADVANCED, _RESTART])
async def test_submission_contract_errors_propagate_without_retry(
    addon, error_type, endpoint
):
    addon.request_errors[endpoint] = [error_type("broken contract")]

    with pytest.raises(error_type, match="broken contract"):
        await addon_restart.restore_info_level(_ADVANCED, _RESTART, _INFO, _MCP)

    assert addon.clock.now == 0
    expected_restart_posts = int(endpoint == _RESTART)
    assert [url for url, _ in addon.posts].count(_RESTART) == expected_restart_posts


async def test_readiness_passes_each_exchange_its_remaining_deadline(
    monkeypatch, clock
):
    calls: list[tuple[str, float, float]] = []

    async def get_instance(url, *, timeout):
        calls.append(("info", clock.now, timeout))
        clock.now += 1.0
        return "replacement-process"

    async def call_tool(url, tool, args, *, timeout):
        calls.append(("mcp", clock.now, timeout))
        clock.now += 1.0
        raise httpx.ConnectError("still warming")

    monkeypatch.setattr(addon_restart, "get_instance_id", get_instance)
    monkeypatch.setattr(addon_restart, "call_tool_fresh", call_tool)

    with pytest.raises(AssertionError):
        await addon_restart.wait_for_addon_replacement(
            _INFO, _MCP, "old-process", timeout=10.0, poll_interval=3.0
        )

    assert calls == [
        ("info", 0.0, 10.0),
        ("mcp", 1.0, 9.0),
        ("info", 5.0, 5.0),
        ("mcp", 6.0, 4.0),
    ]
    assert clock.now == pytest.approx(10.0)


async def test_restore_submission_passes_each_http_exchange_its_remaining_budget(
    monkeypatch, clock
):
    calls: list[tuple[str, float, float]] = []

    async def get_instance(url, *, timeout):
        calls.append(("info", clock.now, timeout))
        clock.now += 1.0
        return "old-process"

    async def post_level(url, level, *, timeout):
        assert level == "INFO"
        calls.append(("advanced", clock.now, timeout))
        clock.now += 2.0

    async def restart(url, *, timeout):
        calls.append(("restart", clock.now, timeout))
        clock.now += 1.0

    async def replacement(
        info, url, baseline, *, timeout, poll_interval, submission_error
    ):
        assert (info, url, baseline) == (_INFO, _MCP, "old-process")
        assert timeout == 11.0
        assert submission_error is None
        return "replacement-process"

    monkeypatch.setattr(addon_restart, "get_instance_id", get_instance)
    monkeypatch.setattr(addon_restart, "post_log_level", post_level)
    monkeypatch.setattr(addon_restart, "restart_self", restart)
    monkeypatch.setattr(addon_restart, "wait_for_addon_replacement", replacement)

    await addon_restart.restore_info_level(
        _ADVANCED, _RESTART, _INFO, _MCP, restore_timeout=10.0, ready_timeout=11.0
    )

    assert calls == [
        ("info", 0.0, 10.0),
        ("advanced", 1.0, 9.0),
        ("restart", 3.0, 7.0),
    ]


@pytest.mark.parametrize(
    "error",
    [TypeError("bug"), ValueError("invalid value"), AssertionError("invalid shape")],
)
@pytest.mark.parametrize("stage", ["info", "mcp"])
async def test_readiness_does_not_retry_programming_or_response_contract_errors(
    monkeypatch, clock, error, stage
):
    async def get_instance(url, *, timeout):
        if stage == "info":
            raise error
        return "replacement-process"

    async def call_tool(url, tool, args, *, timeout):
        raise error

    monkeypatch.setattr(addon_restart, "get_instance_id", get_instance)
    monkeypatch.setattr(addon_restart, "call_tool_fresh", call_tool)

    with pytest.raises(type(error), match=str(error)):
        await addon_restart.wait_for_addon_replacement(
            _INFO, _MCP, "old-process", timeout=10.0
        )
    assert clock.now == 0


class _StallingPeer:
    def __init__(self, stage: str, cancelled: asyncio.Event):
        self.stage = stage
        self.cancelled = cancelled

    async def stall(self):
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled.set()

    async def __aenter__(self):
        if self.stage == "enter":
            await self.stall()
        return self

    async def __aexit__(self, *args):
        if self.stage == "exit":
            await self.stall()

    async def get(self, url):
        if self.stage == "request":
            await self.stall()
        return httpx.Response(
            200,
            json={"instance_id": "replacement-process"},
            request=httpx.Request("GET", url),
        )

    async def post(self, url, *, json):
        if self.stage == "request":
            await self.stall()
        return httpx.Response(
            200, json={"restart_required": True}, request=httpx.Request("POST", url)
        )

    async def call_tool(self, tool, args):
        if self.stage == "request":
            await self.stall()
        return {"success": True}


@pytest.mark.parametrize("stage", ["enter", "request", "exit"])
@pytest.mark.parametrize("operation", ["info", "advanced", "restart", "mcp"])
async def test_entire_http_and_mcp_exchange_is_bounded(monkeypatch, stage, operation):
    """A timeout must cover connection setup and teardown as well as the call.

    No sockets are opened. Event waits make stalls deterministic; the outer
    timeout only keeps a missing inner bound from hanging the unit suite.
    """
    cancelled = asyncio.Event()

    def peer(*args, **kwargs):
        return _StallingPeer(stage, cancelled)

    monkeypatch.setattr(addon_restart.httpx, "AsyncClient", peer)
    monkeypatch.setattr(addon_restart, "Client", peer)
    calls = {
        "info": lambda: addon_restart.get_instance_id(_INFO, timeout=0.02),
        "advanced": lambda: addon_restart.post_log_level(
            _ADVANCED, "INFO", timeout=0.02
        ),
        "restart": lambda: addon_restart.restart_self(_RESTART, timeout=0.02),
        "mcp": lambda: addon_restart.call_tool_fresh(
            _MCP, "ha_get_overview", {}, timeout=0.02
        ),
    }
    task = asyncio.create_task(calls[operation]())
    done, _ = await asyncio.wait({task}, timeout=1.0)
    if not done:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        pytest.fail(f"{operation} exchange did not bound its {stage} await")
    with pytest.raises(TimeoutError):
        await asyncio.gather(task)
    assert cancelled.is_set()
