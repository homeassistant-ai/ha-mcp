"""The world the developer tools run in, on a real ``hass``.

The settings, Tools-tab and policy actions write files in the data dir that
``_dev_tool_env`` gives each test. The server they are registered on is the
real one (``build_server``) with developer mode on, so the tool registry and the
approval queue they read are the server's own. An add-on's Supervisor is an
upstream, served over loopback HTTP at ``SUPERVISOR_BASE_URL``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from homeassistant.core import HomeAssistant

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.config import reset_global_settings
from ha_mcp.policy.approval_queue import compute_args_hash
from ha_mcp.policy.model import Rule
from ha_mcp.policy.persistence import load_policy
from ha_mcp.server import HomeAssistantSmartMCPServer
from ha_mcp.tools.tools_dev import FEATURE_FLAG, DevTools
from ha_mcp.utils.data_paths import get_data_dir

from ._real_server import build_server
from ._tools_addons_core import app_info

# Where the web settings UI saves the settings it overrides.
OVERRIDE_FILE = "feature_flags.json"


def override_path() -> Path:
    return get_data_dir() / OVERRIDE_FILE


def write_overrides(values: dict[str, Any]) -> None:
    """Save ``values`` the way the web settings UI does."""
    override_path().write_text(json.dumps(values))


def stored_overrides() -> dict[str, Any]:
    stored: dict[str, Any] = json.loads(override_path().read_text())
    return stored


def tool_config() -> dict[str, Any]:
    """The saved Tools-tab state, or ``{}`` before anything was saved."""
    path = get_data_dir() / "tool_config.json"
    stored: dict[str, Any] = json.loads(path.read_text()) if path.exists() else {}
    return stored


def policy_rules() -> list[Rule]:
    return load_policy(get_data_dir()).rules


def serve(
    monkeypatch: pytest.MonkeyPatch,
    client: HomeAssistantClient,
    *,
    policies: bool = False,
) -> HomeAssistantSmartMCPServer:
    """The real server with developer mode on; with ``policies`` its approval
    queue is wired, as when security policies were on at startup."""
    monkeypatch.setenv(FEATURE_FLAG, "true")
    if policies:
        monkeypatch.setenv("ENABLE_TOOL_SECURITY_POLICIES", "true")
    return build_server(monkeypatch, client)


def pending_call(server: HomeAssistantSmartMCPServer) -> str:
    """A tool call waiting in the server's approval queue; its token."""
    args = {"domain": "light"}
    entry = server.approval_queue.create(
        "ha_call_service", compute_args_hash(args), args, ttl_minutes=5
    )
    return entry.token


async def policy_access(dev_tools: DevTools) -> bool:
    """Whether the dev tools may rewrite the tool security policy right now."""
    try:
        await dev_tools.ha_dev_manage_settings(
            action="set_policy", policy={"rules": []}
        )
    except ToolError as err:
        assert "AUTH_INSUFFICIENT_PERMISSIONS" in str(err)
        return False
    return True


@pytest.fixture
def dev_tools(hass: HomeAssistant, core_client: HomeAssistantClient) -> DevTools:
    """The dev tools on the server's client, for actions that never read the server."""
    return DevTools(core_client)


@pytest.fixture
def served(
    core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> DevTools:
    """The dev tools on the real server, whose tool registry they read."""
    return DevTools(core_client, server=serve(monkeypatch, core_client))


class Supervisor:
    """An add-on's Supervisor: the options it stores, and what reached it."""

    def __init__(self) -> None:
        self.options: dict[str, Any] = {}
        self.saved: list[dict[str, Any]] = []
        self.restarts = 0
        self.refusal: str | None = None

    async def info(self, request: web.Request) -> web.Response:
        return web.json_response({"result": "ok", "data": app_info(options=self.options)})

    async def save(self, request: web.Request) -> web.Response:
        if self.refusal is not None:
            return web.json_response(
                {"result": "error", "message": self.refusal}, status=400
            )
        self.options = (await request.json())["options"]
        self.saved.append(self.options)
        return web.json_response({"result": "ok", "data": {}})

    async def restart(self, request: web.Request) -> web.Response:
        self.restarts += 1
        return web.json_response({"result": "ok", "data": {}})


@pytest.fixture
async def supervisor(
    aiohttp_client: Any, monkeypatch: pytest.MonkeyPatch, socket_enabled: None
) -> Supervisor:
    """The server runs as an add-on whose Supervisor answers at its address."""
    upstream = Supervisor()
    app = web.Application()
    app.router.add_get("/addons/self/info", upstream.info)
    app.router.add_post("/addons/self/options", upstream.save)
    app.router.add_post("/addons/self/restart", upstream.restart)
    served_at = await aiohttp_client(app)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "supervisor-token-for-tests")
    monkeypatch.setenv("SUPERVISOR_BASE_URL", str(served_at.make_url("")).rstrip("/"))
    reset_global_settings()
    return upstream
