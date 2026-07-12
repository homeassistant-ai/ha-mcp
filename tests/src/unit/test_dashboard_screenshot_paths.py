"""Unit tests for stable dashboard screenshot render-path resolution."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.dashboard_screenshot.paths import (
    DashboardRenderTarget,
    dashboard_render_paths,
    resolve_dashboard_render_target,
    resolve_dashboard_view,
)


class _FakeAsyncClient:
    """Small WebSocket client stand-in with ordered canned responses."""

    def __init__(self, *responses: Any) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    async def send_websocket_message(self, request: dict[str, Any]) -> Any:
        self.requests.append(deepcopy(request))
        if not self._responses:
            raise AssertionError(f"Unexpected WebSocket request: {request}")
        return deepcopy(self._responses.pop(0))


class _FailingAsyncClient:
    async def send_websocket_message(self, request: dict[str, Any]) -> Any:
        raise RuntimeError(f"disconnected while sending {request['type']}")


def _config_response(config: dict[str, Any]) -> dict[str, Any]:
    return {"success": True, "result": config}


def _tool_error(exc_info: pytest.ExceptionInfo[ToolError]) -> dict[str, Any]:
    return json.loads(str(exc_info.value))


@pytest.mark.parametrize(
    ("url_path", "base_path"),
    [
        (None, "lovelace"),
        ("default", "lovelace"),
        ("lovelace", "lovelace"),
        ("wall-panel", "wall-panel"),
    ],
)
def test_render_paths_return_canonical_metadata_without_mutating_config(
    url_path: str | None, base_path: str
) -> None:
    config = {
        "title": "House",
        "views": [
            {"title": "Home", "path": " home ", "cards": [{"type": "clock"}]},
            {"title": "Lights", "path": "lights"},
        ],
    }
    original = deepcopy(config)

    paths, warnings = dashboard_render_paths(url_path, config)

    assert paths == [
        {
            "dashboard_url_path": base_path,
            "view_index": 0,
            "view_path": "home",
            "title": "Home",
            "render_path": f"{base_path}/home",
            "stable": True,
        },
        {
            "dashboard_url_path": base_path,
            "view_index": 1,
            "view_path": "lights",
            "title": "Lights",
            "render_path": f"{base_path}/lights",
            "stable": True,
        },
    ]
    assert warnings == []
    assert config == original


def test_render_paths_report_numeric_fallbacks() -> None:
    config: dict[str, Any] = {
        "views": [
            {"title": "Named", "path": "home"},
            {"title": "Missing"},
            {"title": "Blank", "path": "   "},
            "invalid view shape",
        ]
    }

    paths, warnings = dashboard_render_paths("wall-panel", config)

    assert paths == [
        {
            "dashboard_url_path": "wall-panel",
            "view_index": 0,
            "view_path": "home",
            "title": "Named",
            "render_path": "wall-panel/home",
            "stable": True,
        },
        {
            "dashboard_url_path": "wall-panel",
            "view_index": 1,
            "view_path": None,
            "title": "Missing",
            "render_path": "wall-panel/1",
            "stable": False,
        },
        {
            "dashboard_url_path": "wall-panel",
            "view_index": 2,
            "view_path": None,
            "title": "Blank",
            "render_path": "wall-panel/2",
            "stable": False,
        },
        {
            "dashboard_url_path": "wall-panel",
            "view_index": 3,
            "view_path": None,
            "title": None,
            "render_path": "wall-panel/3",
            "stable": False,
        },
    ]
    assert warnings == [
        "3 dashboard view(s) have no usable stable path; their render paths "
        "use fragile numeric indexes."
    ]


def test_render_paths_use_numeric_fallback_for_unsafe_configured_view_path() -> None:
    paths, warnings = dashboard_render_paths(
        "wall-panel", {"views": [{"title": "Unsafe", "path": "../config"}]}
    )

    assert paths == [
        {
            "dashboard_url_path": "wall-panel",
            "view_index": 0,
            "view_path": "../config",
            "title": "Unsafe",
            "render_path": "wall-panel/0",
            "stable": False,
            "invalid_path": True,
        }
    ]
    assert warnings == [
        "1 dashboard view(s) have no usable stable path; their render paths "
        "use fragile numeric indexes."
    ]


def test_render_paths_use_numeric_fallback_for_duplicate_paths() -> None:
    paths, warnings = dashboard_render_paths(
        "wall-panel",
        {
            "views": [
                {"title": "First", "path": "home"},
                {"title": "Second", "path": "home"},
            ]
        },
    )

    assert [path["render_path"] for path in paths] == [
        "wall-panel/0",
        "wall-panel/1",
    ]
    assert all(path["stable"] is False for path in paths)
    assert all(path["ambiguous_path"] is True for path in paths)
    assert any("duplicate" in warning for warning in warnings)


def test_render_paths_describe_strategy_dashboard_base_route() -> None:
    config = {"strategy": {"type": "original-states"}}

    paths, warnings = dashboard_render_paths("wall-panel", config)

    assert paths == [
        {
            "dashboard_url_path": "wall-panel",
            "view_index": None,
            "view_path": None,
            "title": None,
            "render_path": "wall-panel",
            "stable": False,
            "strategy_dashboard": True,
        }
    ]
    assert warnings == [
        "Strategy dashboard views are generated at runtime; only the dashboard "
        "base render path is available."
    ]


@pytest.mark.parametrize(
    ("dashboard_url_path", "base_path", "expected_request"),
    [
        (
            "default",
            "lovelace",
            {"type": "lovelace/config", "force": True},
        ),
        (
            "wall-panel",
            "wall-panel",
            {
                "type": "lovelace/config",
                "force": True,
                "url_path": "wall-panel",
            },
        ),
    ],
)
async def test_structured_resolution_returns_canonical_named_view(
    dashboard_url_path: str,
    base_path: str,
    expected_request: dict[str, Any],
) -> None:
    client = _FakeAsyncClient(
        _config_response(
            {
                "views": [
                    {"title": "Home", "path": "home"},
                    {"title": "Lights", "path": " lights "},
                ]
            }
        )
    )

    target = await resolve_dashboard_render_target(
        client,
        dashboard_path=None,
        dashboard_url_path=dashboard_url_path,
        view_path="lights",
    )

    assert target == DashboardRenderTarget(
        dashboard_url_path=base_path,
        view_path="lights",
        render_path=f"{base_path}/lights",
        view_index=1,
        stable=True,
    )
    assert client.requests == [expected_request]


async def test_structured_resolution_maps_transport_failure_to_tool_error() -> None:
    with pytest.raises(ToolError) as exc_info:
        await resolve_dashboard_render_target(
            _FailingAsyncClient(),
            dashboard_path=None,
            dashboard_url_path="wall-panel",
            view_path="home",
        )

    error = _tool_error(exc_info)
    assert error["error"]["code"] == "CONNECTION_FAILED"
    assert "disconnected" in error["error"]["details"]


@pytest.mark.parametrize(
    ("config", "reason", "available"),
    [
        ({"views": [{"path": "home"}]}, "not found", ["home"]),
        (
            {"views": [{"path": "lights"}, {"path": "lights"}]},
            "ambiguous",
            ["lights", "lights"],
        ),
    ],
)
def test_named_view_resolution_reports_missing_and_ambiguous_paths(
    config: dict[str, Any], reason: str, available: list[str]
) -> None:
    with pytest.raises(ToolError) as exc_info:
        resolve_dashboard_view("wall-panel", config, "lights")

    error = _tool_error(exc_info)
    assert error["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert reason in error["error"]["message"]
    assert error["dashboard_url_path"] == "wall-panel"
    assert error["view_path"] == "lights"
    assert error["available_view_paths"] == available


@pytest.mark.parametrize(
    ("config", "expected_index", "warning_text"),
    [
        ({"views": [{"path": "home"}]}, 0, "currently first"),
        ({"strategy": {"type": "original-states"}}, None, "runtime"),
        ({"views": []}, None, "no static views"),
    ],
)
def test_base_route_metadata_does_not_invent_a_view_index(
    config: dict[str, Any], expected_index: int | None, warning_text: str
) -> None:
    target = resolve_dashboard_view("wall-panel", config, None)

    assert target.render_path == "wall-panel"
    assert target.view_index == expected_index
    assert target.stable is False
    assert warning_text in target.warnings[0]


@pytest.mark.parametrize(
    ("dashboard_path", "dashboard_url_path", "view_path", "expected_code"),
    [
        (
            "lovelace/0",
            "wall-panel",
            None,
            "VALIDATION_INVALID_PARAMETER",
        ),
        (None, None, "lights", "VALIDATION_MISSING_PARAMETER"),
    ],
)
async def test_raw_and_structured_addressing_are_mutually_exclusive(
    dashboard_path: str | None,
    dashboard_url_path: str | None,
    view_path: str | None,
    expected_code: str,
) -> None:
    client = _FakeAsyncClient()

    with pytest.raises(ToolError) as exc_info:
        await resolve_dashboard_render_target(
            client,
            dashboard_path=dashboard_path,
            dashboard_url_path=dashboard_url_path,
            view_path=view_path,
        )

    error = _tool_error(exc_info)
    assert error["error"]["code"] == expected_code
    assert client.requests == []


async def test_raw_numeric_path_warns_with_canonical_stable_view_path() -> None:
    client = _FakeAsyncClient(
        _config_response(
            {"views": [{"path": "home"}, {"title": "Lights", "path": " lights "}]}
        )
    )

    target = await resolve_dashboard_render_target(
        client,
        dashboard_path="wall-panel/1",
        dashboard_url_path=None,
        view_path=None,
    )

    assert target == DashboardRenderTarget(
        dashboard_url_path="wall-panel",
        view_path=None,
        render_path="wall-panel/1",
        view_index=1,
        stable=False,
        warnings=(
            "Numeric view index 'wall-panel/1' is fragile; use the stable render "
            "path 'wall-panel/lights' or dashboard_url_path/view_path addressing.",
        ),
    )
    assert client.requests == [
        {
            "type": "lovelace/config",
            "force": True,
            "url_path": "wall-panel",
        }
    ]


async def test_raw_dashboard_base_is_reported_as_unstable() -> None:
    target = await resolve_dashboard_render_target(
        _FakeAsyncClient(
            _config_response({"views": [{"title": "Home", "path": "home"}]})
        ),
        dashboard_path="wall-panel",
        dashboard_url_path=None,
        view_path=None,
    )

    assert target.dashboard_url_path == "wall-panel"
    assert target.view_path is None
    assert target.view_index is None
    assert target.stable is False
    assert any("currently first" in warning for warning in target.warnings)


async def test_raw_route_cannot_target_non_dashboard_frontend_panel() -> None:
    client = _FakeAsyncClient(
        {
            "success": False,
            "error": {"message": "Unknown config specified: config"},
        }
    )

    with pytest.raises(ToolError) as exc_info:
        await resolve_dashboard_render_target(
            client,
            dashboard_path="config/integrations",
            dashboard_url_path=None,
            view_path=None,
        )

    error = _tool_error(exc_info)
    assert error["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert client.requests == [
        {
            "type": "lovelace/config",
            "force": True,
            "url_path": "config",
        }
    ]


def test_resolved_path_is_encoded_exactly_once_at_capture_boundary() -> None:
    from ha_mcp.dashboard_screenshot.capture import _validate_dashboard_path

    target = resolve_dashboard_view(
        "wall panel",
        {"views": [{"title": "Home", "path": "my view"}]},
        "my view",
    )

    assert target.render_path == "wall panel/my view"
    assert _validate_dashboard_path(target.render_path) == "wall%20panel/my%20view"


@pytest.mark.parametrize(
    "dashboard_path",
    [
        "https://evil.example/dashboard",
        "lovelace/../config",
        "lovelace/0?redirect=evil",
        "lovelace/0#fragment",
        "user@example/dashboard",
        "lovelace\\0",
    ],
)
async def test_raw_addressing_rejects_unsafe_paths(dashboard_path: str) -> None:
    client = _FakeAsyncClient()

    with pytest.raises(ToolError) as exc_info:
        await resolve_dashboard_render_target(
            client,
            dashboard_path=dashboard_path,
            dashboard_url_path=None,
            view_path=None,
        )

    error = _tool_error(exc_info)
    assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert client.requests == []


async def test_structured_addressing_rejects_unsafe_dashboard_before_fetch() -> None:
    client = _FakeAsyncClient()

    with pytest.raises(ToolError) as exc_info:
        await resolve_dashboard_render_target(
            client,
            dashboard_path=None,
            dashboard_url_path="https://evil.example/dashboard",
            view_path="home",
        )

    error = _tool_error(exc_info)
    assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert client.requests == []


async def test_structured_addressing_rejects_unsafe_named_view() -> None:
    client = _FakeAsyncClient(
        _config_response({"views": [{"path": "home?redirect=evil"}]})
    )

    with pytest.raises(ToolError) as exc_info:
        await resolve_dashboard_render_target(
            client,
            dashboard_path=None,
            dashboard_url_path="wall-panel",
            view_path="home?redirect=evil",
        )

    error = _tool_error(exc_info)
    assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert client.requests == [
        {
            "type": "lovelace/config",
            "force": True,
            "url_path": "wall-panel",
        }
    ]
