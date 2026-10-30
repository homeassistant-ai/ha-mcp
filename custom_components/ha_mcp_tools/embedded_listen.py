"""The in-process server's direct listener: its HTTP guards and sockets.

Built inside the worker's ha_mcp import window (#1904), so every ha_mcp helper
it needs is imported before that window closes.
"""

from __future__ import annotations

from typing import Any

from .const import BIND_HOST_ALL


class DirectListener:
    """Host/Origin guard default, IPv6 guard and sockets for the direct port."""

    def __init__(self, bind_host: str, port: int) -> None:
        """Load what the bundled ha-mcp provides for ``bind_host``."""
        self._bind_host = bind_host
        self._port = port
        self._sockets: list[Any] = []
        self._helpers: tuple[Any, ...] | None = None
        _default_host_origin_guard_off()
        if bind_host != BIND_HOST_ALL:
            return
        try:
            from ha_mcp.config import get_global_settings
            from ha_mcp.ipv6_lan_guard import IPv6LanGuard, parse_allowed_networks
            from ha_mcp.listen_sockets import (
                announce_when_listening,
                wildcard_listen_sockets,
            )
        except ImportError:
            # A bundled ha-mcp older than #2915 has neither helper: "Local
            # network" keeps its IPv4-only bind, as before.
            return
        allowed = parse_allowed_networks(get_global_settings().ipv6_allowed_networks)
        self._helpers = (
            IPv6LanGuard,
            allowed,
            wildcard_listen_sockets,
            announce_when_listening,
        )

    def wrap(self, app: Any) -> tuple[Any, bool]:
        """Return the app to serve and uvicorn's ``proxy_headers`` flag.

        "Local network" also listens on IPv6, which has no NAT, so the app is
        wrapped in the guard that keeps IPv6 to the local network (#2915). The
        guard re-applies proxy-header handling after its own check, so
        uvicorn's is turned off.
        """
        if self._helpers is None:
            return app, True
        guard, allowed, _bind, _announce = self._helpers
        return guard(app, allowed_networks=allowed), False

    def bind(self) -> dict[str, Any]:
        """Return uvicorn ``serve()`` kwargs, binding IPv4 and IPv6 for "Local network"."""
        if self._helpers is None:
            return {}
        _guard, _allowed, bind, announce = self._helpers
        self._sockets = bind(self._bind_host, self._port)
        announce(self._sockets, self._bind_host, self._port)
        return {"sockets": self._sockets}

    def close(self) -> None:
        """Release the sockets: uvicorn closes them only on a clean shutdown.

        A serve that exits early would otherwise hold the port into the next
        bring-up inside Home Assistant's long-lived process.
        """
        for sock in self._sockets:
            sock.close()


def _default_host_origin_guard_off() -> None:
    """Turn fastmcp's Host/Origin (DNS-rebinding) guard off by default.

    fastmcp >= 3.4.3 ships it on, and it would 421 the direct LAN listener
    (bind 0.0.0.0:9584 by default), reached on arbitrary hosts. Default it off
    to match the CLI / add-on entry points. Guard only the import: a bundled
    ha-mcp old enough to lack the helper also predates the guard, so there is
    nothing to disable.
    """
    try:
        from ha_mcp.transport_security import ensure_host_origin_guard_default_off
    except ImportError:
        return
    ensure_host_origin_guard_default_off()
