"""Screenshot targets for a dashboard Home Assistant holds no config for (#2632).

Core answers "No config found." for it and the frontend renders its generated
default view, so the screenshot renders that view instead of calling the
dashboard missing.
"""

from __future__ import annotations

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.dashboard_screenshot.paths import resolve_dashboard_render_target

from .test_dashboard_screenshot_paths import _FakeAsyncClient, _tool_error

_NO_CONFIG = {
    "success": False,
    "error_code": "config_not_found",
    "error": "Command failed: No config found.",
}


async def test_dashboard_without_config_renders_its_default_view() -> None:
    target = await resolve_dashboard_render_target(
        _FakeAsyncClient(_NO_CONFIG),
        dashboard_path=None,
        url_path="wall-panel",
        view_path=None,
    )

    assert target.render_path == "wall-panel"
    assert target.stable is False
    assert target.warnings


async def test_view_path_on_dashboard_without_config_is_refused() -> None:
    """There are no static views to resolve, so view_path is the caller's error."""
    with pytest.raises(ToolError) as exc_info:
        await resolve_dashboard_render_target(
            _FakeAsyncClient(_NO_CONFIG),
            dashboard_path=None,
            url_path="wall-panel",
            view_path="home",
        )

    assert _tool_error(exc_info)["error"]["code"] == "VALIDATION_INVALID_PARAMETER"


async def test_raw_route_of_dashboard_without_config_keeps_its_root() -> None:
    target = await resolve_dashboard_render_target(
        _FakeAsyncClient(_NO_CONFIG),
        dashboard_path="wall-panel/0",
        url_path=None,
        view_path=None,
    )

    assert target.url_path == "wall-panel"
    assert target.render_path == "wall-panel/0"
