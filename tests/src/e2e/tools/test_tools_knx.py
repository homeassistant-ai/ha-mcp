"""
E2E smoke test for ha_knx_get_project.

The test container does not have the KNX integration configured, so
``knx/get_knx_project`` returns the upstream "KNX integration not loaded."
error. This test confirms the tool maps that to a clean COMPONENT_NOT_INSTALLED
ToolError over the real WebSocket plumbing — catching any command rename
(``knx/get_knx_project``) or error-serialisation divergence that the mocked
unit tests cannot. The success path (parsed group addresses) is covered by the
unit tests, which would otherwise require a live HA with an uploaded ETS
project.
"""

import logging

import pytest
from fastmcp.exceptions import ToolError

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
async def test_knx_get_project_without_integration_raises_clean_error(mcp_client):
    """Without the KNX integration loaded, the tool raises a clear
    COMPONENT_NOT_INSTALLED error rather than leaking a stacktrace."""
    with pytest.raises(ToolError) as exc_info:
        await mcp_client.call_tool("ha_knx_get_project", {})

    message = str(exc_info.value)
    assert "COMPONENT_NOT_INSTALLED" in message
    assert "KNX" in message
    logger.info("ha_knx_get_project surfaced a clean not-loaded error")
