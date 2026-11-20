"""``ha_restart`` from a server outside Home Assistant, on real Home Assistant Core.

Core's own ``homeassistant.restart`` runs, with its config check; only the
stop handler it ends with is the test's (``async_set_stop_handler``, Core's
hook for that), so the test's Core records the restart instead of stopping.
Behind a reverse proxy, Home Assistant going down mid-request reaches the
server as the proxy's own error (#612, #1666): ``_gateway`` is that proxy.
"""

import asyncio
from pathlib import Path
from unittest.mock import patch

import aiohttp
import pytest
from aiohttp import web
from homeassistant.components.homeassistant import async_set_stop_handler
from homeassistant.components.recorder import Recorder
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_system import SystemTools

from ._gateway import Gateway
from ._gateway import gateway as gateway
from ._gateway import gateway_client as gateway_client
from ._tool_errors import rejected_call_body

RESTART_PATH = "/api/services/homeassistant/restart"


@pytest.fixture(autouse=True)
def _outside_home_assistant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HA_MCP_EMBEDDED", raising=False)


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir: str) -> str:
    """A config dir whose ``configuration.yaml`` Core's restart check accepts."""
    Path(hass_tmp_config_dir, "configuration.yaml").write_text("homeassistant: {}\n")
    return hass_tmp_config_dir


@pytest.fixture
async def stops(hass: HomeAssistant, core_client: HomeAssistantClient) -> list[bool]:
    """Each stop Core was asked for (True for a restart)."""
    asked: list[bool] = []

    async def stop(hass: HomeAssistant, restart: bool) -> None:
        asked.append(restart)

    for domain in ("homeassistant", "config"):
        assert await async_setup_component(hass, domain, {})
    async_set_stop_handler(hass, stop)
    return asked


@pytest.fixture
def restart_answer_lost(
    gateway: Gateway,
    core_client: HomeAssistantClient,
    status: int | None,
) -> None:
    """The gateway hands the restart to Core, then loses the answer: it replies
    ``status`` itself or drops the connection, as when Core stops mid-request."""

    async def lose_answer(request: web.Request) -> web.Response:
        gateway.requests.append(f"{request.method} {request.path}")
        async with (
            aiohttp.ClientSession() as session,
            session.post(
                f"{core_client.base_url}{request.path}",
                headers={"Authorization": request.headers["Authorization"]},
                data=await request.read(),
            ) as answer,
        ):
            assert answer.status == 200
        if status is None:
            assert request.transport is not None
            request.transport.close()
            return web.Response()
        return web.Response(status=status, text="no reply from upstream")

    gateway.routes.append(web.post(RESTART_PATH, lose_answer))


@pytest.mark.parametrize("status", [502, 503, 504, None])
@pytest.mark.usefixtures("restart_answer_lost")
async def test_a_restart_whose_answer_is_lost_is_reported_as_restarting(
    stops: list[bool], gateway: Gateway, gateway_client: HomeAssistantClient
) -> None:
    """The restart happened: the user is told to wait for it, not that it failed."""
    result = await SystemTools(gateway_client).ha_restart(confirm=True)

    assert stops == [True]
    assert gateway.requests.count(f"POST {RESTART_PATH}") == 1
    assert result["success"] is True
    assert any("Wait 1-5 minutes" in w for w in result["warnings"])


@pytest.fixture
def gateway_goes_away_after_the_check(
    gateway: Gateway, core_client: HomeAssistantClient
) -> None:
    """The gateway answers the config check, then stops listening: the restart
    call that follows finds nothing to connect to."""

    async def check_then_stop(request: web.Request) -> web.Response:
        gateway.requests.append(f"{request.method} {request.path}")
        async with (
            aiohttp.ClientSession() as session,
            session.post(
                f"{core_client.base_url}{request.path}",
                headers={"Authorization": request.headers["Authorization"]},
            ) as answer,
        ):
            body = await answer.read()
        for site in list(gateway.sites):
            await site.stop()
        return web.Response(
            body=body, content_type="application/json", headers={"Connection": "close"}
        )

    gateway.routes.append(web.post("/api/config/core/check_config", check_then_stop))


@pytest.mark.usefixtures("gateway_goes_away_after_the_check")
async def test_a_restart_that_could_not_connect_is_not_reported_as_restarting(
    stops: list[bool], gateway: Gateway, gateway_client: HomeAssistantClient
) -> None:
    """Nothing was sent, so telling the user to wait would be for a restart
    that never comes."""
    body = await rejected_call_body(
        SystemTools(gateway_client).ha_restart(confirm=True)
    )

    assert gateway.requests == ["POST /api/config/core/check_config"]
    assert stops == []
    assert body["error"]["code"] == "CONNECTION_FAILED"


@pytest.fixture
def restart_redirected(gateway: Gateway) -> None:
    """The gateway answers the restart call with a redirect, which the client
    does not follow."""

    async def redirect(request: web.Request) -> web.Response:
        gateway.requests.append(f"{request.method} {request.path}")
        return web.Response(status=307, headers={"Location": "/elsewhere"})

    gateway.routes.append(web.post(RESTART_PATH, redirect))


