"""Unit tests for ha_config_get_dashboard get-mode view_path scoping (issue #2010).

``view_path`` used to be a screenshot-only render selector: get mode accepted
it but returned the full multi-view config regardless, so a single-view read
on a large dashboard blew up the response. These tests pin the scoped
envelope: ``view`` + ``view_index`` instead of ``config`` (so the view object
can't be pushed back as a full-config replacement), ``config_hash`` still
covering the FULL config (python_transform optimistic locking unchanged),
structured errors for unknown/empty/strategy paths, and the explicit
ignored-parameter warning in list/search mode. Scoping is pure config
walking — no screenshot beta feature involved.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_config_dashboards import DashboardConfigTools
from ha_mcp.utils.config_hash import compute_config_hash

_CONFIG = {
    "title": "Multi",
    "views": [
        {"title": "Home", "path": "home", "cards": [{"type": "tile", "entity": "a.b"}]},
        {
            "title": "Office",
            "path": "office",
            "cards": [
                {"type": "tile", "entity": "light.desk"},
                {"type": "button", "entity": "switch.fan"},
            ],
        },
        {"title": "Pathless", "cards": []},
    ],
}


@pytest.fixture
def mock_client():
    client = MagicMock()
    # url_path="default" sends a single lovelace/config read (no lazy-resolve
    # retry); the component fast path falls back to this legacy read in the
    # unit environment (no websocket client), matching the sibling test files.
    client.send_websocket_message = AsyncMock(return_value={"result": _CONFIG})
    return client


@pytest.fixture
def get_dashboard_tool(mock_client):
    return DashboardConfigTools(mock_client).ha_config_get_dashboard


@pytest.mark.asyncio
async def test_view_path_scopes_response_to_single_view(get_dashboard_tool):
    result = await get_dashboard_tool(url_path="default", view_path="office")

    assert result["success"] is True
    assert result["action"] == "get"
    assert result["view"] == _CONFIG["views"][1]
    assert result["view_path"] == "office"
    assert result["view_index"] == 1
    assert result["view_count"] == 3
    # The scoped envelope must NOT carry a config key: the view object must
    # not be mistakable for a full config and pushed back through
    # ha_config_set_dashboard(config=...), which would drop the other views.
    assert "config" not in result
    # Optimistic locking stays anchored to the FULL config.
    assert result["config_hash"] == compute_config_hash(_CONFIG)
    assert result["config_size_bytes"] == len(json.dumps(_CONFIG))
    assert result["view_size_bytes"] == len(json.dumps(_CONFIG["views"][1]))
    assert "config['views'][1]" in result["hint"]


@pytest.mark.asyncio
async def test_scoped_render_paths_limited_to_matched_view(get_dashboard_tool):
    result = await get_dashboard_tool(url_path="default", view_path="office")

    assert [row["view_index"] for row in result["render_paths"]] == [1]
    assert result["render_paths"][0]["view_path"] == "office"


@pytest.mark.asyncio
async def test_scoped_get_without_screenshot_emits_no_ignored_warning(
    get_dashboard_tool,
):
    """view_path is a first-class get-mode parameter now — consuming it for
    scoping must not trip the 'screenshot render options are ignored' warning."""
    result = await get_dashboard_tool(url_path="default", view_path="office")

    for warning in result.get("warnings", []):
        assert "ignored" not in warning, warning


@pytest.mark.asyncio
async def test_unknown_view_path_errors_and_lists_available(get_dashboard_tool):
    with pytest.raises(ToolError) as exc_info:
        await get_dashboard_tool(url_path="default", view_path="garage")

    error = json.loads(str(exc_info.value))
    assert error["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert error["available_view_paths"] == ["home", "office"]


@pytest.mark.asyncio
async def test_empty_view_path_errors(get_dashboard_tool):
    with pytest.raises(ToolError) as exc_info:
        await get_dashboard_tool(url_path="default", view_path="   ")

    error = json.loads(str(exc_info.value))
    assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"


@pytest.mark.asyncio
async def test_strategy_dashboard_view_path_errors_with_get_suggestion(mock_client):
    mock_client.send_websocket_message = AsyncMock(
        return_value={"result": {"strategy": {"type": "original-states"}}}
    )
    tool = DashboardConfigTools(mock_client).ha_config_get_dashboard

    with pytest.raises(ToolError) as exc_info:
        await tool(url_path="default", view_path="home")

    error = json.loads(str(exc_info.value))
    assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "Omit view_path" in error["error"]["suggestion"]


@pytest.mark.asyncio
async def test_full_get_unchanged_without_view_path(get_dashboard_tool):
    result = await get_dashboard_tool(url_path="default")

    assert result["config"] == _CONFIG
    assert "view" not in result
    assert "view_index" not in result


@pytest.mark.asyncio
async def test_large_config_hint_mentions_view_path(mock_client):
    big_config = {
        "views": [
            {
                "title": "Big",
                "path": "big",
                "cards": [{"type": "markdown", "content": "x" * 12000}],
            }
        ]
    }
    mock_client.send_websocket_message = AsyncMock(return_value={"result": big_config})
    tool = DashboardConfigTools(mock_client).ha_config_get_dashboard

    result = await tool(url_path="default")

    assert "view_path" in result["hint"]


@pytest.mark.asyncio
async def test_view_path_in_list_mode_warns_ignored(mock_client):
    mock_client.send_websocket_message = AsyncMock(return_value={"result": []})
    tool = DashboardConfigTools(mock_client).ha_config_get_dashboard

    result = await tool(list_only=True, view_path="office")

    assert result["success"] is True
    assert any("view_path" in w and "ignored" in w for w in result["warnings"])


@pytest.mark.asyncio
async def test_view_path_in_search_mode_warns_ignored(get_dashboard_tool):
    result = await get_dashboard_tool(
        url_path="default", entity_id="light.desk", view_path="office"
    )

    assert result["match_count"] == 1
    assert any("view_path" in w and "ignored" in w for w in result["warnings"])
