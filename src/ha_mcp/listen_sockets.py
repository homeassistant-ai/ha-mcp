"""Listening sockets for the IPv4 wildcard bind host (issue #2915).

Binding ``0.0.0.0`` accepts IPv4 clients only, and ``::`` alone is IPv6-only
because asyncio sets ``IPV6_V6ONLY``. The app and the in-process server keep
``0.0.0.0`` as their configured and displayed host, so every log line and
connect URL stays as before, and hand uvicorn one socket per address family.

Stdlib only: the component's tests load this file without importing ha_mcp.
"""

from __future__ import annotations

import errno
import socket

ALL_INTERFACES_V4 = "0.0.0.0"


def wildcard_listen_sockets(host: str, port: int) -> list[socket.socket] | None:
    """Bind ``port`` on every IPv4 and IPv6 address when ``host`` is ``0.0.0.0``.

    Returns ``None`` for any other host, which uvicorn then binds itself. Like
    asyncio's ``create_server(host=None)``, a family the machine has disabled is
    skipped; any other bind error (such as the port being in use) is raised.
    """
    if host != ALL_INTERFACES_V4:
        return None
    sockets: list[socket.socket] = []
    try:
        for family, kind, proto, _name, address in socket.getaddrinfo(
            None, port, socket.AF_UNSPEC, socket.SOCK_STREAM, 0, socket.AI_PASSIVE
        ):
            try:
                sock = socket.socket(family, kind, proto)
            except OSError:
                continue
            sockets.append(sock)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6:
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            try:
                sock.bind(address)
            except OSError as err:
                if family == socket.AF_INET6 and err.errno == errno.EADDRNOTAVAIL:
                    sockets.pop().close()
                    continue
                raise
    except BaseException:
        for sock in sockets:
            sock.close()
        raise
    if not sockets:
        raise OSError(f"No address family could bind port {port}")
    return sockets
