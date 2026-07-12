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
            {"title": "Home", "path": "home", "cards": [{"type": "clock"}]},
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
                    {"title": "Lights", "path": "lights"},
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
    "ha_error",
    [
        {
            "code": "config_not_found",
            "message": "A localized message that does not contain the path marker",
        },
        "Command failed: Unknown config specified: wall-panel",
        {"code": None, "message": "No config found."},
        "Command failed: No config found.",
    ],
)
async def test_dashboard_fetch_maps_only_exact_missing_signals_to_not_found(
    ha_error: Any,
) -> None:
    client = _FakeAsyncClient({"success": False, "error": ha_error})

    with pytest.raises(ToolError) as exc_info:
        await resolve_dashboard_render_target(
            client,
            dashboard_path=None,
            dashboard_url_path="wall-panel",
            view_path="home",
        )

    error = _tool_error(exc_info)
    assert error["error"]["code"] == "RESOURCE_NOT_FOUND"


@pytest.mark.parametrize(
    "ha_error",
    [
        {"code": "unauthorized", "message": "Forbidden"},
        {"code": "not_found", "message": "Unknown config specified later"},
        {
            "code": "other",
            "message": "Wrapper contains Unknown config specified: wall-panel",
        },
        {"code": "other", "message": "Wrapper contains No config found."},
        {"code": None, "message": None},
        "Command failed: No config found. Retry later",
        "WebSocket request failed",
    ],
)
async def test_dashboard_fetch_preserves_non_missing_failures(
    ha_error: Any,
) -> None:
    client = _FakeAsyncClient({"success": False, "error": ha_error})

    with pytest.raises(ToolError) as exc_info:
        await resolve_dashboard_render_target(
            client,
            dashboard_path=None,
            dashboard_url_path="wall-panel",
            view_path="home",
        )

    error = _tool_error(exc_info)
    assert error["error"]["code"] == "SERVICE_CALL_FAILED"


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
        ({"views": [{"path": "home"}]}, None, "first view visible"),
        (
            {
                "views": [
                    {"path": "hidden", "visible": False},
                    {"path": "shown"},
                ]
            },
            None,
            "first view visible",
        ),
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
            {"views": [{"path": "home"}, {"title": "Lights", "path": "lights"}]}
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


async def test_raw_js_numeric_route_warns_with_canonical_stable_view_path() -> None:
    client = _FakeAsyncClient(
        _config_response({"views": [{"path": "home"}, {"path": "lights"}]})
    )

    target = await resolve_dashboard_render_target(
        client,
        dashboard_path="wall-panel/1e0",
        dashboard_url_path=None,
        view_path=None,
    )

    assert target.view_index == 1
    assert "wall-panel/lights" in target.warnings[0]


async def test_raw_default_numeric_path_uses_stored_stable_alias() -> None:
    client = _FakeAsyncClient(
        _config_response({"views": [{"title": "Home", "path": "home"}]})
    )

    target = await resolve_dashboard_render_target(
        client,
        dashboard_path="lovelace/0",
        dashboard_url_path=None,
        view_path=None,
    )

    assert target.view_index == 0
    assert target.stable is False
    assert "lovelace/home" in target.warnings[0]
    assert client.requests == [{"type": "lovelace/config", "force": True}]


async def test_raw_default_path_allows_known_no_config_fallback() -> None:
    client = _FakeAsyncClient(
        {
            "success": False,
            "error": {
                "code": "config_not_found",
                "message": "No config found",
            },
        }
    )

    target = await resolve_dashboard_render_target(
        client,
        dashboard_path="lovelace/0",
        dashboard_url_path=None,
        view_path=None,
    )

    assert target.render_path == "lovelace/0"
    assert target.view_index == 0
    assert target.stable is False
    assert "cannot be verified" in target.warnings[0]


async def test_raw_default_path_does_not_hide_permission_failure() -> None:
    client = _FakeAsyncClient(
        {
            "success": False,
            "error": {"code": "unauthorized", "message": "Forbidden"},
        }
    )

    with pytest.raises(ToolError) as exc_info:
        await resolve_dashboard_render_target(
            client,
            dashboard_path="lovelace/0",
            dashboard_url_path=None,
            view_path=None,
        )

    assert _tool_error(exc_info)["error"]["code"] == "SERVICE_CALL_FAILED"


@pytest.mark.parametrize("invalid_result", [None, [], "invalid"])
async def test_raw_default_path_does_not_hide_malformed_success(
    invalid_result: Any,
) -> None:
    client = _FakeAsyncClient({"success": True, "result": invalid_result})

    with pytest.raises(ToolError) as exc_info:
        await resolve_dashboard_render_target(
            client,
            dashboard_path="lovelace/0",
            dashboard_url_path=None,
            view_path=None,
        )

    error = _tool_error(exc_info)
    assert error["error"]["code"] == "SERVICE_CALL_FAILED"
    assert error["payload_type"] == type(invalid_result).__name__


