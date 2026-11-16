"""A refused app port must explain listener setup before blaming routing.

Supervisor and the target app are the stand-ins of ``_tools_addons_core.py``:
Supervisor reports the app, and the proxy's request reaches a real socket.
"""

import asyncio
import socket
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from homeassistant.core import HomeAssistant

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools import tools_addons

from ._core_client import free_port
from ._tool_errors import rejected_call_body
from ._tools_addons_core import SLUG, Route, manage, outwait, timers
from ._tools_addons_core import app_route as app_route
from ._tools_addons_core import core_route as core_route
from ._tools_addons_core import core_supervisor as core_supervisor
from ._tools_addons_core import quick_app_http as quick_app_http
from ._tools_addons_core import supervisor as supervisor
from ._tools_addons_core import target_app as target_app

ESPHOME = "5c53de3b_esphome"
# The port ESPHome's dashboard listens on when its direct access is enabled.
ESPHOME_DASHBOARD_PORT = 6052


def _guidance(body: dict[str, Any]) -> str:
    error = body["error"]
    return " ".join(error.get("suggestions", [error["suggestion"]]))


@pytest.fixture
def dashboard_port(socket_enabled: None) -> Iterator[int]:
    """ESPHome's dashboard port, bound but never listened on: a connection to
    it is refused, and no other process can listen there during the test."""
    with socket.socket() as held:
        held.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        held.bind(("127.0.0.1", ESPHOME_DASHBOARD_PORT))
        yield ESPHOME_DASHBOARD_PORT


@pytest.fixture
def no_time_to_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    """The app proxy's HTTP client gives a connection no time to open."""
    build = tools_addons.build_addon_http_client

    def hurried(
        client: HomeAssistantClient, timeout: int, slug: str
    ) -> httpx.AsyncClient:
        http = build(client, timeout, slug)
        http.timeout = httpx.Timeout(timeout, connect=0.0)
        return http

    monkeypatch.setattr(tools_addons, "build_addon_http_client", hurried)


@pytest.mark.parametrize("websocket", [False, True], ids=["http", "websocket"])
@pytest.mark.parametrize("slug", ["test_addon", ESPHOME])
@pytest.mark.parametrize("dashboard", [True, False], ids=["port-6052", "other-port"])
async def test_refused_app_port_explains_listener_setup(
    app_route: Route,
    dashboard_port: int,
    websocket: bool,
    slug: str,
    dashboard: bool,
) -> None:
    """Both proxy transports must return actionable setup and auth guidance."""
    port = dashboard_port if dashboard else free_port()
    app_route.supervisor.running(slug)

    body = await rejected_call_body(
        manage(app_route.client, slug=slug, path="/", port=port, websocket=websocket)
    )

    assert body["error"]["code"] == "CONNECTION_FAILED"
    guidance = _guidance(body)
    assert f"ha_get_app(slug='{slug}')" in guidance
    assert f"listening on internal port {port}" in guidance
    if slug == ESPHOME and dashboard:
        assert "Network" in guidance and "restart" in guidance.lower()
        assert "leave_front_door_open" in guidance and "authentication" in guidance
        assert "explicit user approval" in guidance
    else:
        assert "leave_front_door_open" not in guidance
        assert "Configuration > Network" not in guidance


@pytest.mark.parametrize("direct", [False, True], ids=["ingress", "direct-port"])
async def test_connect_timeout_preserves_route_guidance(
    core_route: Route, no_time_to_connect: None, direct: bool
) -> None:
    """A request that never connected needs connectivity advice, not status checks."""
    core_route.supervisor.running()
    port = core_route.target.port if direct else None

    body = await rejected_call_body(
        manage(core_route.client, slug=SLUG, path="/", port=port)
    )

    guidance = _guidance(body)
    assert "still be running" not in guidance
    assert body["error"]["code"] == "CONNECTION_FAILED"
    if direct:
        assert "'port' parameter" in guidance and "Ingress" in guidance
    else:
        assert core_route.client.base_url in guidance and "connectivity" in guidance
    assert core_route.target.requests == []


@pytest.mark.parametrize("websocket", [False, True], ids=["http", "websocket"])
@pytest.mark.parametrize("direct", [False, True], ids=["ingress", "direct-port"])
async def test_app_timeout_does_not_change_listener_auth(
    hass: HomeAssistant,
    app_route: Route,
    quick_app_http: None,
    websocket: bool,
    direct: bool,
) -> None:
    """A connected app timing out must not prompt disabling authentication."""
    app_route.supervisor.running(ESPHOME)
    app_route.target.hang = True
    port = app_route.target.port if direct else None
    before = timers(hass)
    call = asyncio.create_task(
        manage(app_route.client, slug=ESPHOME, path="/", port=port, websocket=websocket)
    )
    await app_route.target.arrived.wait()
    if websocket:
        # The handshake waits on Core's clock; the HTTP request times out alone.
        await outwait(hass, 5, 11, before)

    body = await rejected_call_body(call)

    assert body["error"]["code"] == "TIMEOUT_OPERATION"
    guidance = _guidance(body)
    assert "leave_front_door_open" not in guidance
    assert "Configuration > Network" not in guidance
    assert "restart" not in guidance.lower()
    assert "'port' parameter" not in guidance
    assert "logs" in guidance.lower() and "retry" in guidance.lower()
