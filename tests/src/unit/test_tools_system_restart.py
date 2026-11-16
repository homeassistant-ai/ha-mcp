"""``ha_restart`` from a server outside Home Assistant, on real Home Assistant Core.

Core's own ``homeassistant.restart`` runs, with its config check; only the
stop handler it ends with is the test's (``async_set_stop_handler``, Core's
hook for that), so the test's Core records the restart instead of stopping.
Behind a reverse proxy, Home Assistant going down mid-request reaches the
server as the proxy's own error (#612, #1666): ``_gateway`` is that proxy.
"""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from aiohttp import web
from homeassistant.components.homeassistant import async_set_stop_handler
from homeassistant.components.recorder import Recorder
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.tools.tools_system import SystemTools

from ._gateway import Gateway
from ._gateway import gateway as gateway
from ._gateway import gateway_client as gateway_client


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
    gateway: Gateway, core_client: HomeAssistantClient, status: int
) -> None:
    """The gateway hands the restart to Core, then answers ``status`` itself:
    Home Assistant went down before its answer reached the gateway."""

    async def relay_then_fail(request: web.Request) -> web.Response:
        async with (
            aiohttp.ClientSession() as session,
            session.post(
                f"{core_client.base_url}{request.path}",
                headers={"Authorization": request.headers["Authorization"]},
                data=await request.read(),
            ) as answer,
        ):
            assert answer.status == 200
        return web.Response(status=status, text="no reply from upstream")

    gateway.routes.append(
        web.post("/api/services/homeassistant/restart", relay_then_fail)
    )


@pytest.mark.parametrize("status", [502, 503, 504])
@pytest.mark.usefixtures("restart_answer_lost")
async def test_a_gateway_error_after_core_got_the_restart_is_reported_as_restarting(
    stops: list[bool], gateway_client: HomeAssistantClient
) -> None:
    """The restart happened: the user is told to wait for it, not that it failed."""
    result = await SystemTools(gateway_client).ha_restart(confirm=True)

    assert stops == [True]
    assert result["success"] is True
    assert any("Wait 1-5 minutes" in w for w in result["warnings"])


@pytest.mark.parametrize(
    "message", ["Proxy returned: Bad Gateway", "Upstream Service Unavailable"]
)
async def test_a_lost_connection_naming_the_gateway_is_reported_as_restarting(
    core_client: HomeAssistantClient, stops: list[bool], message: str
) -> None:
    with patch.object(
        core_client,
        "call_service",
        AsyncMock(side_effect=HomeAssistantConnectionError(message)),
    ):
        result = await SystemTools(core_client).ha_restart(confirm=True)

    assert result["success"] is True
    assert any("Wait 1-5 minutes" in w for w in result["warnings"])


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
