"""System log entries retain their integration and both measures of frequency."""

import logging
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_logs import LogTools

from ._log_tool_calls import call_kwargs, set_up_core_logs


async def test_repeated_warning_counts_as_one_row_and_two_occurrences(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    await set_up_core_logs(hass)
    for _ in range(2):
        logging.getLogger("homeassistant.components.logger.test").warning(
            "repeated warning"
        )
    with patch.object(
        component_core_client,
        "send_websocket_message",
        AsyncMock(wraps=component_core_client.send_websocket_message),
    ) as sent:
        result = await LogTools(component_core_client).get_logs(
            **call_kwargs(
                source="system", integration="logger", search="repeated warning"
            )
        )
    assert result["entries"][0]["integrations"] == ["logger"]
    assert result["integration_counts"]["logger"] == {"rows": 1, "occurrences": 2}
    assert not result.get("warnings")
    assert all(
        call.args[0]["type"] != "system_log/list" for call in sent.await_args_list
    )
