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
import threading
import time

ALL_INTERFACES_V4 = "0.0.0.0"
# Cold starts import for minutes before uvicorn listens (#1904).
_ANNOUNCE_TIMEOUT_SECONDS = 900


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


def announce_when_listening(sockets: list[socket.socket], host: str, port: int) -> None:
    """Log uvicorn's "Uvicorn running on ..." line once ``sockets`` are serving.

    uvicorn skips that line when handed pre-bound sockets, assuming a process
    manager logged it. Emit the same text on uvicorn's own logger at the moment
    uvicorn would have (its listen() call, after application startup), so the
    startup log reads exactly as it did with a plain host bind.
    """

    def _wait() -> None:
        deadline = time.monotonic() + _ANNOUNCE_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                if all(
                    sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)
                    for sock in sockets
                ):
                    break
            except OSError:
                return  # closed before it ever served: startup failed
            time.sleep(0.05)
        else:
            return  # never started serving: uvicorn logged why
        logging.getLogger("uvicorn.error").info(
            "Uvicorn running on %s://%s:%d (Press CTRL+C to quit)", "http", host, port
        )

    # Named after the caller (uvicorn's own thread) so log formats that show
    # the thread name read the same too.
    name = threading.current_thread().name
    threading.Thread(target=_wait, name=name, daemon=True).start()