@pytest.mark.parametrize("dashboard_url_path", ["", "   "])
async def test_structured_empty_dashboard_path_is_rejected_without_ws(
    dashboard_url_path: str,
) -> None:
    client = _FakeAsyncClient()

    with pytest.raises(ToolError) as exc_info:
        await resolve_dashboard_render_target(
            client,
            dashboard_path=None,
            dashboard_url_path=dashboard_url_path,
            view_path="home",
        )

    assert _tool_error(exc_info)["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert client.requests == []


@pytest.mark.parametrize("raw_view", ["missing", "99"])
async def test_raw_custom_suffix_must_resolve_to_a_real_view(raw_view: str) -> None:
    client = _FakeAsyncClient(_config_response({"views": [{"path": "home"}]}))

    with pytest.raises(ToolError) as exc_info:
        await resolve_dashboard_render_target(
            client,
            dashboard_path=f"wall-panel/{raw_view}",
            dashboard_url_path=None,
            view_path=None,
        )

    assert _tool_error(exc_info)["error"]["code"] == "RESOURCE_NOT_FOUND"


async def test_raw_duplicate_named_suffix_selects_first_but_is_unstable() -> None:
    client = _FakeAsyncClient(
        _config_response({"views": [{"path": "home"}, {"path": "home"}]})
    )

    target = await resolve_dashboard_render_target(
        client,
        dashboard_path="wall-panel/home",
        dashboard_url_path=None,
        view_path=None,
    )

    assert target.view_index == 0
    assert target.render_path == "wall-panel/home"
    assert target.stable is False
    assert "first matching" in target.warnings[0]


async def test_raw_strategy_suffix_is_unverified_not_stable() -> None:
    client = _FakeAsyncClient(
        _config_response({"strategy": {"type": "original-states"}})
    )

    target = await resolve_dashboard_render_target(
        client,
        dashboard_path="wall-panel/runtime/view",
        dashboard_url_path=None,
        view_path=None,
    )

    assert target.render_path == "wall-panel/runtime/view"
    assert target.stable is False
    assert "cannot be verified" in target.warnings[0]


async def test_raw_multi_segment_cannot_select_slash_configured_path() -> None:
    client = _FakeAsyncClient(_config_response({"views": [{"path": "floor/second"}]}))

    with pytest.raises(ToolError) as exc_info:
        await resolve_dashboard_render_target(
            client,
            dashboard_path="wall-panel/floor/second",
            dashboard_url_path=None,
            view_path=None,
        )

    assert _tool_error(exc_info)["error"]["code"] == "RESOURCE_NOT_FOUND"


async def test_raw_multi_segment_uses_only_frontend_view_suffix() -> None:
    client = _FakeAsyncClient(_config_response({"views": [{"path": "floor"}]}))

    target = await resolve_dashboard_render_target(
        client,
        dashboard_path="wall-panel/floor/second",
        dashboard_url_path=None,
        view_path=None,
    )

    assert target.view_path == "floor"
    assert target.render_path == "wall-panel/floor"
    assert target.stable is True


async def test_duplicate_numeric_warning_does_not_recommend_ambiguous_alias() -> None:
    client = _FakeAsyncClient(
        _config_response({"views": [{"path": "home"}, {"path": "home"}]})
    )

    target = await resolve_dashboard_render_target(
        client,
        dashboard_path="wall-panel/1",
        dashboard_url_path=None,
        view_path=None,
    )

    assert target.render_path == "wall-panel/1"
    assert "wall-panel/home" not in target.warnings[0]
    assert "unique views[].path" in target.warnings[0]


async def test_numeric_warning_does_not_recommend_normalization_changed_alias() -> None:
    client = _FakeAsyncClient(_config_response({"views": [{"path": "."}]}))

    target = await resolve_dashboard_render_target(
        client,
        dashboard_path="wall-panel/0",
        dashboard_url_path=None,
        view_path=None,
    )

    assert target.view_index == 0
    assert "stable render path 'wall-panel'" not in target.warnings[0]
    assert "unique views[].path" in target.warnings[0]


@pytest.mark.parametrize("configured_path", ["1", "01", "1.0", "1e0", "0x1"])
def test_numeric_coercible_named_path_uses_verified_index_fallback(
    configured_path: str,
) -> None:
    config = {
        "views": [
            {"path": "first"},
            {"path": "second"},
            {"path": configured_path},
        ]
    }

    target = resolve_dashboard_view("wall-panel", config, configured_path)

    assert target.view_index == 2
    assert target.render_path == "wall-panel/2"
    assert target.stable is False
    assert "numeric fallback" in target.warnings[0]


def test_numeric_leading_non_number_path_remains_stable() -> None:
    target = resolve_dashboard_view("wall-panel", {"views": [{"path": "1abc"}]}, "1abc")

    assert target.render_path == "wall-panel/1abc"
    assert target.stable is True


@pytest.mark.parametrize("configured_path", ["1", "1e0"])
async def test_raw_exact_numeric_path_precedes_index_match(
    configured_path: str,
) -> None:
    client = _FakeAsyncClient(
        _config_response({"views": [{"path": configured_path}, {"path": "second"}]})
    )

    target = await resolve_dashboard_render_target(
        client,
        dashboard_path=f"wall-panel/{configured_path}",
        dashboard_url_path=None,
        view_path=None,
    )

    assert target.view_index == 0
    assert target.stable is False
    assert "wall-panel/second" not in target.warnings[0]


async def test_shadowed_numeric_fallback_uses_equivalent_unshadowed_suffix() -> None:
    config = {
        "views": [
            {"path": "1"},
            {"path": "floor/second"},
        ]
    }

    paths, _ = dashboard_render_paths("wall-panel", config)
    target = resolve_dashboard_view("wall-panel", config, "floor/second")
    raw_target = await resolve_dashboard_render_target(
        _FakeAsyncClient(_config_response(config)),
        dashboard_path="wall-panel/1.0",
        dashboard_url_path=None,
        view_path=None,
    )

    assert paths[1]["render_path"] == "wall-panel/1.0"
    assert target.render_path == "wall-panel/1.0"
    assert raw_target.view_index == 1


@pytest.mark.parametrize(
    "configured_path", ["floor/second", "hass-unused-entities", ".", " home "]
)
def test_unusable_configured_paths_use_verified_numeric_fallback(
    configured_path: str,
) -> None:
    paths, warnings = dashboard_render_paths(
        "wall-panel", {"views": [{"path": configured_path}]}
    )

    assert paths[0]["view_path"] == configured_path
    assert paths[0]["render_path"] == "wall-panel/0"
    assert paths[0]["stable"] is False
    assert paths[0]["invalid_path"] is True
    assert warnings


@pytest.mark.parametrize("reserved_character", list("$&+,:;="))
def test_decode_uri_reserved_view_paths_use_numeric_fallback(
    reserved_character: str,
) -> None:
    configured_path = f"heat{reserved_character}zone"
    config = {
        "views": [
            {"title": "Home", "path": "home"},
            {"title": "Heat", "path": configured_path},
        ]
    }

    paths, warnings = dashboard_render_paths("wall-panel", config)
    target = resolve_dashboard_view("wall-panel", config, configured_path)

    assert paths[1]["view_path"] == configured_path
    assert paths[1]["view_index"] == 1
    assert paths[1]["render_path"] == "wall-panel/1"
    assert paths[1]["stable"] is False
    assert paths[1]["invalid_path"] is True
    assert target.render_path == "wall-panel/1"
    assert target.view_index == 1
    assert target.stable is False
    assert "numeric fallback" in target.warnings[0]
    assert warnings


def test_exact_whitespace_view_path_resolves_to_numeric_fallback() -> None:
    target = resolve_dashboard_view(
        "wall-panel", {"views": [{"path": " home "}]}, " home "
    )

    assert target.view_path == " home "
    assert target.render_path == "wall-panel/0"
    assert target.stable is False


@pytest.mark.parametrize(
    "dashboard_path",
    ["lovelace/hass-unused-entities", "wall-panel/hass-unused-entities"],
)
async def test_raw_reserved_view_suffix_is_rejected(
    dashboard_path: str,
) -> None:
    client = _FakeAsyncClient()

    with pytest.raises(ToolError) as exc_info:
        await resolve_dashboard_render_target(
            client,
            dashboard_path=dashboard_path,
            dashboard_url_path=None,
            view_path=None,
        )

    assert _tool_error(exc_info)["error"]["code"] == ("VALIDATION_INVALID_PARAMETER")
    assert client.requests == []


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
    assert any("first view visible" in warning for warning in target.warnings)


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


async def test_structured_addressing_falls_back_for_unsafe_named_view() -> None:
    client = _FakeAsyncClient(
        _config_response({"views": [{"path": "home?redirect=evil"}]})
    )

    target = await resolve_dashboard_render_target(
        client,
        dashboard_path=None,
        dashboard_url_path="wall-panel",
        view_path="home?redirect=evil",
    )

    assert target.render_path == "wall-panel/0"
    assert target.stable is False
    assert "unsafe" in target.warnings[0]
    assert client.requests == [
        {
            "type": "lovelace/config",
            "force": True,
            "url_path": "wall-panel",
        }
    ]
