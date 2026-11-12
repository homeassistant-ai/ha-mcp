"""Regression coverage for #1544 (HTTP entrypoint silently exits 0).

The coroutine ``ha-mcp-web`` hands its runner must re-raise the exception of
a server task that finishes on its own, so *any* hard startup failure becomes
a logged ``sys.exit(1)`` instead of a silent exit 0. The arguments the server
is started with are covered alongside it, since that server task starts from
them.
"""

import asyncio
from typing import Any

import pytest

from ._main_web import start_main_web


def _serve_with(monkeypatch: pytest.MonkeyPatch, run_async: Any, **env: str) -> Any:
    """``ha-mcp-web``'s serve coroutine, its server running ``run_async``."""
    start = start_main_web(monkeypatch, keep_serve=True, **env)
    monkeypatch.setattr(start.mcp, "run_async", run_async)
    return start.serve


async def _started_with(monkeypatch: pytest.MonkeyPatch, **env: str) -> dict[str, Any]:
    started: list[dict[str, Any]] = []

    async def run_async(**kwargs: Any) -> None:
        started.append(kwargs)

    await _serve_with(monkeypatch, run_async, **env)
    [kwargs] = started
    return kwargs


async def test_server_runs_stateless_streamable_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every HTTP entrypoint runs Streamable HTTP in stateless mode."""
    kw = await _started_with(monkeypatch)
    assert kw["transport"] == "http"
    assert kw["stateless_http"] is True


async def test_server_gets_bind_target_and_log_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bind target and the timestamped uvicorn log config are passed through."""
    kw = await _started_with(
        monkeypatch, MCP_HOST="127.0.0.1", MCP_PORT="8087", MCP_SECRET_PATH="/p"
    )
    assert {"transport", "host", "port", "path", "show_banner", "uvicorn_config"} <= (
        kw.keys()
    )
    assert kw["host"] == "127.0.0.1"
    assert kw["port"] == 8087
    assert kw["path"] == "/p"


async def test_serve_surfaces_server_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression #1544: a self-terminating server task must not exit 0.

    When the server task finishes on its own (no shutdown signal), the serve
    coroutine re-raises its exception so the runner logs it and exits 1 —
    instead of swallowing it into a silent exit 0.
    """

    async def failing_server(**kwargs: Any) -> None:
        raise ValueError("port already in use")

    with pytest.raises(ValueError, match="port already in use"):
        await _serve_with(monkeypatch, failing_server)


async def test_serve_returns_when_server_finishes_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server task that returns normally (no shutdown signal) must not raise."""

    async def clean_server(**kwargs: Any) -> None:
        return None

    await _serve_with(monkeypatch, clean_server)  # must not raise


async def test_serve_cleans_up_when_server_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resources are still cleaned up when a self-terminating server fails.

    The failure surfaces (covered above), but the cleanup must still run so a
    crash on startup doesn't leak resources.
    """
    cleaned = False

    async def fake_cleanup() -> None:
        nonlocal cleaned
        cleaned = True

    monkeypatch.setattr("ha_mcp.__main__._cleanup_resources", fake_cleanup)

    async def failing_server(**kwargs: Any) -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await _serve_with(monkeypatch, failing_server)
    assert cleaned, "cleanup must run even when the server task fails"


async def test_serve_surfaces_unexpected_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression #1544: a server task cancelled with no shutdown signal is a
    hard stop, not a graceful one — it must propagate rather than exit 0.

    Without the shutdown-event gate, the re-raised CancelledError is caught
    and logged as a benign "Server task cancelled", silently exiting 0.
    """

    async def self_cancelling_server(**kwargs: Any) -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _serve_with(monkeypatch, self_cancelling_server)
