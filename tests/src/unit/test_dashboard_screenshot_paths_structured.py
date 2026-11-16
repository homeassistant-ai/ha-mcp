"""Screenshot targets addressed by url_path and view_path, resolved against
the dashboard config Core holds."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.dashboard_screenshot.paths import (
    DashboardRenderTarget,
    resolve_dashboard_render_target,
)

from ._dashboard_screenshot_paths_core import (
    broken_default_dashboard,
    named_target,
    save_dashboard,
)
from ._tool_errors import error_body, error_of


@pytest.mark.parametrize(
    ("url_path", "stored_as", "base_path"),
    [
        ("default", None, "lovelace"),
        ("wall-panel", "wall-panel", "wall-panel"),
    ],
)
async def test_structured_resolution_returns_canonical_named_view(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    sent: AsyncMock,
    url_path: str,
    stored_as: str | None,
    base_path: str,
) -> None:
    await save_dashboard(
        hass,
        core_client,
        {
            "views": [
                {"title": "Home", "path": "home"},
                {"title": "Lights", "path": "lights"},
            ]
        },
        url_path=stored_as,
    )
    sent.reset_mock()

    target = await named_target(core_client, url_path, "lights")

    assert target == DashboardRenderTarget(
        url_path=base_path,
        view_path="lights",
        render_path=f"{base_path}/lights",
        view_index=1,
        stable=True,
    )
    assert [call.args[0]["type"] for call in sent.await_args_list] == [
        "lovelace/config"
    ]


async def test_structured_resolution_reports_a_lost_connection(
    core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        core_client,
        "send_websocket_message",
        AsyncMock(side_effect=HomeAssistantConnectionError("disconnected mid-read")),
    )

    with pytest.raises(ToolError) as exc_info:
        await named_target(core_client, "wall-panel", "home")

    error = error_body(exc_info)
    assert error["error"]["code"] == "CONNECTION_FAILED"
    assert "disconnected" in error["error"]["details"]


async def test_unknown_dashboard_is_reported_not_found(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    assert await async_setup_component(hass, "lovelace", {})

    with pytest.raises(ToolError) as exc_info:
        await named_target(core_client, "wall-panel", "home")

    assert error_of(exc_info)["code"] == "RESOURCE_NOT_FOUND"


async def test_dashboard_core_cannot_load_is_not_reported_missing(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await broken_default_dashboard(hass)

    with pytest.raises(ToolError) as exc_info:
        await named_target(core_client, "lovelace", "home")

    assert error_of(exc_info)["code"] == "SERVICE_CALL_FAILED"


@pytest.mark.parametrize(
    ("views", "reason", "available"),
    [
        ([{"path": "home"}], "not found", ["home"]),
        ([{"path": "lights"}, {"path": "lights"}], "ambiguous", ["lights", "lights"]),
    ],
)
async def test_named_view_resolution_reports_missing_and_ambiguous_paths(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    views: list[dict[str, Any]],
    reason: str,
    available: list[str],
) -> None:
    await save_dashboard(hass, core_client, {"views": views})

    with pytest.raises(ToolError) as exc_info:
        await named_target(core_client, "wall-panel", "lights")

    error = error_body(exc_info)
    assert error["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert reason in error["error"]["message"]
    assert error["url_path"] == "wall-panel"
    assert error["view_path"] == "lights"
    assert error["available_view_paths"] == available


async def test_named_view_resolution_rejects_strategy_dashboard(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A strategy dashboard generates its views at runtime, so a named
    view_path can never resolve."""
    await save_dashboard(hass, core_client, {"strategy": {"type": "original-states"}})

    with pytest.raises(ToolError) as exc_info:
        await named_target(core_client, "wall-panel", "some-view")

    error = error_body(exc_info)
    assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "do not expose static named view paths" in error["error"]["message"]


