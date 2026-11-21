"""The settings UI serves the live tool-approval routes only where a queue exists.

A sidecar, no server, and a server running without tool security policies (no
approval queue) all fall back to stub handlers that answer 503 on the pending,
approve and deny routes. The decision PIN is a file in the data dir, so the stub
set serves it for real.
"""

from __future__ import annotations

import json

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Route

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.server import HomeAssistantSmartMCPServer
from ha_mcp.settings_ui import build_settings_handlers

from ._real_server import build_server

_ROUTES = {
    "policy_get_pending": ("GET", None),
    "policy_post_approve": ("POST", {"token": "nope"}),
    "policy_post_deny": ("POST", {"token": "nope"}),
    "policy_get_decision_pin": ("GET", None),
}


@pytest.fixture(autouse=True)
def _own_data_dir(tmp_path, monkeypatch):
    """Each case keeps its policy and PIN files in its own data dir."""
    from ha_mcp.utils.data_paths import get_data_dir

    monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(tmp_path))
    get_data_dir.cache_clear()
    yield
    get_data_dir.cache_clear()


def _server(
    setup: str, monkeypatch: pytest.MonkeyPatch, core_client: HomeAssistantClient
) -> HomeAssistantSmartMCPServer | None:
    if setup == "no_server":
        return None
    policies = "false" if setup == "no_queue" else "true"
    monkeypatch.setenv("ENABLE_TOOL_SECURITY_POLICIES", policies)
    return build_server(monkeypatch, core_client)


async def _answer(server, route_name: str, *, is_sidecar: bool) -> httpx.Response:
    handlers = build_settings_handlers(server, is_sidecar=is_sidecar)
    method, body = _ROUTES[route_name]
    app = Starlette(routes=[Route("/r", handlers[route_name], methods=[method])])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://s") as client:
        return await client.request(method, "/r", json=body)


@pytest.mark.parametrize(
    ("setup", "stub"),
    [("sidecar", True), ("no_server", True), ("no_queue", True), ("live", False)],
)
@pytest.mark.parametrize(
    ("route_name", "live_status"),
    [
        # pending lists an empty queue; approve/deny find no token "nope".
        ("policy_get_pending", 200),
        ("policy_post_approve", 404),
        ("policy_post_deny", 404),
    ],
)
async def test_the_approval_routes_are_live_only_with_a_queue(
    setup: str,
    stub: bool,
    route_name: str,
    live_status: int,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
):
    """A sidecar falls back to the stubs even with a queue present: the
    sidecar flag overrides it, for all three routes."""
    server = _server(setup, monkeypatch, core_client)
    if server is not None:
        queue = getattr(server, "approval_queue", None)
        assert (queue is None) is (setup == "no_queue")

    response = await _answer(server, route_name, is_sidecar=setup == "sidecar")

    assert response.status_code == (503 if stub else live_status)


@pytest.mark.parametrize("setup", ["sidecar", "no_server", "no_queue", "live"])
async def test_the_decision_pin_route_answers_in_every_mode(
    setup: str, core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
):
    """The config PUT refuses to switch event-bus decisions on while no PIN is
    stored, so a sidecar that 503'd this route would make that switch
    permanently unreachable there."""
    server = _server(setup, monkeypatch, core_client)

    response = await _answer(
        server, "policy_get_decision_pin", is_sidecar=setup == "sidecar"
    )

    assert response.status_code == 200
    assert json.loads(response.content) == {"set": False}
