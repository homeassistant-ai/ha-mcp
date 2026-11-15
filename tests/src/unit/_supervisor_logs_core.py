"""The log tool's two ways to Supervisor, on a real ``hass``.

Inside the app the server's client asks Supervisor directly at
``SUPERVISOR_BASE_URL``, so ``app_route`` serves that address over real HTTP.
Anywhere else the client asks Core's ``/api/hassio/...`` proxy, which is
Core's own ``HassIOView`` as ``hassio``'s ``async_setup`` registers it; only the
Supervisor HTTP API behind it is answered by ``aioclient_mock``, as Core's own
hassio tests answer it. ``route`` is each of the two in turn.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from aiohttp import web
from homeassistant.components.hassio.http import HassIOView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.setup import async_setup_component
from pytest_homeassistant.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from yarl import URL

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_logs import LogTools

from ._log_tool_calls import call_kwargs

SUPERVISOR_TOKEN = "supervisor-token-for-tests"
# The Supervisor address Core's hassio tests give it.
CORE_SUPERVISOR_HOST = "127.0.0.1"


@dataclass(frozen=True)
class Seen:
    """One request that reached Supervisor."""

    path: str
    query: dict[str, str]
    accept: str | None
    authorization: str | None
    range: str | None


class Supervisor:
    """What Supervisor answers to a log request, and what reached it."""

    def __init__(self, mock: AiohttpClientMocker | None = None) -> None:
        self.status = 200
        self.text = ""
        self.reason: str | None = None
        # Never answer, or close the connection without an answer.
        self.hang = False
        self.drop = False
        self._mock = mock
        self._seen: list[Seen] = []

    def reply(
        self, text: str = "", status: int = 200, reason: str | None = None
    ) -> None:
        self.status, self.text, self.reason = status, text, reason

    @property
    def requests(self) -> list[Seen]:
        if self._mock is None:
            return self._seen
        return [
            Seen(
                url.path,
                dict(url.query),
                None,
                (headers or {}).get("Authorization"),
                (headers or {}).get("Range"),
            )
            for _method, url, _data, headers in self._mock.mock_calls
        ]

    async def serve_app(self, request: web.Request) -> web.StreamResponse:
        self._seen.append(
            Seen(
                request.path,
                dict(request.query),
                request.headers.get("Accept"),
                request.headers.get("Authorization"),
                request.headers.get("Range"),
            )
        )
        if self.hang:
            await asyncio.Event().wait()
        if self.drop:
            assert request.transport is not None
            request.transport.close()
            raise ConnectionResetError
        return web.Response(
            status=self.status,
            text=self.text,
            reason=self.reason,
            content_type="text/plain",
        )

    async def serve_core(
        self, method: str, url: URL, data: Any
    ) -> AiohttpClientMockResponse:
        assert not self.drop and self.reason is None, (
            "the Core route answers through aioclient_mock: no drop, no reason"
        )
        if self.hang:
            await asyncio.Event().wait()
        return AiohttpClientMockResponse(
            method,
            url,
            status=self.status,
            text=self.text,
            headers={"Content-Type": "text/plain"},
        )


@dataclass(frozen=True)
class Route:
    """The server's client, and the Supervisor it reaches by one route."""

    client: HomeAssistantClient
    supervisor: Supervisor
    in_app: bool


@pytest.fixture
async def core_proxy(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    monkeypatch: pytest.MonkeyPatch,
) -> Supervisor:
    """Core's hassio proxy in front of a Supervisor ``aioclient_mock`` answers.

    Registered before ``core_client`` starts Core's HTTP server, which freezes
    its routes, so a test takes it ahead of ``core_client``.
    """
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.delenv("HA_MCP_EMBEDDED", raising=False)
    assert await async_setup_component(hass, "http", {})
    hass.http.register_view(
        HassIOView(CORE_SUPERVISOR_HOST, async_get_clientsession(hass))
    )
    supervisor = Supervisor(aioclient_mock)
    aioclient_mock.get(
        re.compile(rf"^http://{re.escape(CORE_SUPERVISOR_HOST)}/"),
        side_effect=supervisor.serve_core,
    )
    return supervisor


@pytest.fixture
async def core_route(core_proxy: Supervisor, core_client: HomeAssistantClient) -> Route:
    """The client outside the app, reading Supervisor through Core's proxy."""
    return Route(core_client, core_proxy, in_app=False)


@pytest.fixture
async def app_route(
    core_client: HomeAssistantClient,
    aiohttp_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[Route]:
    """The client as the app runs it: Supervisor is reached directly."""
    supervisor = Supervisor()
    app = web.Application()
    app.router.add_get("/{path:.+}/logs", supervisor.serve_app)
    server = await aiohttp_client(app)
    monkeypatch.delenv("HA_MCP_EMBEDDED", raising=False)
    monkeypatch.setenv("SUPERVISOR_TOKEN", SUPERVISOR_TOKEN)
    monkeypatch.setenv("SUPERVISOR_BASE_URL", str(server.make_url("")).rstrip("/"))
    yield Route(core_client, supervisor, in_app=True)


@pytest.fixture(params=["app", "core"])
def route(request: pytest.FixtureRequest) -> Route:
    """Each way the client reaches Supervisor."""
    found: Route = request.getfixturevalue(f"{request.param}_route")
    return found


@dataclass(frozen=True)
class Target:
    """A log the tool reads from Supervisor, and the path it is served at."""

    source: str
    slug: str
    path: str


TARGETS = {
    "app": Target("supervisor", "core_mosquitto", "/addons/core_mosquitto/logs"),
    "service": Target("system_service", "host", "/host/logs"),
}


@pytest.fixture(params=list(TARGETS))
def target(request: pytest.FixtureRequest) -> Target:
    """An app's log, then a system service's."""
    found: Target = TARGETS[request.param]
    return found


async def ask(
    client: HomeAssistantClient, target: Target, **overrides: Any
) -> dict[str, Any]:
    """``ha_get_logs`` for ``target``, with ``overrides`` on its defaults."""
    kwargs = call_kwargs(**({"source": target.source, "slug": target.slug} | overrides))
    return await LogTools(client).get_logs(**kwargs)
