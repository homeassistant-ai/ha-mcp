"""The generic integration tools refuse ha-mcp's own ``ha_mcp_tools`` entries.

Reaching them through ``ha_set_integration`` or ``ha_remove_helpers_integrations``
would bypass the HA UI and the ``ha_dev_*`` tools, and a write alters the very
server answering the call.
"""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantAPIError
from ha_mcp.tools.tools_integrations import IntegrationTools

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
