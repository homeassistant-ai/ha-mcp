"""A real MCP server on loopback for the LLM API tests (issue #1745).

The component's one collaborator that is not Home Assistant is the in-process
ha-mcp server it reaches over loopback streamable HTTP. This module stands up
a small server speaking real MCP through the SDK's own server classes, so the
component's real client, transport and error mapping run against a real peer.
It lists what the real server stamps on ``tools/list``: per-tool
``_meta.ha_mcp`` exposure, the compact params line and initialize instructions.
"""

from __future__ import annotations

import asyncio
import socket
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

import pytest
import uvicorn
import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from pytest_homeassistant.common import MockConfigEntry
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from custom_components.ha_mcp_tools import llm_api
from custom_components.ha_mcp_tools.const import (
    DOMAIN,
    EXPOSURE_FULL,
    EXPOSURE_TOOL_SEARCH,
    OPT_LLM_API_EXPOSURE,
)

from ._llm_api_helpers import llm_context

SECRET_PATH = "/private_x"
ENTRY_ID = "entry-1745"
SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
}


@dataclass
class ServedTool:
    """One tool the server lists, with the stamp ha-mcp puts on it."""

    name: str = "ha_search"
    description: str | None = None
    input_schema: dict[str, Any] = field(default_factory=lambda: dict(SEARCH_SCHEMA))
    exposed: bool = True
    pinned: bool = False
    stamped: bool = True
    params: str | None = "query (string)"
    annotations: dict[str, Any] | None = None
    result: types.CallToolResult | None = None

    def listing(self) -> types.Tool:
        namespace: dict[str, Any] = {
            "llm_api_exposed": self.exposed,
            "pinned": self.pinned,
        }
        if self.params is not None:
            namespace["params"] = self.params
        return types.Tool.model_validate(
            {
                "name": self.name,
                "description": self.description or f"{self.name} description",
                "inputSchema": self.input_schema,
                "annotations": self.annotations,
                "_meta": {"ha_mcp": namespace} if self.stamped else None,
            }
        )


@dataclass
class LoopbackServer:
    """What the test controls on the running server, and what it saw."""

    instructions: str | None = "Use the skills-first workflow."
    port: int = 0
    tools: list[ServedTool] = field(default_factory=lambda: [ServedTool()])
    call_delay: float = 0.0
    list_delay: float = 0.0
    list_error: BaseException | None = None
    empty_list_answer: bool = False
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)


class _Asgi:
    """An ASGI endpoint: Starlette treats a plain callable object as an app."""

    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._app(scope, receive, send)


def _build_app(state: LoopbackServer) -> Starlette:
    server: Server[Any] = Server(
        "ha-mcp-loopback-test", instructions=state.instructions
    )

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        if state.list_delay:
            await asyncio.sleep(state.list_delay)
        if state.list_error is not None:
            raise state.list_error
        return [tool.listing() for tool in state.tools]

    if state.empty_list_answer:

        async def answer_with_nothing(_request: Any) -> types.ServerResult:
            return types.ServerResult(types.EmptyResult())

        server.request_handlers[types.ListToolsRequest] = answer_with_nothing

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, Any]) -> Any:
        state.calls.append((name, arguments))
        if state.call_delay:
            await asyncio.sleep(state.call_delay)
        served = next(t for t in state.tools if t.name == name)
        if served.result is not None:
            return served.result
        return [types.TextContent(type="text", text="ok")]

    # JSON answers rather than SSE: the event-stream response keeps a shutdown
    # watcher task alive past the test, which Core's plugin refuses.
    manager = StreamableHTTPSessionManager(
        app=server, stateless=True, json_response=True
    )

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with manager.run():
            yield

    return Starlette(
        routes=[
            Route(
                SECRET_PATH,
                endpoint=_Asgi(manager.handle_request),
                methods=["POST", "DELETE"],
            )
        ],
        lifespan=lifespan,
    )


