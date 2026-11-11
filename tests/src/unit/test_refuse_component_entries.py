"""The generic integration tools refuse ha-mcp's own ``ha_mcp_tools`` entries.

Reaching them through ``ha_set_integration`` or ``ha_remove_helpers_integrations``
would bypass the HA UI and the ``ha_dev_*`` tools, and a write alters the very
server answering the call.

The tools run on the server's own client against a real ``hass`` that holds
the entry.
"""

import json
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import Mock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_integrations import IntegrationTools

_OWN_ENTRY_ID = "own-server-entry"

# The only command a refused call may send: the entry lookup that finds the
# domain to refuse.
_READ_ONLY_COMMANDS = {"config_entries/get_single"}


@pytest.fixture
async def own_entry(hass: HomeAssistant) -> MockConfigEntry:
    """The server's own entry, in a ``hass`` that answers config-entry reads."""
    assert await async_setup_component(hass, "config", {})
    entry = MockConfigEntry(domain="ha_mcp_tools", entry_id=_OWN_ENTRY_ID)
    entry.add_to_hass(hass)
    return entry


@contextmanager
def _writes(client: HomeAssistantClient) -> Iterator[dict[str, Mock]]:
    """Spies on every client call that would start changing an entry; each
    calls through, so a refused call must reach none of them."""
    with (
        patch.object(
            client, "start_config_flow", wraps=client.start_config_flow
        ) as start_config_flow,
        patch.object(
            client, "start_options_flow", wraps=client.start_options_flow
        ) as start_options_flow,
        patch.object(
            client, "start_reconfigure_flow", wraps=client.start_reconfigure_flow
        ) as start_reconfigure_flow,
        patch.object(
            client, "delete_config_entry", wraps=client.delete_config_entry
        ) as delete_config_entry,
        patch.object(client, "call_service", wraps=client.call_service) as call_service,
        patch.object(
            client, "send_websocket_message", wraps=client.send_websocket_message
        ) as send_websocket_message,
    ):
        yield {
            "start_config_flow": start_config_flow,
            "start_options_flow": start_options_flow,
            "start_reconfigure_flow": start_reconfigure_flow,
            "delete_config_entry": delete_config_entry,
            "call_service": call_service,
            "send_websocket_message": send_websocket_message,
        }


def _assert_nothing_written(hass: HomeAssistant, spies: dict[str, Mock]) -> None:
    sent = spies.pop("send_websocket_message")
    assert {call.args[0]["type"] for call in sent.call_args_list} <= (
        _READ_ONLY_COMMANDS
    )
    for spy in spies.values():
        spy.assert_not_called()
    # Core's own view: no flow was started on any entry.
    assert hass.config_entries.flow.async_progress() == []
    assert hass.config_entries.options.async_progress() == []


def _assert_refused_without_mutation(
    exc_info: pytest.ExceptionInfo[ToolError],
    hass: HomeAssistant,
    spies: dict[str, Mock],
) -> None:
    error = json.loads(str(exc_info.value))["error"]
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    # The refusal points the agent at the paths that own these entries.
    assert any("ha_dev_manage_settings" in s for s in error["suggestions"])
    _assert_nothing_written(hass, spies)
    entry = hass.config_entries.async_get_entry(_OWN_ENTRY_ID)
    assert entry is not None
    assert entry.disabled_by is None
    assert dict(entry.options) == {}


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"entry_id": _OWN_ENTRY_ID, "config": {"x": 1}}, id="options"),
        pytest.param(
            {
                "entry_id": _OWN_ENTRY_ID,
                "reconfigure": True,
                "config": {"host": "192.0.2.1"},
            },
            id="reconfigure",
        ),
        pytest.param({"entry_id": _OWN_ENTRY_ID, "enabled": False}, id="disable"),
        pytest.param(
            {"entry_id": _OWN_ENTRY_ID, "log_level": "DEBUG"}, id="log-level-entry"
        ),
        pytest.param(
            {"domain": "ha_mcp_tools", "log_level": "DEBUG"}, id="log-level-domain"
        ),
        # Core's integration lookup is case-sensitive today; normalising the
        # domain is defence in depth, so a mixed-case one is still refused.
        pytest.param({"domain": "HA_MCP_TOOLS"}, id="add-mixed-case"),
    ],
)
async def test_set_integration_cannot_alter_ha_mcps_own_entries(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    own_entry: MockConfigEntry,
    kwargs: dict[str, Any],
) -> None:
    """An agent cannot edit, disable, re-log or add the server's own entries
    through ha_set_integration, bypassing the HA UI and developer tools."""
    with _writes(core_client) as spies, pytest.raises(ToolError) as exc_info:
        await IntegrationTools(core_client).ha_set_integration(**kwargs)

    _assert_refused_without_mutation(exc_info, hass, spies)


async def test_remove_cannot_delete_ha_mcps_own_entry(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    own_entry: MockConfigEntry,
) -> None:
    """An agent cannot delete the server's own config entry by its entry_id."""
    with _writes(core_client) as spies, pytest.raises(ToolError) as exc_info:
        await IntegrationTools(core_client).ha_remove_helpers_integrations(
            target=_OWN_ENTRY_ID, confirm=True
        )

    _assert_refused_without_mutation(exc_info, hass, spies)


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(
            lambda client: IntegrationTools(client).ha_remove_helpers_integrations(
                target="ghost-entry", confirm=True
            ),
            id="delete",
        ),
        pytest.param(
            lambda client: IntegrationTools(client).ha_set_integration(
                entry_id="ghost-entry", enabled=False
            ),
            id="disable",
        ),
    ],
)
async def test_an_unknown_entry_is_reported_missing_before_any_write(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    own_entry: MockConfigEntry,
    call: Callable[[HomeAssistantClient], Awaitable[Any]],
) -> None:
    """The own-entry lookup now runs first, so a typo'd entry_id must still
    come back as not found, with nothing sent to Home Assistant."""
    with _writes(core_client) as spies, pytest.raises(ToolError) as exc_info:
        await call(core_client)

    assert json.loads(str(exc_info.value))["error"]["code"] == "RESOURCE_NOT_FOUND"
    _assert_nothing_written(hass, spies)
