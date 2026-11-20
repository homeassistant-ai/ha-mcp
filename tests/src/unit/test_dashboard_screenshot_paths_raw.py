"""Screenshot targets addressed by a raw frontend route (dashboard_path),
verified against the dashboard config Core holds."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.dashboard_screenshot.paths import DashboardRenderTarget

from ._dashboard_screenshot_paths_core import (
    broken_default_dashboard,
    raw_target,
    save_dashboard,
)
from ._tool_errors import error_body, error_of


async def test_raw_numeric_path_warns_with_canonical_stable_view_path(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await save_dashboard(
        hass,
        core_client,
        {"views": [{"path": "home"}, {"title": "Lights", "path": "lights"}]},
    )

    target = await raw_target(core_client, "wall-panel/1")

    assert target == DashboardRenderTarget(
        url_path="wall-panel",
        view_path=None,
        render_path="wall-panel/1",
        view_index=1,
        stable=False,
        warnings=(
            "Numeric view index 'wall-panel/1' is fragile; use the stable render "
            "path 'wall-panel/lights' or url_path/view_path addressing.",
        ),
    )


async def test_raw_js_numeric_route_warns_with_canonical_stable_view_path(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await save_dashboard(
        hass, core_client, {"views": [{"path": "home"}, {"path": "lights"}]}
    )

    target = await raw_target(core_client, "wall-panel/1e0")

    assert target.view_index == 1
    assert "wall-panel/lights" in target.warnings[0]


async def test_raw_numeric_path_drops_unverified_trailing_segment(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Only the view segment is checked against the config, so "/debug" must
    not reach the engine."""
    await save_dashboard(
        hass, core_client, {"views": [{"path": "home"}, {"path": "lights"}]}
    )

    target = await raw_target(core_client, "wall-panel/1/debug")

    assert target.render_path == "wall-panel/1"
    assert target.view_index == 1


