"""Regression coverage for #1544 (HTTP entrypoint silently exits 0).

``_run_with_shutdown`` must re-raise the exception of a server task that
finishes on its own, so *any* hard startup failure becomes a logged
``sys.exit(1)`` instead of a silent exit 0. ``_http_run_kwargs`` is covered
alongside it, since the kwargs it builds are what that server task starts from.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from ha_mcp.__main__ import _http_run_kwargs, _run_with_shutdown


@pytest.fixture(autouse=True)
def _no_startup_hacs_nudge():
    """Keep the startup HACS nudge out of these tests.

    ``_run_with_shutdown`` fires it as a background task; left real it would
    reach PyPI and the Home Assistant WebSocket from a unit test.
    """
    with patch(
        "ha_mcp.hacs_auto_refresh.maybe_refresh_hacs_after_update",
        new=AsyncMock(return_value=None),
    ):
        yield


def test_kwargs_request_stateless_streamable_http():
    """Every HTTP entrypoint runs Streamable HTTP in stateless mode."""
    kw = _http_run_kwargs("127.0.0.1", 8086, "/mcp")
    assert kw["transport"] == "http"
    assert kw["stateless_http"] is True


def test_kwargs_carry_bind_target_and_log_config():
    """The bind target and the timestamped uvicorn log config are passed through."""
    kw = _http_run_kwargs("127.0.0.1", 8086, "/p")
    assert {"transport", "host", "port", "path", "show_banner", "uvicorn_config"} <= (
        kw.keys()
    )
    assert kw["host"] == "127.0.0.1"
    assert kw["port"] == 8086
    assert kw["path"] == "/p"


async def test_run_with_shutdown_surfaces_server_exception():
    """Regression #1544: a self-terminating server task must not exit 0.

    When the server task finishes on its own (no shutdown signal),
    _run_with_shutdown re-raises its exception so _run_entrypoint logs it
    and exits 1 — instead of swallowing it into a silent exit 0.
    """

    async def failing_server():
        raise ValueError("port already in use")

    with pytest.raises(ValueError, match="port already in use"):
        await _run_with_shutdown(failing_server())


async def test_run_with_shutdown_returns_when_server_finishes_cleanly():
    """A server task that returns normally (no shutdown signal) must not raise.

    Exercises the same new ``elif server_task in done`` branch as the exception
    test, but for the clean-return case: ``server_task.result()`` returns
    harmlessly and _run_with_shutdown completes without error.
    """

    async def clean_server():
        return None

    await _run_with_shutdown(clean_server())  # must not raise


async def test_run_with_shutdown_cleans_up_when_server_fails(monkeypatch):
    """Resources are still cleaned up when a self-terminating server fails.

    The failure surfaces (covered above), but the finally block must still run
    _cleanup_resources so a crash on startup doesn't leak resources.
    """
    cleaned = False

    async def fake_cleanup():
        nonlocal cleaned
        cleaned = True

    monkeypatch.setattr("ha_mcp.__main__._cleanup_resources", fake_cleanup)

    async def failing_server():
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await _run_with_shutdown(failing_server())
    assert cleaned, "cleanup must run even when the server task fails"


async def test_run_with_shutdown_surfaces_unexpected_cancellation():
    """Regression #1544: a server task cancelled with no shutdown signal is a
    hard stop, not a graceful one — it must propagate rather than exit 0.

    Without the _shutdown_event.is_set() gate, the re-raised CancelledError is
    caught and logged as a benign "Server task cancelled", silently exiting 0.
    """

    async def self_cancelling_server():
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _run_with_shutdown(self_cancelling_server())