@pytest.mark.usefixtures("restart_redirected")
async def test_a_redirected_restart_is_not_reported_as_restarting(
    stops: list[bool], gateway: Gateway, gateway_client: HomeAssistantClient
) -> None:
    """The restart never reached Core, so there is nothing to wait for."""
    body = await rejected_call_body(
        SystemTools(gateway_client).ha_restart(confirm=True)
    )

    assert gateway.requests.count(f"POST {RESTART_PATH}") == 1
    assert stops == []
    assert body["error"]["code"] == "CONNECTION_FAILED"


@pytest.fixture
def restart_over_websocket(gateway: Gateway) -> None:
    """An explicit REST 401 routes the service call through the client's WS path."""

    async def refuse_rest(request: web.Request) -> web.Response:
        gateway.requests.append(f"{request.method} {request.path}")
        return web.Response(status=401)

    gateway.routes.append(web.post(RESTART_PATH, refuse_rest))


@pytest.mark.parametrize(
    "accept_upgrade", [False, True], ids=["before-send", "after-send"]
)
@pytest.mark.usefixtures("restart_over_websocket")
async def test_a_lost_websocket_reports_only_what_the_client_knows(
    stops: list[bool],
    gateway: Gateway,
    gateway_client: HomeAssistantClient,
    accept_upgrade: bool,
) -> None:
    if accept_upgrade:
        # The gateway closes the socket on the frame, as Core does when it stops.
        gateway.drops.add("call_service")
    else:
        gateway.upgrade_status = 502
    with patch.object(
        gateway_client,
        "send_websocket_message",
        wraps=gateway_client.send_websocket_message,
    ) as ws:
        restart = SystemTools(gateway_client).ha_restart(confirm=True)
        if accept_upgrade:
            result = await restart
        else:
            body = await rejected_call_body(restart)

    assert gateway.requests.count(f"POST {RESTART_PATH}") == 1
    assert [call.args[0]["type"] for call in ws.await_args_list] == ["call_service"]
    if accept_upgrade:
        assert any("Wait 1-5 minutes" in w for w in result["warnings"])
    else:
        # The socket never opened, so nothing was sent.
        assert stops == []
        assert body["error"]["code"] == "CONNECTION_FAILED"


@pytest.mark.parametrize(
    "acknowledged", [True, False], ids=["acknowledged", "reply-timeout"]
)
@pytest.mark.usefixtures("restart_over_websocket")
async def test_websocket_acknowledgement_waits_for_the_stop_handler(
    hass: HomeAssistant,
    gateway_client: HomeAssistantClient,
    stops: list[bool],
    monkeypatch: pytest.MonkeyPatch,
    acknowledged: bool,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def stop(_hass: HomeAssistant, restart: bool) -> None:
        stops.append(restart)
        entered.set()
        await release.wait()

    async_set_stop_handler(hass, stop)
    if not acknowledged:
        monkeypatch.setattr(
            "ha_mcp.client.websocket_client.DEFAULT_COMMAND_WAIT_TIMEOUT", 2
        )
    task = asyncio.create_task(SystemTools(gateway_client).ha_restart(confirm=True))
    try:
        async with asyncio.timeout(15):
            await entered.wait()
            if acknowledged:
                assert not task.done()
                release.set()
                assert (await task)["success"] is True
            else:
                body = await rejected_call_body(task)
                assert body["error"]["code"] == "CONNECTION_TIMEOUT"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert stops == [True]


async def test_a_gateway_error_before_the_restart_was_sent_fails(
    stops: list[bool], gateway: Gateway, gateway_client: HomeAssistantClient
) -> None:
    """The gateway failed the config check, so no restart went out: reporting
    one would leave the user waiting for a restart that never comes."""
    gateway.outages.append(502)

    with pytest.raises(ToolError):
        await SystemTools(gateway_client).ha_restart(confirm=True)

    assert gateway.requests == ["POST /api/config/core/check_config"]
    assert stops == []


async def test_a_restart_core_refuses_is_not_reported_as_restarting(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    stops: list[bool],
) -> None:
    """Core refuses to restart during a database upgrade. A unit-test recorder
    cannot be mid-upgrade; Core sets this flag while it migrates."""
    with (
        patch.object(recorder_mock, "migration_in_progress", True),
        pytest.raises(ToolError, match="database upgrade"),
    ):
        await SystemTools(core_client).ha_restart(confirm=True)

    assert stops == []


async def test_a_restart_core_answers_warns_that_the_connection_will_drop(
    core_client: HomeAssistantClient, stops: list[bool]
) -> None:
    result = await SystemTools(core_client).ha_restart(confirm=True)

    assert stops == [True]
    assert result["success"] is True
    assert any("Connection will be lost" in w for w in result["warnings"])
