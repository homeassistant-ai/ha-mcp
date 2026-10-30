"""Probe which address families a real uvicorn listener accepts on (issue #2915)."""

from __future__ import annotations

import asyncio
import socket
from contextlib import suppress
from typing import Any

import pytest
import uvicorn


def require_ipv6_loopback() -> None:
    """Skip when this machine cannot bind ``::1`` (IPv6 disabled)."""
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
            sock.bind(("::1", 0))
    except OSError:
        pytest.skip("IPv6 loopback unavailable on this machine")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    if scope["type"] == "lifespan":
        return
    await send({"type": "http.response.start", "status": 204, "headers": []})
    await send({"type": "http.response.body", "body": b""})


async def _connects(address: str, port: int) -> bool:
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(address, port), timeout=2
        )
    except (OSError, TimeoutError):
        return False
    writer.close()
    with suppress(OSError):
        await writer.wait_closed()
    return True


async def reachable_loopbacks(host: str) -> set[str]:
    """Serve on ``host`` and return the loopback addresses that connect."""
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(_app, host=host, port=port, lifespan="off", log_config=None)
    )
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started or task.done():
                break
            await asyncio.sleep(0.05)
        assert server.started, f"uvicorn never started on host={host!r}"
        return {
            address
            for address in ("127.0.0.1", "::1")
            if await _connects(address, port)
        }
    finally:
        server.should_exit = True
        await task
