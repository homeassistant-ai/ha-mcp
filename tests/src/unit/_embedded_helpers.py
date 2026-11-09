"""Test doubles for the embedded server's webhook relay and worker.

``make_request`` / ``FakeSession`` / ``FakeUpstream`` stand in for the aiohttp
request and client session the forwarding handler is handed, so tests can
assert on the forwarded call and stream chunks without a network.
``no_direct_listener_helpers`` keeps a stubbed ``_serve`` off real sockets.
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import HttpVersion11


def response_json(response: Any) -> Any:
    """The JSON body of an ``aiohttp.web.Response`` a view returned."""
    return json.loads(response.text)


class StreamWriter:
    """The connection an ``aiohttp.web.StreamResponse`` streams to, recorded."""

    def __init__(self) -> None:
        self.chunks: list[bytes] = []
        self.eof = False
        self.length: int | None = None
        self.output_size = 0

    def enable_chunking(self) -> None:
        pass

    def send_headers(self) -> None:
        pass

    async def write_headers(self, status_line: str, headers: Any) -> None:
        pass

    async def write(self, data: bytes) -> None:
        self.chunks.append(bytes(data))

    async def write_eof(self, data: bytes = b"") -> None:
        if data:
            self.chunks.append(bytes(data))
        self.eof = True

    async def drain(self) -> None:
        pass


def stream_ready(request: MagicMock) -> StreamWriter:
    """Give a fake request what Core's real ``StreamResponse.prepare`` reads."""
    writer = StreamWriter()
    request.version = HttpVersion11
    request.keep_alive = True
    request._payload_writer = writer
    request._prepare_hook = AsyncMock()
    return writer


def make_request(
    *,
    headers: dict[str, str] | None = None,
    method: str = "POST",
    body: bytes = b"",
    scheme: str = "https",
) -> MagicMock:
    """Build a fake aiohttp request with a plain-dict headers mapping.

    A streamed response's chunks land in ``request._payload_writer.chunks``.
    """
    req = MagicMock(name="Request")
    req.headers = dict(headers or {})
    req.method = method
    req.scheme = scheme
    req.read = AsyncMock(return_value=body)
    stream_ready(req)
    return req


class FakeUpstream:
    """Fake upstream response returned by the forwarding session."""

    def __init__(
        self,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
        chunks: list[bytes] | None = None,
        stream_exc: BaseException | None = None,
    ) -> None:
        self.status = status
        self.headers = dict(headers or {})
        self._body = body
        self._chunks = chunks or []
        self._stream_exc = stream_exc
        self.content = SimpleNamespace(iter_any=self._iter_any)

    async def read(self) -> bytes:
        return self._body

    async def _iter_any(self):
        for chunk in self._chunks:
            yield chunk
        if self._stream_exc is not None:
            raise self._stream_exc


class _UpstreamCtx:
    def __init__(
        self, upstream: FakeUpstream | None, exc: BaseException | None
    ) -> None:
        self._upstream = upstream
        self._exc = exc

    async def __aenter__(self) -> FakeUpstream:
        if self._exc is not None:
            raise self._exc
        assert self._upstream is not None
        return self._upstream

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class FakeSession:
    """Fake aiohttp ClientSession recording the forwarded request args."""

    def __init__(
        self,
        *,
        upstream: FakeUpstream | None = None,
        exc: BaseException | None = None,
    ) -> None:
        self._upstream = upstream
        self._exc = exc
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def request(self, **kwargs: Any) -> _UpstreamCtx:
        self.calls.append(kwargs)
        return _UpstreamCtx(self._upstream, self._exc)

    async def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def no_direct_listener_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the worker against a server without the #2915 listener helpers.

    Tests that drive ``_serve`` stub ``ha_mcp`` piecemeal; a real
    ``ha_mcp.listen_sockets`` / ``ha_mcp.ipv6_lan_guard`` cached by another
    test would otherwise bind real sockets behind their stub uvicorn. A test
    module opts in by importing this fixture; ``test_embedded_listen`` covers
    the helpers themselves.
    """
    monkeypatch.setitem(sys.modules, "ha_mcp.listen_sockets", None)
    monkeypatch.setitem(sys.modules, "ha_mcp.ipv6_lan_guard", None)
