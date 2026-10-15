"""The generic integration tools refuse ha-mcp's own ``ha_mcp_tools`` entries.

Those entries hold the server's own settings and credentials. Reaching them
through ``ha_set_integration`` or ``ha_remove_helpers_integrations`` would
bypass the HA UI and the developer tools that own those changes, and a write
reloads or alters the very server answering the call.
"""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools.tools_integrations import IntegrationTools

_OWN_ENTRY_ID = "own-server-entry"

# Every backend call that would start changing an entry; a refused call
# must reach none of them.
_MUTATING_CALLS = (
    "start_config_flow",
    "start_options_flow",
    "start_reconfigure_flow",
    "send_websocket_message",
    "delete_config_entry",
)


@pytest.fixture
def client() -> MagicMock:
    client = MagicMock()
    # supports_* set so a refusal cannot come from a capability check instead.
    client.get_config_entry = AsyncMock(
        return_value={
            "entry_id": _OWN_ENTRY_ID,
            "domain": "ha_mcp_tools",
            "supports_options": True,
            "supports_reconfigure": True,
        }
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
        pytest.param({"domain": "ha_mcp_tools"}, id="add"),
        # Core resolves a mixed-case domain, so the guard must not miss it.
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
