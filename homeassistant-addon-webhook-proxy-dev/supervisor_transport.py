"""Send an app's Supervisor API request over its Unix socket when it has one.

Supervisor 2026.11 mounts a socket in every app (``SUPERVISOR_SOCKET``) and
deprecates the app's token over TCP. On the socket Supervisor identifies the
app by the socket itself and reads no token, so the token is not sent there.
Without the socket (an older Supervisor) the request goes over TCP unchanged.

The ha-mcp app and the webhook proxy app ship byte-identical copies of this
file, which ``tests/addon/test_addon_startup.py`` keeps equal.
"""

from __future__ import annotations

import functools
import http.client
import os
import socket
import urllib.request
from typing import Any


def urlopen_supervisor(req: urllib.request.Request, timeout: float) -> Any:
    """Open ``req`` (an ``http://supervisor`` URL) as ``urlopen`` would."""
    socket_path = os.environ.get("SUPERVISOR_SOCKET")
    if not socket_path:
        return urllib.request.urlopen(req, timeout=timeout)
    req.remove_header("Authorization")
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _SocketHandler(socket_path)
    )
    return opener.open(req, timeout=timeout)


class _SocketConnection(http.client.HTTPConnection):
    """An HTTP connection to Supervisor over the app's Unix socket."""

    def __init__(self, socket_path: str, host: str, **kwargs: Any) -> None:
        super().__init__(host, **kwargs)
        self._socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


class _SocketHandler(urllib.request.HTTPHandler):
    """Send every ``http://`` request over the app's Unix socket."""

    def __init__(self, socket_path: str) -> None:
        super().__init__()
        self._socket_path = socket_path

    def http_open(self, req: urllib.request.Request) -> Any:
        return self.do_open(
            functools.partial(_SocketConnection, self._socket_path), req
        )