@asynccontextmanager
async def serve(
    tools: list[ServedTool] | None = None,
    *,
    instructions: str | None = "Use the skills-first workflow.",
    empty_list_answer: bool = False,
    list_error: BaseException | None = None,
) -> AsyncIterator[LoopbackServer]:
    """Run the server on a free loopback port until the block exits."""
    state = LoopbackServer(
        instructions=instructions,
        empty_list_answer=empty_list_answer,
        list_error=list_error,
    )
    if tools is not None:
        state.tools = tools
    config = uvicorn.Config(
        _build_app(state),
        host="127.0.0.1",
        port=0,
        log_level="warning",
        lifespan="on",
        timeout_graceful_shutdown=1,
    )
    uvicorn_server = uvicorn.Server(config)
    task = asyncio.create_task(uvicorn_server.serve())
    try:
        while not uvicorn_server.started:
            if task.done():
                task.result()
            await asyncio.sleep(0.01)
        state.port = uvicorn_server.servers[0].sockets[0].getsockname()[1]
        yield state
    finally:
        uvicorn_server.should_exit = True
        await asyncio.wait_for(task, timeout=60)


def unused_port() -> int:
    """A loopback port nothing listens on: a refused connection, for real."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def register(
    hass: HomeAssistant, port: int, exposure: str = EXPOSURE_TOOL_SEARCH
) -> str:
    """Register the component's LLM API for the server on ``port``; its id."""
    entry = hass.config_entries.async_get_entry(ENTRY_ID)
    if entry is None:
        entry = MockConfigEntry(domain=DOMAIN, title="HA-MCP Server", entry_id=ENTRY_ID)
        entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, options={OPT_LLM_API_EXPOSURE: exposure}
    )
    await llm_api.async_register_llm_api(
        hass, entry, port=port, secret_path=SECRET_PATH
    )
    suffix = "" if exposure == EXPOSURE_FULL else "-toolsearch"
    return f"{DOMAIN}-{ENTRY_ID}{suffix}"


async def api_instance(
    hass: HomeAssistant, port: int, exposure: str = EXPOSURE_TOOL_SEARCH
) -> llm.APIInstance:
    """What a conversation agent gets from Core for the registered API."""
    api_id = await register(hass, port, exposure)
    return await llm.async_get_api(hass, api_id, llm_context())


def tool_named(instance: llm.APIInstance, name: str) -> llm.Tool:
    return next(tool for tool in instance.tools if tool.name == name)


async def call(
    hass: HomeAssistant, tool: llm.Tool, args: dict[str, Any]
) -> llm.ToolResult:
    """Call ``tool`` the way a conversation agent does."""
    result = await tool.async_call(hass, llm.ToolInput(tool.name, args), llm_context())
    assert isinstance(result, llm.ToolResult)
    return result


def mirrored_tool(port: int) -> llm_api.HaMcpTool:
    """The component's tool for ``ha_search`` on the server at ``port``."""
    return llm_api.HaMcpTool(
        "ha_search", "Search", vol.Schema({}), f"http://127.0.0.1:{port}{SECRET_PATH}"
    )


def use_sdk_v2(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Home Assistant's shared ``mcp`` on the 2.x line, for the next session.

    The 2.x SDK is ha-mcp's vendored copy: the component finds the SDK by the
    ``mcp.*`` names, so those names are pointed at it. Returns its transport
    module.
    """
    import ha_mcp._vendor.mcp as sdk
    from ha_mcp._vendor.mcp.client import session, streamable_http

    monkeypatch.setitem(sys.modules, "mcp", sdk)
    monkeypatch.setitem(sys.modules, "mcp.client.session", session)
    monkeypatch.setitem(sys.modules, "mcp.client.streamable_http", streamable_http)
    return streamable_http


def payload(result: llm.ToolResult) -> dict[str, Any]:
    """The dict a tool answered with."""
    assert isinstance(result.data, dict)
    return result.data
