"""A component search that fails is not credited with the composed name.

When a current component's search errors, the legacy path answers and its
records carry no ``name``. A caller who asked for that field still has to be
told why it is missing.
"""

import pytest

from ha_mcp.client.rest_client import HomeAssistantCommandError
from ha_mcp.tools.search import component as search_component

from ._component_routing_helpers import make_ws, patch_ws
from .test_ha_search_component_routing import (
    _CAPS_SEARCH,
    RoutingClient,
    _build_ha_search,
    _setup_visibility_disabled,
)


@pytest.mark.asyncio
async def test_a_requested_name_is_explained_when_a_current_component_fails(
    tmp_path, monkeypatch
) -> None:
    _setup_visibility_disabled(tmp_path, monkeypatch)
    ws = make_ws(
        "ha_mcp_tools/search",
        info_result=_CAPS_SEARCH,
        cmd_exc=HomeAssistantCommandError("Command failed: boom", "internal_error"),
    )
    ha_search = _build_ha_search(RoutingClient())

    with patch_ws(ws, search_component):
        resp = await ha_search(query="kitchen", result_fields=["entity_id", "name"])

    assert resp["entities"]
    assert all("name" not in row for row in resp["entities"])
    assert any("composed name" in w for w in resp["warnings"])
