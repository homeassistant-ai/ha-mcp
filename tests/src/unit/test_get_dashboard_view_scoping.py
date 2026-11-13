"""``ha_config_get_dashboard`` get-mode view_path scoping (issue #2010).

``view_path`` used to be a screenshot-only render selector: get mode accepted
it but returned the full multi-view config regardless, so a single-view read
on a large dashboard blew up the response. These tests pin the scoped
envelope: ``view`` + ``view_index`` instead of ``config`` (so the view object
can't be pushed back as a full-config replacement), ``config_hash`` still
covering the FULL config (python_transform optimistic locking unchanged),
structured errors for unknown/empty/strategy paths, and the explicit
ignored-parameter warning in list/search mode.

The default dashboard's config is saved in a real ``hass`` and read back by
the tool through Core's own ``lovelace/config``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp import config as ha_mcp_config
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import ToolResult
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.dashboard_screenshot import capture
from ha_mcp.tools.tools_config_dashboards import DashboardConfigTools
from ha_mcp.utils.config_hash import compute_config_hash

from ._addon_tool_helpers import _parse_tool_error

_CONFIG: dict[str, Any] = {
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


async def _save_default(
    hass: HomeAssistant, client: HomeAssistantClient, config: dict[str, Any] | str
) -> None:
    """Save ``config`` as the default (storage) dashboard in Core."""
    assert await async_setup_component(hass, "lovelace", {})
    saved = await client.send_websocket_message(
        {"type": "lovelace/config/save", "config": config}
    )
    assert saved["success"], saved


async def _get_tool(hass: HomeAssistant, client: HomeAssistantClient) -> Any:
    await _save_default(hass, client, _CONFIG)
    return DashboardConfigTools(client).ha_config_get_dashboard


async def test_view_path_scopes_response_to_single_view(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    tool = await _get_tool(hass, core_client)
    full = await tool(url_path="default")

    result = await tool(url_path="default", view_path="office")

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
    assert result["config_hash"] == full["config_hash"]
    assert result["config_hash"] == compute_config_hash(_CONFIG)
    assert result["config_size_bytes"] == full["config_size_bytes"]
    assert result["view_size_bytes"] < result["config_size_bytes"]
    assert "config['views'][1]" in result["hint"]


async def test_scoped_render_paths_limited_to_matched_view(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    tool = await _get_tool(hass, core_client)

    result = await tool(url_path="default", view_path="office")

    assert [row["view_index"] for row in result["render_paths"]] == [1]
    assert result["render_paths"][0]["view_path"] == "office"


async def test_scoped_get_without_screenshot_emits_no_ignored_warning(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """view_path is a first-class get-mode parameter now — consuming it for
    scoping must not trip the 'screenshot render options are ignored' warning."""
    tool = await _get_tool(hass, core_client)

    result = await tool(url_path="default", view_path="office")

    for warning in result.get("warnings", []):
        assert "ignored" not in warning, warning


async def test_unknown_view_path_errors_and_lists_available(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    tool = await _get_tool(hass, core_client)

    with pytest.raises(ToolError) as exc_info:
        await tool(url_path="default", view_path="garage")

    error = _parse_tool_error(exc_info)
    assert error["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert error["available_view_paths"] == ["home", "office"]


async def test_empty_view_path_errors(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    tool = await _get_tool(hass, core_client)

    with pytest.raises(ToolError) as exc_info:
        await tool(url_path="default", view_path="   ")

    error = _parse_tool_error(exc_info)
    assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"


async def test_strategy_dashboard_view_path_errors_with_get_suggestion(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await _save_default(hass, core_client, {"strategy": {"type": "original-states"}})
    tool = DashboardConfigTools(core_client).ha_config_get_dashboard

    with pytest.raises(ToolError) as exc_info:
        await tool(url_path="default", view_path="home")

    error = _parse_tool_error(exc_info)
    assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "Omit view_path" in error["error"]["suggestion"]


async def test_non_dict_config_with_view_path_errors(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Core stores a string config as given; it cannot be scoped — structured
    error, not an AttributeError from walking it."""
    await _save_default(hass, core_client, "views: []")
    tool = DashboardConfigTools(core_client).ha_config_get_dashboard

    with pytest.raises(ToolError) as exc_info:
        await tool(url_path="default", view_path="office")

    error = _parse_tool_error(exc_info)
    assert error["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert "view_path cannot be resolved" in error["error"]["message"]


@pytest.mark.parametrize("include_screenshot", [True, False])
async def test_screenshot_renders_the_scoped_view_only_when_requested(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    include_screenshot: bool,
) -> None:
    """view_path drives BOTH scoping and the render target: it selects the view
    to render when a screenshot is requested, and nothing renders when not."""
    monkeypatch.setattr(
        ha_mcp_config,
        "get_global_settings",
        lambda: SimpleNamespace(
            enable_dashboard_screenshot=True, enable_mandatory_bps=False
        ),
    )
    rendered: list[str] = []

    async def record(path: str, **kwargs: Any) -> list[Any]:
        rendered.append(path)
        return []

    # The screenshot engine is this repository's own collaborator, not Core.
    monkeypatch.setattr(capture, "capture_dashboard_images", record)
    tool = await _get_tool(hass, core_client)

    result = await tool(
        url_path="default", view_path="office", include_screenshot=include_screenshot
    )

    if include_screenshot:
        assert isinstance(result, ToolResult)
        structured = result.structured_content
        assert structured is not None
        assert structured["view"] == _CONFIG["views"][1]
        assert rendered == [structured["render_paths"][0]["render_path"]]
    else:
        assert result["view"] == _CONFIG["views"][1]
        assert rendered == []


async def test_full_get_unchanged_without_view_path(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    tool = await _get_tool(hass, core_client)

    result = await tool(url_path="default")

    assert result["config"] == _CONFIG
    assert "view" not in result
    assert "view_index" not in result


async def test_large_config_hint_mentions_view_path(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    big_config = {
        "views": [
            {
                "title": "Big",
                "path": "big",
                "cards": [{"type": "markdown", "content": "x" * 12000}],
            }
        ]
    }
    await _save_default(hass, core_client, big_config)
    tool = DashboardConfigTools(core_client).ha_config_get_dashboard

    result = await tool(url_path="default")

    assert "view_path" in result["hint"]


async def test_view_path_in_list_mode_warns_ignored(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    tool = await _get_tool(hass, core_client)

    result = await tool(list_only=True, view_path="office")

    assert result["success"] is True
    assert any("view_path" in w and "ignored" in w for w in result["warnings"])


async def test_view_path_in_search_mode_warns_ignored(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    tool = await _get_tool(hass, core_client)

    result = await tool(url_path="default", entity_id="light.desk", view_path="office")

    assert result["match_count"] == 1
    assert any("view_path" in w and "ignored" in w for w in result["warnings"])


async def test_view_path_in_unscoped_search_warns_ignored(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A search across dashboards warns about view_path instead of dropping it."""
    tool = await _get_tool(hass, core_client)

    result = await tool(query="light.desk", view_path="office")

    assert result["success"] is True
    assert result["action"] == "search"
    assert any("view_path" in w and "ignored" in w for w in result["warnings"])
