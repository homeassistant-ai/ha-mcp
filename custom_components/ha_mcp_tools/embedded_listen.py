"""The in-process server's direct listener: its HTTP guards and sockets.

Built inside the worker's ha_mcp import window (#1904), so every ha_mcp helper
it needs is imported before that window closes.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from .const import BIND_HOST_ALL

_LOGGER = logging.getLogger(__name__)


class DirectListener:
    """IPv6 guard and dual-stack sockets for the direct port's "Local network"."""

    def __init__(self, bind_host: str, port: int) -> None:
        """Load the bundled ha-mcp's listener helpers for ``bind_host``."""
        self._bind_host = bind_host
        self._port = port
        self._sockets: list[socket.socket] = []
        self._guard: Callable[[Any], Any] | None = None
        self._bind_sockets: Callable[[str, int], list[Any] | None] | None = None
        self._announce: Callable[[list[Any], str, int], None] | None = None
        if bind_host != BIND_HOST_ALL:
            return
        try:
            from ha_mcp.ipv6_lan_guard import (
                IPv6LanGuard,
                configured_allowed_networks,
            )
            from ha_mcp.listen_sockets import (
                announce_when_listening,
                wildcard_listen_sockets,
            )
        except ImportError:
            _LOGGER.info(
                "The bundled ha-mcp predates IPv6 listening: Local network "
                "accepts IPv4 clients only"
            )
            return
        allowed = configured_allowed_networks()
        self._guard = lambda app: IPv6LanGuard(app, allowed_networks=allowed)
        self._bind_sockets = wildcard_listen_sockets
        self._announce = announce_when_listening

    def wrap(self, app: Any) -> tuple[Any, bool]:
        """Return the app to serve and uvicorn's ``proxy_headers`` flag.

        "Local network" also listens on IPv6, which has no NAT, so the app is
        wrapped in the guard that keeps IPv6 to the local network (#2915). The
        guard re-applies proxy-header handling after its own check, so
        uvicorn's is turned off.
        """
        if self._guard is None:
            return app, True
        return self._guard(app), False

    def bind(self) -> dict[str, Any]:
        """Return uvicorn ``serve()`` kwargs, binding IPv4 and IPv6 for "Local network"."""
        if self._bind_sockets is None or self._announce is None:
            return {}
        sockets = self._bind_sockets(self._bind_host, self._port)
        if not sockets:
            return {}
        self._sockets = sockets
        self._announce(sockets, self._bind_host, self._port)
        return {"sockets": sockets}

    def close(self) -> None:
        """Release the sockets: uvicorn closes them only on a clean shutdown.

        A serve that exits early would otherwise hold the port into the next
        bring-up inside Home Assistant's long-lived process.
        """
        for sock in self._sockets:
            sock.close()


def default_host_origin_guard_off() -> None:
    """Turn fastmcp's Host/Origin (DNS-rebinding) guard off by default.

    fastmcp >= 3.4.3 ships it on, and it would 421 the direct listener on the
    Local network choice (0.0.0.0:9584), reached on arbitrary hosts. Default it
    off to match the CLI / add-on entry points. Guard only the import: a
    bundled ha-mcp old enough to lack the helper also predates the guard, so
    there is nothing to disable.
    """
    try:
        from ha_mcp.transport_security import ensure_host_origin_guard_default_off
    except ImportError:
        return
    ensure_host_origin_guard_default_off()


async def async_probe_loopback(port: int, timeout: float) -> bool:
    """Return True if a loopback TCP connection to ``port`` succeeds.

    Probes 127.0.0.1 regardless of bind host: a 0.0.0.0 bind still accepts on
    loopback, and the forwarding webhook only ever talks to loopback.
    """
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port), timeout=timeout
        )
    except (TimeoutError, OSError):
        return False
    writer.close()
    with suppress(OSError, TimeoutError):
        await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
    return True
