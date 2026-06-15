"""Regression coverage for #1544 (SSE entrypoint silently exits 0).

Two independent guards:

1. ``_http_run_kwargs`` must not pass ``stateless_http=True`` for ``transport="sse"``.
   On the pinned fastmcp, that combination raises
   ``ValueError("SSE transport does not support stateless mode")``, which ha-mcp
   swallowed — leaving the SSE entrypoint to exit 0 without binding.
2. ``_run_with_shutdown`` must re-raise the exception of a server task that
   finishes on its own, so *any* hard startup failure becomes a logged
   ``sys.exit(1)`` instead of a silent exit 0.
"""

import asyncio

import pytest

from ha_mcp.__main__ import _http_run_kwargs, _run_with_shutdown


def test_http_transport_includes_stateless_http():
    kw = _http_run_kwargs("http", "127.0.0.1", 8086, "/mcp")
    assert kw["stateless_http"] is True


def test_streamable_http_transport_includes_stateless_http():
    kw = _http_run_kwargs("streamable-http", "127.0.0.1", 8086, "/mcp")
    assert kw["stateless_http"] is True


def test_sse_transport_omits_stateless_http():
    """Regression #1544: stateless_http must be absent for SSE.

    fastmcp's run_async raises ValueError for ``stateless_http=True`` +
    ``transport="sse"``; gating it out of the SSE kwargs is what lets the
    SSE entrypoint bind instead of silently exiting 0.
    """
    kw = _http_run_kwargs("sse", "127.0.0.1", 8087, "/sse")
    assert "stateless_http" not in kw


def test_common_kwargs_present_across_transports():
    """Non-stateless kwargs are identical regardless of transport."""
    common_keys = {"transport", "host", "port", "path", "show_banner", "uvicorn_config"}
    for transport in ("http", "sse", "streamable-http"):
        kw = _http_run_kwargs(transport, "127.0.0.1", 8086, "/p")
        assert common_keys.issubset(kw.keys()), (
            f"missing keys for transport={transport}"
        )
        assert kw["transport"] == transport
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
        raise ValueError("SSE transport does not support stateless mode")

    with pytest.raises(ValueError, match="does not support stateless mode"):
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