async def test_legacy_lovelace_route_reports_a_lost_connection(
    core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lost connection is not the default dashboard having no config."""
    monkeypatch.setattr(
        core_client,
        "send_websocket_message",
        AsyncMock(side_effect=HomeAssistantConnectionError("Connection lost")),
    )

    with pytest.raises(ToolError) as exc_info:
        await raw_target(core_client, "lovelace/0")

    error = error_body(exc_info)
    assert error["error"]["code"] == "CONNECTION_FAILED"
    assert "Connection lost" in error["error"]["details"]


async def test_raw_default_numeric_path_uses_stored_stable_alias(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await save_dashboard(
        hass, core_client, {"views": [{"title": "Home", "path": "home"}]}, url_path=None
    )

    target = await raw_target(core_client, "lovelace/0")

    assert target.view_index == 0
    assert target.stable is False
    assert "lovelace/home" in target.warnings[0]


async def test_raw_default_path_allows_known_no_config_fallback(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    assert await async_setup_component(hass, "lovelace", {})

    target = await raw_target(core_client, "lovelace/0")

    assert target.render_path == "lovelace/0"
    assert target.view_index == 0
    assert target.stable is False
    assert "cannot be verified" in target.warnings[0]


async def test_raw_default_path_does_not_hide_a_load_failure(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await broken_default_dashboard(hass)

    with pytest.raises(ToolError) as exc_info:
        await raw_target(core_client, "lovelace/0")

    assert error_of(exc_info)["code"] == "SERVICE_CALL_FAILED"


async def test_raw_default_path_does_not_hide_a_config_that_is_not_a_mapping(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await save_dashboard(hass, core_client, "invalid", url_path=None)

    with pytest.raises(ToolError) as exc_info:
        await raw_target(core_client, "lovelace/0")

    error = error_body(exc_info)
    assert error["error"]["code"] == "SERVICE_CALL_FAILED"
    assert error["payload_type"] == "str"


@pytest.mark.parametrize("raw_view", ["missing", "99"])
async def test_raw_custom_suffix_must_resolve_to_a_real_view(
    hass: HomeAssistant, core_client: HomeAssistantClient, raw_view: str
) -> None:
    await save_dashboard(hass, core_client, {"views": [{"path": "home"}]})

    with pytest.raises(ToolError) as exc_info:
        await raw_target(core_client, f"wall-panel/{raw_view}")

    assert error_of(exc_info)["code"] == "RESOURCE_NOT_FOUND"


async def test_raw_duplicate_named_suffix_selects_first_but_is_unstable(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await save_dashboard(
        hass, core_client, {"views": [{"path": "home"}, {"path": "home"}]}
    )

    target = await raw_target(core_client, "wall-panel/home")

    assert target.view_index == 0
    assert target.render_path == "wall-panel/home"
    assert target.stable is False
    assert "first matching" in target.warnings[0]


async def test_raw_strategy_suffix_is_unverified_not_stable(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await save_dashboard(hass, core_client, {"strategy": {"type": "original-states"}})

    target = await raw_target(core_client, "wall-panel/runtime/view")

    assert target.render_path == "wall-panel/runtime/view"
    assert target.stable is False
    assert "cannot be verified" in target.warnings[0]


async def test_raw_multi_segment_cannot_select_slash_configured_path(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await save_dashboard(hass, core_client, {"views": [{"path": "floor/second"}]})

    with pytest.raises(ToolError) as exc_info:
        await raw_target(core_client, "wall-panel/floor/second")

    assert error_of(exc_info)["code"] == "RESOURCE_NOT_FOUND"


async def test_raw_multi_segment_uses_only_frontend_view_suffix(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await save_dashboard(hass, core_client, {"views": [{"path": "floor"}]})

    target = await raw_target(core_client, "wall-panel/floor/second")

    assert target.view_path == "floor"
    assert target.render_path == "wall-panel/floor"
    assert target.stable is True


async def test_duplicate_numeric_warning_does_not_recommend_ambiguous_alias(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await save_dashboard(
        hass, core_client, {"views": [{"path": "home"}, {"path": "home"}]}
    )

    target = await raw_target(core_client, "wall-panel/1")

    assert target.render_path == "wall-panel/1"
    assert "wall-panel/home" not in target.warnings[0]
    assert "unique views[].path" in target.warnings[0]


async def test_numeric_warning_does_not_recommend_normalization_changed_alias(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await save_dashboard(hass, core_client, {"views": [{"path": "."}]})

    target = await raw_target(core_client, "wall-panel/0")

    assert target.view_index == 0
    assert "stable render path 'wall-panel'" not in target.warnings[0]
    assert "unique views[].path" in target.warnings[0]


async def test_raw_exact_numeric_path_precedes_index_match(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """The frontend selects a view whose path is "1" before view index 1."""
    await save_dashboard(
        hass, core_client, {"views": [{"path": "1"}, {"path": "second"}]}
    )

    target = await raw_target(core_client, "wall-panel/1")

    assert target.view_index == 0
    assert target.stable is False
    assert "wall-panel/second" not in target.warnings[0]


async def test_raw_reserved_view_suffix_is_rejected(
    core_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    with pytest.raises(ToolError) as exc_info:
        await raw_target(core_client, "wall-panel/hass-unused-entities")

    assert error_of(exc_info)["code"] == "VALIDATION_INVALID_PARAMETER"
    sent.assert_not_awaited()


async def test_raw_dashboard_base_is_reported_as_unstable(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await save_dashboard(
        hass, core_client, {"views": [{"title": "Home", "path": "home"}]}
    )

    target = await raw_target(core_client, "wall-panel")

    assert target.url_path == "wall-panel"
    assert target.view_path is None
    assert target.view_index is None
    assert target.stable is False
    assert any("first view visible" in warning for warning in target.warnings)


async def test_raw_route_cannot_target_non_dashboard_frontend_panel(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """The "config" frontend panel is not a dashboard Core holds a config for."""
    assert await async_setup_component(hass, "lovelace", {})

    with pytest.raises(ToolError) as exc_info:
        await raw_target(core_client, "config/integrations")

    error = error_body(exc_info)
    assert error["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert error["url_path"] == "config"


async def test_raw_addressing_rejects_unsafe_paths_before_fetch(
    core_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    with pytest.raises(ToolError) as exc_info:
        await raw_target(core_client, "lovelace/../config")

    assert error_of(exc_info)["code"] == "VALIDATION_INVALID_PARAMETER"
    sent.assert_not_awaited()
