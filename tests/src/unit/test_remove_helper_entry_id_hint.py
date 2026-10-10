"""ha_remove_helpers_integrations guidance for an entry_id passed with helper_type."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools.tools_integrations import IntegrationTools


@pytest.mark.asyncio
async def test_flow_helper_entry_id_with_helper_type_error_says_to_omit_it():
    """An agent that passes a flow helper's entry_id with its helper_type is
    told to omit helper_type, not only to search for an entity_id.
    """
    client = MagicMock()
    client.send_websocket_message = AsyncMock()
    tools = IntegrationTools(client)

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_remove_helpers_integrations(
            target="01M443F764NKA7W4N2GG49YHS4",
            helper_type="utility_meter",
            confirm=True,
        )

    err = json.loads(str(exc_info.value))
    assert err["error"]["code"] == "ENTITY_NOT_FOUND"
    assert any("omit helper_type" in s for s in err["error"]["suggestions"])
