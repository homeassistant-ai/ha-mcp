"""The generic integration tools refuse ha-mcp's own ``ha_mcp_tools`` entries.

Reaching them through ``ha_set_integration``, ``ha_remove_helpers_integrations``,
``ha_reload_core`` or ``ha_call_service`` would bypass the HA UI and the
``ha_dev_*`` tools, and a write reloads or alters the very server answering
the call.
"""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.tools_integrations import IntegrationTools
from ha_mcp.tools.tools_service import ServiceTools
from ha_mcp.tools.tools_system import SystemTools

_OWN_ENTRY_ID = "own-server-entry"
_OWN_ENTRY = {
    "entry_id": _OWN_ENTRY_ID,
    "domain": "ha_mcp_tools",
    "supports_options": True,
    "supports_reconfigure": True,
}

# Every backend call that would start changing an entry; a refused call
# must reach none of them.
_MUTATING_CALLS = (
    "start_config_flow",
    "start_options_flow",
    "start_reconfigure_flow",
    "send_websocket_message",
    "delete_config_entry",
    "call_service",
    "_request",
)


@pytest.fixture
def client() -> MagicMock:
    client = MagicMock()
    # supports_* set so a refusal cannot come from a capability check instead.
    client.get_config_entry = AsyncMock(return_value=dict(_OWN_ENTRY))
    # The component's update entity and its device belong to the server entry;
    # a light in another room belongs to another integration.
    client.list_config_entries = AsyncMock(
        return_value=[dict(_OWN_ENTRY), {"entry_id": "hue-entry", "domain": "hue"}]
    )
    client.list_entity_registry = AsyncMock(
        return_value=[
            {
                "entity_id": "update.ha_mcp_server",
                "config_entry_id": _OWN_ENTRY_ID,
                "device_id": "own-device",
                "area_id": None,
            },
            {
                "entity_id": "light.lamp",
                "config_entry_id": "hue-entry",
                "device_id": "hue-device",
                "area_id": None,
            },
        ]
    )
    client.list_device_registry = AsyncMock(
        return_value=[
            {
                "id": "own-device",
                "config_entries": [_OWN_ENTRY_ID],
                "area_id": "office",
            },
            {"id": "hue-device", "config_entries": ["hue-entry"], "area_id": "kitchen"},
        ]
    )
    for name in _MUTATING_CALLS:
        setattr(client, name, AsyncMock())
    return client


def _assert_refused_without_mutation(
    exc_info: pytest.ExceptionInfo[ToolError], client: MagicMock
) -> None:
    error = json.loads(str(exc_info.value))["error"]
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    # The refusal points the agent at the paths that own these entries.
    assert any("ha_dev_manage_settings" in s for s in error["suggestions"])
    for name in _MUTATING_CALLS:
        getattr(client, name).assert_not_called()


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
    client: MagicMock, kwargs: dict[str, Any]
) -> None:
    """An agent cannot edit, disable, re-log or add the server's own entries
    through ha_set_integration, bypassing the HA UI and developer tools."""
    with pytest.raises(ToolError) as exc_info:
        await IntegrationTools(client).ha_set_integration(**kwargs)

    _assert_refused_without_mutation(exc_info, client)


async def test_remove_cannot_delete_ha_mcps_own_entry(client: MagicMock) -> None:
    """An agent cannot delete the server's own config entry by its entry_id."""
    with pytest.raises(ToolError) as exc_info:
        await IntegrationTools(client).ha_remove_helpers_integrations(
            target=_OWN_ENTRY_ID, confirm=True
        )

    _assert_refused_without_mutation(exc_info, client)


async def test_reload_core_cannot_reload_ha_mcps_own_entry(client: MagicMock) -> None:
    """ha_reload_core(entry_id=...) must not reload the server's own entry
    over the unshielded REST route, which strands it until HA restarts."""
    with pytest.raises(ToolError) as exc_info:
        await SystemTools(client).ha_reload_core(entry_id=_OWN_ENTRY_ID)

    _assert_refused_without_mutation(exc_info, client)


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"data": {"entry_id": _OWN_ENTRY_ID}}, id="entry-id"),
        # ha_call_service's own entity_id argument lands in the service data.
        pytest.param({"entity_id": "update.ha_mcp_server"}, id="entity"),
        pytest.param({"data": {"device_id": ["own-device"]}}, id="device"),
        pytest.param({"data": {"area_id": "office"}}, id="area"),
    ],
)
async def test_call_service_cannot_reload_ha_mcps_own_entry(
    client: MagicMock, kwargs: dict[str, Any]
) -> None:
    """homeassistant.reload_config_entry through ha_call_service is the same
    bypass, whether it names the entry or a target Core resolves to it, and
    must be refused before the service call."""
    with pytest.raises(ToolError) as exc_info:
        await ServiceTools(client, MagicMock()).ha_call_service(
            domain="homeassistant", service="reload_config_entry", **kwargs
        )

    _assert_refused_without_mutation(exc_info, client)


async def test_call_service_still_reloads_another_integrations_entry(
    client: MagicMock,
) -> None:
    """Targets that resolve to another integration's entry are not refused."""
    client.call_service.return_value = []

    result = await ServiceTools(client, MagicMock()).ha_call_service(
        domain="homeassistant",
        service="reload_config_entry",
        data={"area_id": "kitchen"},
    )

    assert result["success"] is True
    client.call_service.assert_awaited_once()


async def test_a_failed_reload_lookup_is_an_error_not_a_dispatched_call(
    client: MagicMock,
) -> None:
    """A timed-out own-entry lookup happens before anything is sent, so it
    must not be reported as a service call that was dispatched."""
    timeout = HomeAssistantConnectionError("Request timed out")
    timeout.__cause__ = httpx.ReadTimeout("timed out")
    client.list_config_entries.side_effect = timeout

    with pytest.raises(ToolError) as exc_info:
        await ServiceTools(client, MagicMock()).ha_call_service(
            domain="homeassistant",
            service="reload_config_entry",
            data={"entry_id": _OWN_ENTRY_ID},
        )

    assert json.loads(str(exc_info.value))["success"] is False
    client.call_service.assert_not_called()


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
        pytest.param(
            lambda client: SystemTools(client).ha_reload_core(entry_id="ghost-entry"),
            id="reload",
        ),
    ],
)
async def test_an_unknown_entry_is_reported_missing_before_any_write(
    client: MagicMock, call: Any
) -> None:
    """The own-entry lookup now runs first, so a typo'd entry_id must still
    come back as not found, with nothing sent to Home Assistant."""
    client.get_config_entry.side_effect = HomeAssistantAPIError(
        "Config entry not found: ghost-entry", status_code=404
    )
    with pytest.raises(ToolError) as exc_info:
        await call(client)

    assert json.loads(str(exc_info.value))["error"]["code"] == "RESOURCE_NOT_FOUND"
    for name in _MUTATING_CALLS:
        getattr(client, name).assert_not_called()
