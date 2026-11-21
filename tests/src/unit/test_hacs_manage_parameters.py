"""``ha_manage_hacs``'s parameters: each action takes its own, a parameter of
another action is refused rather than dropped, and the action list, the
handlers, their parameters and the tool's signature agree.

No Marketplace is set up: every check here ends before Core, or at Core's
refusal of a command it does not have.
"""

from __future__ import annotations

import inspect
from typing import Any, get_args

import pytest

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient
from ha_mcp.tools.tools_hacs import HacsTools

from ._client_spies import sent_commands
from ._hacs_marketplace import pooled_socket
from ._tool_errors import rejected_call_body

# A value of the declared type for every parameter but action and ctx.
EVERY_PARAMETER: dict[str, Any] = {
    "repository_id": "1",
    "version": "v1.0.0",
    "repository": "owner/repo",
    "category": "plugin",
    "categories": ["plugin"],
    "show_beta": True,
    "confirm_replace_built_in": True,
    "confirm": True,
    "config": {},
    "flow_id": "flow",
    "abort": True,
}


@pytest.fixture
def tools(core_client: HomeAssistantClient) -> HacsTools:
    return HacsTools(core_client)


@pytest.fixture
async def socket(core_client: HomeAssistantClient) -> HomeAssistantWebSocketClient:
    return await pooled_socket(core_client)


@pytest.mark.parametrize(
    ("arguments", "foreign"),
    [
        # "Uninstall this version" is not something remove can do.
        ({"action": "remove", "repository_id": "1", "version": "v4.0.0"}, "version"),
        ({"action": "download", "repository_id": "1", "category": "theme"}, "category"),
        (
            {"action": "add_repository", "repository": "o/r", "repository_id": "1"},
            "repository_id",
        ),
        (
            {
                "action": "set_beta",
                "repository_id": "1",
                "show_beta": True,
                "confirm": True,
            },
            "confirm",
        ),
        ({"action": "connect_github", "repository": "o/r"}, "repository"),
        ({"action": "remove", "repository_id": "1", "abort": True}, "abort"),
    ],
)
async def test_a_parameter_of_another_action_is_refused_not_dropped(
    tools: HacsTools,
    socket: HomeAssistantWebSocketClient,
    arguments: dict[str, Any],
    foreign: str,
) -> None:
    with sent_commands(socket) as spy:
        body = await rejected_call_body(tools.ha_manage_hacs(**arguments))
    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert body["foreign_parameters"] == [foreign]
    spy.assert_not_awaited()


def _signature_actions() -> tuple[str, ...]:
    annotated = inspect.signature(HacsTools.ha_manage_hacs).parameters["action"]
    literal = get_args(annotated.annotation)[0]
    return get_args(literal)


@pytest.mark.parametrize("action", _signature_actions())
async def test_every_action_reaches_its_handler_with_its_own_parameters(
    tools: HacsTools, action: str
) -> None:
    """Every action the signature offers is handled, and the parameters it
    accepts are the tool's and its handler's: a drift between the action
    list, the handler table, the parameter sets and the actions that get the
    REST client fails here as an internal error instead of in a caller's
    call."""
    declared = set(inspect.signature(HacsTools.ha_manage_hacs).parameters)
    assert declared - {"self", "action", "ctx"} == set(EVERY_PARAMETER)

    mixed = await rejected_call_body(
        tools.ha_manage_hacs(action=action, **EVERY_PARAMETER)
    )
    assert mixed["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    own = {
        name: value
        for name, value in EVERY_PARAMETER.items()
        if name not in mixed["foreign_parameters"]
    }
    handled = await rejected_call_body(tools.ha_manage_hacs(action=action, **own))

    assert handled["error"]["code"] != "INTERNAL_ERROR"
