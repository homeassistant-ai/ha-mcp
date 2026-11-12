"""ha_get_system_health over the component's ``system_snapshot``, on a real ``hass``.

The snapshot and Core's own repairs command answer from the same ``hass``. A
snapshot command that fails, gets no answer or answers without its lists is
staged through Core's own WebSocket API (``_core_staging``); a dropped
connection is the client's own send raising its transport error.
"""

from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.components import websocket_api as core_websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client import websocket_client
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.component_api import COMPONENT_INSTALL_HINT
from ha_mcp.tools.tools_system import WS_SYSTEM_SNAPSHOT, SystemTools

from ._core_staging import never_answers, served_by
from ._tool_errors import _error


@pytest.fixture(autouse=True)
async def _core(hass: HomeAssistant) -> None:
    for domain in ("system_health", "repairs"):
        assert await async_setup_component(hass, domain, {})
    ir.async_create_issue(
        hass,
        "demo",
        "outdated",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="outdated",
    )


@pytest.fixture
def tools(component_core_client: HomeAssistantClient) -> SystemTools:
    return SystemTools(component_core_client)


@callback
def _fails(
    hass: HomeAssistant,
    connection: core_websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    raise HomeAssistantError("Registry unreadable")


@callback
def _answers_without_its_lists(
    hass: HomeAssistant,
    connection: core_websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    connection.send_result(msg["id"], {})


def _issue_ids(result: dict[str, Any]) -> list[str]:
    return [issue["issue_id"] for issue in result["repairs"]["issues"]]


async def test_a_served_snapshot_adds_no_warning(tools: SystemTools) -> None:
    result = await tools.ha_get_system_health(include="repairs")

    assert _issue_ids(result) == ["outdated"]
    assert "warnings" not in result


@pytest.mark.parametrize(
    ("handler", "failure"),
    [
        pytest.param(_fails, "Registry unreadable", id="error"),
        pytest.param(never_answers, "Command timeout", id="no_answer"),
        pytest.param(_answers_without_its_lists, "malformed reply", id="malformed"),
    ],
)
async def test_a_failing_snapshot_reads_home_assistant_and_warns(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    tools: SystemTools,
    handler: core_websocket_api.WebSocketCommandHandler,
    failure: str,
) -> None:
    # The wait bounds every command of the call; an in-process Core answers
    # the others in milliseconds.
    monkeypatch.setattr(websocket_client, "DEFAULT_COMMAND_WAIT_TIMEOUT", 1)
    served_by(hass, WS_SYSTEM_SNAPSHOT, handler)

    result = await tools.ha_get_system_health(include="repairs")

    assert _issue_ids(result) == ["outdated"]
    assert result["warnings"] == [
        f"{WS_SYSTEM_SNAPSHOT} failed: {failure}; the sections were read from "
        "Home Assistant directly. " + COMPONENT_INSTALL_HINT
    ]


async def test_a_lost_connection_during_the_snapshot_ends_the_call(
    monkeypatch: pytest.MonkeyPatch,
    component_core_client: HomeAssistantClient,
    tools: SystemTools,
) -> None:
    """CONNECTION_FAILED, not a component failure read around: the repairs
    would still answer on the tool's own health connection."""
    await tools.ha_get_system_health(include="repairs")  # caches the caps
    pooled = await websocket_client.get_websocket_client(
        url=component_core_client.base_url,
        token=component_core_client.token,
        verify_ssl=component_core_client.verify_ssl,
    )
    monkeypatch.setattr(
        pooled,
        "send_command",
        AsyncMock(side_effect=HomeAssistantConnectionError("connection lost")),
    )

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_get_system_health(include="repairs")

    assert _error(exc_info)["code"] == "CONNECTION_FAILED"
