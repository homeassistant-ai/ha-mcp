"""The settings page's routes over HTTP, in a world of the test's own.

``settings_http`` serves the handlers ``build_settings_handlers`` returns on the
paths of ``SETTINGS_ROUTES``, as the stdio sidecar mounts them; ``mounted_http``
serves what ``register_settings_routes`` put on a FastMCP server. Requests are
real HTTP requests through Starlette, so a body is JSON on the wire.

Inside the app the handlers ask Supervisor directly; ``in_app`` points them at
the stand-in Supervisor of ``_tools_addons_core``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Route

import ha_mcp
from ha_mcp._vendor.fastmcp import FastMCP
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.config import (
    BACKUP_OVERRIDE_FIELDS,
    FEATURE_FLAG_FIELDS,
    Settings,
    get_global_settings,
    reset_global_settings,
)
from ha_mcp.server import HomeAssistantSmartMCPServer
from ha_mcp.settings_ui import SETTINGS_ROUTES, build_settings_handlers
from ha_mcp.utils.data_paths import get_data_dir

from ._real_server import build_server
from ._tools_addons_core import APP_TOKEN, Supervisor

# Supervisor's own add-on, as the handlers name it.
SELF_INFO = ("GET", "/addons/self/info")
SELF_OPTIONS = ("POST", "/addons/self/options")
SELF_RESTART = ("POST", "/addons/self/restart")

_HA_MCP = Path(ha_mcp.__file__).parent
_VENDORED = _HA_MCP / "_vendor"


@pytest.fixture(autouse=True)
def settings_world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """Settings files in ``tmp_path``, none of the server's variables set, outside
    the app, and the settings page mounted nowhere yet."""
    monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(tmp_path))
    for name in (
        "SUPERVISOR_TOKEN",
        "SUPERVISOR_BASE_URL",
        "HA_MCP_EMBEDDED",
        "DISABLED_TOOLS",
        "PINNED_TOOLS",
    ):
        monkeypatch.delenv(name, raising=False)
    for _field, env_name, _type in (*FEATURE_FLAG_FIELDS, *BACKUP_OVERRIDE_FIELDS):
        monkeypatch.delenv(env_name, raising=False)
    # Process-wide records of the last registration, put back after the test.
    monkeypatch.setattr("ha_mcp.settings_ui._http_settings_prefix", None)
    monkeypatch.setattr("ha_mcp.settings_ui._http_settings_mounted", False)
    get_data_dir.cache_clear()
    reset_global_settings()
    yield tmp_path
    get_data_dir.cache_clear()
    reset_global_settings()


def with_env(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    """Set ``env`` and return the settings read from it."""
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    reset_global_settings()
    return get_global_settings()


def in_app(monkeypatch: pytest.MonkeyPatch, supervisor: Supervisor) -> None:
    """Run as the app does: with its token, asking ``supervisor`` directly."""
    monkeypatch.setenv("SUPERVISOR_TOKEN", APP_TOKEN)
    monkeypatch.setenv("SUPERVISOR_BASE_URL", f"http://{supervisor.url}")
    reset_global_settings()


def app_server(
    monkeypatch: pytest.MonkeyPatch, client: HomeAssistantClient
) -> HomeAssistantSmartMCPServer:
    """The real ha-mcp server the settings handlers read, on ``client``, built
    from the current environment (see ``_real_server.build_server``)."""
    return build_server(monkeypatch, client)


def _client(app: Any, peer: str) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, client=(peer, 51234))
    return httpx.AsyncClient(transport=transport, base_url="http://settings")


@asynccontextmanager
async def settings_http(
    server: HomeAssistantSmartMCPServer | None = None,
    *,
    is_sidecar: bool = False,
    peer: str = "127.0.0.1",
) -> AsyncIterator[httpx.AsyncClient]:
    """The settings routes of ``server`` (the sidecar's without one)."""
    handlers = build_settings_handlers(server, is_sidecar=is_sidecar)
    app = Starlette(
        routes=[
            Route(path, handlers[key], methods=methods)
            for path, methods, key in SETTINGS_ROUTES
        ]
    )
    async with _client(app, peer) as http:
        yield http


@asynccontextmanager
async def mounted_http(
    mcp: FastMCP, *, peer: str = "127.0.0.1"
) -> AsyncIterator[httpx.AsyncClient]:
    """What ``mcp`` serves over HTTP, asked from ``peer``."""
    async with _client(mcp.http_app(path="/mcp", stateless_http=True), peer) as http:
        yield http


def ha_mcp_tasks() -> set[asyncio.Task[Any]]:
    """The unfinished tasks running ha-mcp's own code (not a vendored package's)."""
    found = set()
    for task in asyncio.all_tasks():
        code = getattr(task.get_coro(), "cr_code", None)
        if task.done() or code is None:
            continue
        parents = Path(code.co_filename).parents
        if _HA_MCP in parents and _VENDORED not in parents:
            found.add(task)
    return found