@pytest.mark.parametrize(
    ("config", "warning_text"),
    [
        ({"views": [{"path": "home"}]}, "first view visible"),
        ({"strategy": {"type": "original-states"}}, "runtime"),
        ({"views": []}, "no static views"),
    ],
)
async def test_base_route_metadata_does_not_invent_a_view_index(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    config: dict[str, Any],
    warning_text: str,
) -> None:
    await save_dashboard(hass, core_client, config)

    target = await named_target(core_client, "wall-panel", None)

    assert target.render_path == "wall-panel"
    assert target.view_index is None
    assert target.stable is False
    assert warning_text in target.warnings[0]


@pytest.mark.parametrize(
    ("dashboard_path", "url_path", "view_path", "expected_code"),
    [
        ("lovelace/0", "wall-panel", None, "VALIDATION_INVALID_PARAMETER"),
        (None, None, "lights", "VALIDATION_MISSING_PARAMETER"),
    ],
)
async def test_raw_and_structured_addressing_are_mutually_exclusive(
    core_client: HomeAssistantClient,
    sent: AsyncMock,
    dashboard_path: str | None,
    url_path: str | None,
    view_path: str | None,
    expected_code: str,
) -> None:
    with pytest.raises(ToolError) as exc_info:
        await resolve_dashboard_render_target(
            core_client,
            dashboard_path=dashboard_path,
            url_path=url_path,
            view_path=view_path,
        )

    assert error_of(exc_info)["code"] == expected_code
    sent.assert_not_awaited()


async def test_structured_empty_dashboard_path_is_rejected_without_ws(
    core_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    with pytest.raises(ToolError) as exc_info:
        await named_target(core_client, "   ", "home")

    assert error_of(exc_info)["code"] == "VALIDATION_INVALID_PARAMETER"
    sent.assert_not_awaited()


@pytest.mark.parametrize("configured_path", ["1e0", "0x1"])
async def test_numeric_coercible_named_path_uses_verified_index_fallback(
    hass: HomeAssistant, core_client: HomeAssistantClient, configured_path: str
) -> None:
    """The frontend reads a number-like path as a view index."""
    await save_dashboard(
        hass,
        core_client,
        {"views": [{"path": "first"}, {"path": "second"}, {"path": configured_path}]},
    )

    target = await named_target(core_client, "wall-panel", configured_path)

    assert target.view_index == 2
    assert target.render_path == "wall-panel/2"
    assert target.stable is False
    assert "numeric fallback" in target.warnings[0]


async def test_numeric_leading_non_number_path_remains_stable(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await save_dashboard(hass, core_client, {"views": [{"path": "1abc"}]})

    target = await named_target(core_client, "wall-panel", "1abc")

    assert target.render_path == "wall-panel/1abc"
    assert target.stable is True


async def test_exact_whitespace_view_path_resolves_to_numeric_fallback(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await save_dashboard(hass, core_client, {"views": [{"path": " home "}]})

    target = await named_target(core_client, "wall-panel", " home ")

    assert target.view_path == " home "
    assert target.render_path == "wall-panel/0"
    assert target.stable is False


async def test_resolved_path_is_left_for_capture_to_encode(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Capture percent-encodes the render path; one encoded here as well would
    reach the engine encoded twice."""
    await save_dashboard(
        hass, core_client, {"views": [{"title": "Home", "path": "my view"}]}
    )

    target = await named_target(core_client, "wall-panel", "my view")

    assert target.render_path == "wall-panel/my view"


async def test_structured_addressing_rejects_unsafe_dashboard_before_fetch(
    core_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    with pytest.raises(ToolError) as exc_info:
        await named_target(core_client, "https://evil.example/dashboard", "home")

    assert error_of(exc_info)["code"] == "VALIDATION_INVALID_PARAMETER"
    sent.assert_not_awaited()


async def test_structured_addressing_falls_back_for_unsafe_named_view(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await save_dashboard(hass, core_client, {"views": [{"path": "home?redirect=evil"}]})

    target = await named_target(core_client, "wall-panel", "home?redirect=evil")

    assert target.render_path == "wall-panel/0"
    assert target.stable is False
    assert "unsafe" in target.warnings[0]
