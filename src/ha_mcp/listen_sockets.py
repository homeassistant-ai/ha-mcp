"""Listening sockets for the IPv4 wildcard bind host (issue #2915).

Binding ``0.0.0.0`` accepts IPv4 clients only, and ``::`` alone is IPv6-only
because asyncio sets ``IPV6_V6ONLY``. The app and the in-process server keep
``0.0.0.0`` as their configured and displayed host, so every log line and
connect URL stays as before, and hand uvicorn one socket per address family.

Stdlib only: the component's tests load this file without importing ha_mcp.
"""

from __future__ import annotations

import errno
import logging
import socket
from collections.abc import Callable

logger = logging.getLogger(__name__)

ALL_INTERFACES_V4 = "0.0.0.0"


def wildcard_listen_sockets(host: str, port: int) -> list[_ListenSocket] | None:
    """Bind ``port`` on every IPv4 and IPv6 address when ``host`` is ``0.0.0.0``.

    Returns ``None`` for any other host, which uvicorn then binds itself. Like
    asyncio's ``create_server(host=None)``, a family the machine has disabled is
    skipped. IPv6 is also skipped, with a warning, when another process holds
    the port there while IPv4 bound. Any other bind error (such as the port
    being in use on IPv4) is raised, and so is ``OSError`` when no family binds.
    """
    if host != ALL_INTERFACES_V4:
        return None
    sockets: list[_ListenSocket] = []
    try:
        for family, kind, proto, _name, address in socket.getaddrinfo(
            None, port, socket.AF_UNSPEC, socket.SOCK_STREAM, 0, socket.AI_PASSIVE
        ):
            try:
                sock = _ListenSocket(family, kind, proto)
            except OSError:
                continue
            sockets.append(sock)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6:
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            try:
                sock.bind(address)
            except OSError as err:
                if not _ipv6_bind_skippable(err, family, sockets, port):
                    raise
                sockets.remove(sock)
                sock.close()
    except BaseException:
        for opened in sockets:
            opened.close()
        raise
    if not sockets:
        raise OSError(f"No address family could bind port {port}")
    return sockets


def _ipv6_bind_skippable(
    err: OSError, family: int, sockets: list[_ListenSocket], port: int
) -> bool:
    """Return True when a failed IPv6 bind should leave the server on IPv4."""
    if family != socket.AF_INET6:
        return False
    if err.errno == errno.EADDRNOTAVAIL:
        return True  # IPv6 disabled with sysctl
    # Another process holding the port on IPv6 only must not stop the IPv4
    # listener the server had before IPv6 was added.
    if err.errno == errno.EADDRINUSE and any(
        sock.family == socket.AF_INET for sock in sockets
    ):
        logger.warning("Port %d is already in use on IPv6; serving IPv4 only", port)
        return True
    return False


def announce_when_listening(sockets: list[_ListenSocket], host: str, port: int) -> None:
    """Log uvicorn's "Uvicorn running on ..." line once ``sockets`` are serving.

    uvicorn skips that line when handed pre-bound sockets, assuming a process
    manager logged it. The sockets log it themselves from the ``listen()`` call
    that starts serving them, which is the point in uvicorn's startup where it
    would have logged it, on its thread, so the startup log reads as before.
    """
    pending = {id(sock) for sock in sockets}

    def listened(sock: socket.socket) -> None:
        pending.discard(id(sock))
        if not pending:
            logging.getLogger("uvicorn.error").info(
                "Uvicorn running on http://%s:%d (Press CTRL+C to quit)", host, port
            )

    for sock in sockets:
        sock.on_listen = listened


class _ListenSocket(socket.socket):
    """A socket that reports the ``listen()`` call that starts serving it."""

    on_listen: Callable[[socket.socket], None] | None = None

    def listen(self, *args: int) -> None:
        super().listen(*args)
        if self.on_listen is not None:
            callback, self.on_listen = self.on_listen, None
            callback(self)
