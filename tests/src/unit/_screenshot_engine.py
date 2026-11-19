"""A screenshot engine serving queued renders over loopback HTTP."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from aiohttp import web
from pytest_homeassistant.typing import ClientSessionGenerator

from ._dashboard_screenshot_common import PNG, set_settings


@dataclass
class Render:
    status: int = 200
    content: bytes = PNG
    text: str = ""
    content_type: str = "image/png"
    content_length: int | None = None
    chunks: list[bytes] | None = None
    hold_body: bool = False


@dataclass
class Engine:
    url: str = ""
    gets: list[dict[str, Any]] = field(default_factory=list)
    next_response: Render | list[Render | None] | None = field(default_factory=Render)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def handle(self, request: web.Request) -> web.StreamResponse:
        self.gets.append(
            {"path": request.rel_url.raw_path, "params": dict(request.query)}
        )
        reply = self.next_response
        if isinstance(reply, list):
            reply = reply.pop(0)
        if reply is None:
            await self.release.wait()
            return web.Response(status=504)

        response = web.StreamResponse(status=reply.status)
        response.content_type = reply.content_type
        if reply.content_length is not None:
            response.content_length = reply.content_length
        await response.prepare(request)
        if reply.hold_body:
            await self.release.wait()
            return response
        for chunk in reply.chunks or [reply.text.encode() or reply.content]:
            await response.write(chunk)
        return response


@pytest.fixture
async def engine(
    aiohttp_client: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
    socket_enabled: None,
) -> AsyncIterator[Engine]:
    engine = Engine()
    app = web.Application()
    app.router.add_get("/{path:.*}", engine.handle)
    client = await aiohttp_client(app)
    engine.url = str(client.make_url("")).rstrip("/")
    set_settings(monkeypatch, dashboard_screenshot_engine_url=engine.url)
    try:
        yield engine
    finally:
        engine.release.set()
