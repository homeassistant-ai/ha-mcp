"""A component behind the server's release is refused, and works once updated.

Core sets up the File & YAML Tools entry, which reports the component's own
version. The server's floor is raised above it, as on a server one release
ahead, and put back, as after the component's update.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools import tools_filesystem

from ._file_services import make_file_service
from ._tool_errors import error_of


async def test_a_refused_component_works_again_once_it_is_updated(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every call is refused while the component is behind, and the refusal
    is not remembered: the update the error asks for is enough, with no
    restart of the server."""
    await make_file_service(hass)
    released = tools_filesystem.MIN_COMPONENT_VERSION
    ahead = f"{int(released.split('.')[0]) + 1}.0.0"

    with monkeypatch.context() as server_ahead:
        server_ahead.setattr(tools_filesystem, "MIN_COMPONENT_VERSION", ahead)
        for _ in range(2):
            with pytest.raises(ToolError) as exc:
                await tools_filesystem.call_mcp_tools_service(
                    core_client, "list_files", {"path": "www"}
                )

    error = error_of(exc)
    assert error["code"] == "COMPONENT_NOT_INSTALLED"
    assert released in error["message"] and ahead in error["message"]

    listing = await tools_filesystem.call_mcp_tools_service(
        core_client, "list_files", {"path": "www"}
    )
    assert isinstance(listing, dict)
