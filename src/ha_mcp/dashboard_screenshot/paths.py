"""Stable Lovelace render-path resolution for dashboard screenshots."""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, cast

from fastmcp.exceptions import ToolError

from ..errors import ErrorCode, create_error_response
from ..tools.helpers import raise_tool_error
from .capture import _normalize_dashboard_path

_UNKNOWN_CONFIG_PREFIX = "Unknown config specified:"
_NO_CONFIG_MESSAGES = frozenset(
    {"No config found.", "Command failed: No config found."}
)
_JS_DECIMAL_NUMBER = re.compile(
    r"[+-]?(?:(?:\d+(?:\.\d*)?)|(?:\.\d+))(?:[eE][+-]?\d+)?\Z"
)
_JS_RADIX_NUMBER = re.compile(r"0(?P<base>[xXbBoO])(?P<digits>[0-9a-fA-F]+)\Z")
_RESERVED_VIEW_PATHS = frozenset({"hass-unused-entities"})
# Puppet percent-encodes each render-path segment, while HA's frontend applies
# decodeURI before matching views.  decodeURI intentionally leaves these URI
# delimiters encoded, so an exact configured-path match would silently miss.
_DECODE_URI_RESERVED_VIEW_CHARS = frozenset("$&+,:;=")


@dataclass(frozen=True, slots=True)
class DashboardRenderTarget:
    """Resolved frontend route and audit metadata for a screenshot request."""

    dashboard_url_path: str
    view_path: str | None
    render_path: str
    view_index: int | None
    stable: bool
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.stable and not self.warnings:
            raise ValueError(
                "a non-stable DashboardRenderTarget must carry at least one warning"
            )


def dashboard_frontend_path(url_path: str | None) -> str:
    """Map a dashboard URL path to its Lovelace frontend base route."""
    if not url_path or url_path in {"default", "lovelace"}:
        return "lovelace"
    return _normalize_dashboard_path(url_path)


def _configured_view_path(view: Any) -> str | None:
    """Return a non-blank configured view path, if one exists."""
    if not isinstance(view, dict):
        return None
    path = view.get("path")
    if not isinstance(path, str) or not path.strip():
        return None
    return path


def _numeric_view_index(path: str) -> int | None:
    """Mirror HA's ``Number(selectedView)`` integer-index route collision."""
    numeric_text = path.strip()
    radix_match = _JS_RADIX_NUMBER.fullmatch(numeric_text)
    if radix_match is not None:
        base = {"x": 16, "b": 2, "o": 8}[radix_match.group("base").lower()]
        try:
            return int(radix_match.group("digits"), base)
        except ValueError:
            return None
    if _JS_DECIMAL_NUMBER.fullmatch(numeric_text) is None:
        return None
    try:
        numeric = float(numeric_text)
    except ValueError:
        return None
    if not math.isfinite(numeric) or numeric < 0 or not numeric.is_integer():
        return None
    return int(numeric)


def _view_path_requires_numeric_fallback(
    path: str, index: int, views: list[Any]
) -> bool:
    """Return whether HA's frontend route cannot select this exact view by path."""
    return (
        _numeric_view_index(path) is not None
        or path != path.strip()
        or "/" in path
        or any(char in path for char in _DECODE_URI_RESERVED_VIEW_CHARS)
        or path in _RESERVED_VIEW_PATHS
        or _raw_frontend_view_match(path, views) != index
    )


def _numeric_fallback_suffix(index: int, views: list[Any]) -> str:
    """Choose a Number()-equivalent index route that HA resolves to ``index``."""
    decimal_places = 0
    candidate = str(index)
    while _raw_frontend_view_match(candidate, views) != index:
        decimal_places += 1
        candidate = f"{index}.{'0' * decimal_places}"
    return candidate


def _raw_frontend_view_match(raw_view: str, views: list[Any]) -> int | None:
    """Mirror HA's ordered path-or-Number view selection loop."""
    numeric_index = _numeric_view_index(raw_view)
    for index, view in enumerate(views):
        if _configured_view_path(view) == raw_view or index == numeric_index:
            return index
    return None


def _render_path_metadata(
    base_path: str,
    index: int,
    view: Any,
    path_counts: Counter[str],
    views: list[Any],
) -> tuple[dict[str, Any], bool, bool]:
    """Resolve one view to stable or safe numeric render metadata."""
    view_config = view if isinstance(view, dict) else {}
    stable_path = _configured_view_path(view)
    ambiguous_path = stable_path is not None and path_counts[stable_path] > 1
    unusable_path = stable_path is not None and _view_path_requires_numeric_fallback(
        stable_path, index, views
    )
    fallback_suffix = _numeric_fallback_suffix(index, views)
    suffix = (
        stable_path
        if stable_path is not None and not ambiguous_path and not unusable_path
        else fallback_suffix
    )
    invalid_path = unusable_path
    try:
        render_path = _normalize_dashboard_path(f"{base_path}/{suffix}")
        if suffix == stable_path and render_path != f"{base_path}/{stable_path}":
            invalid_path = True
            render_path = _normalize_dashboard_path(f"{base_path}/{fallback_suffix}")
    except ToolError:
        # The configured path is unusable, but its numeric index remains a safe
        # route to the same view.
        invalid_path = True
        render_path = _normalize_dashboard_path(f"{base_path}/{fallback_suffix}")

    metadata: dict[str, Any] = {
        "dashboard_url_path": base_path,
        "view_index": index,
        "view_path": stable_path,
        "title": view_config.get("title"),
        "render_path": render_path,
        "stable": stable_path is not None and not ambiguous_path and not invalid_path,
    }
    if ambiguous_path:
        metadata["ambiguous_path"] = True
    if invalid_path:
        metadata["invalid_path"] = True
    return (
        metadata,
        stable_path is None or ambiguous_path or invalid_path,
        ambiguous_path,
    )


def dashboard_render_paths(
    url_path: str | None, config: dict[str, Any] | None
) -> tuple[list[dict[str, Any]], list[str]]:
    """Build canonical render metadata without mutating dashboard config."""
    base_path = dashboard_frontend_path(url_path)
    if not isinstance(config, dict):
        return [], []
    if "strategy" in config:
        return (
            [
                {
                    "dashboard_url_path": base_path,
                    "view_index": None,
                    "view_path": None,
                    "title": None,
                    "render_path": base_path,
                    "stable": False,
                    "strategy_dashboard": True,
                }
            ],
            [
                "Strategy dashboard views are generated at runtime; only the "
                + "dashboard base render path is available."
            ],
        )

    views = config.get("views")
    if not isinstance(views, list):
        return [], []

    configured_paths = [
        path for view in views if (path := _configured_view_path(view)) is not None
    ]
    path_counts = Counter(configured_paths)
    paths: list[dict[str, Any]] = []
    fallback_count = 0
    ambiguous_count = 0
    for index, view in enumerate(views):
        metadata, used_fallback, ambiguous_path = _render_path_metadata(
            base_path, index, view, path_counts, views
        )
        if used_fallback:
            fallback_count += 1
        if ambiguous_path:
            ambiguous_count += 1
        paths.append(metadata)

    warnings: list[str] = []
    if fallback_count:
        warnings.append(
            f"{fallback_count} dashboard view(s) have no usable stable path; "
            "their render paths use fragile numeric indexes."
        )
    if ambiguous_count:
        warnings.append(
            f"{ambiguous_count} dashboard view(s) share a duplicate configured path; "
            "numeric render paths are returned for those ambiguous views."
        )
    return paths, warnings


def _safe_named_render_path(base_path: str, view_path: str) -> str | None:
    """Return an unchanged safe named route, or ``None`` when it is unusable."""
    expected = f"{base_path}/{view_path}"
    try:
        normalized = _normalize_dashboard_path(expected)
    except ToolError:
        return None
    return normalized if normalized == expected else None


def _fallback_view_target(
    *,
    base_path: str,
    view_path: str,
    view_index: int,
    views: list[Any],
    reason: str,
) -> DashboardRenderTarget:
    """Build one verified numeric fallback target for an unusable named path."""
    fallback_suffix = _numeric_fallback_suffix(view_index, views)
    return DashboardRenderTarget(
        dashboard_url_path=base_path,
        view_path=view_path,
        render_path=_normalize_dashboard_path(f"{base_path}/{fallback_suffix}"),
        view_index=view_index,
        stable=False,
        warnings=(
            f"Configured view path '{view_path}' {reason}; the verified numeric "
            f"fallback '{base_path}/{fallback_suffix}' is used.",
        ),
    )


async def fetch_dashboard_render_config(
    client: Any, dashboard_url_path: str
) -> dict[str, Any]:
    """Fetch a Lovelace dashboard config for stable view-path resolution."""
    base_path = dashboard_frontend_path(dashboard_url_path)
    request: dict[str, Any] = {"type": "lovelace/config", "force": True}
    if base_path != "lovelace":
        request["url_path"] = base_path

    try:
        response = await client.send_websocket_message(request)
    except ToolError:
        raise
    except Exception as exc:
        raise_tool_error(
            create_error_response(
                ErrorCode.CONNECTION_FAILED,
                f"Could not load dashboard '{dashboard_url_path}' for view-path "
                "resolution.",
                details=str(exc),
                context={"dashboard_url_path": dashboard_url_path},
                suggestions=[
                    "Check the Home Assistant connection",
                    "Retry with legacy dashboard_path if you already know the frontend route",
                ],
            )
        )
    if isinstance(response, dict) and not response.get("success", True):
        error = response.get("error", {})
        if isinstance(error, dict):
            error_code = str(error.get("code") or "").lower()
            message = str(error.get("message", error))
        else:
            error_code = ""
            message = str(error)
        missing_dashboard = (
            error_code == "config_not_found"
            or message.startswith(
                (_UNKNOWN_CONFIG_PREFIX, f"Command failed: {_UNKNOWN_CONFIG_PREFIX}")
            )
            or message in _NO_CONFIG_MESSAGES
        )
        if missing_dashboard:
            raise_tool_error(
                create_error_response(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    f"Could not load dashboard '{dashboard_url_path}': {message}",
                    context={"dashboard_url_path": dashboard_url_path},
                    suggestions=[
                        "Use ha_config_get_dashboard(list_only=True) to list dashboard URL paths"
                    ],
                )
            )
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Failed to load dashboard '{dashboard_url_path}': {message}",
                context={
                    "dashboard_url_path": dashboard_url_path,
                    "home_assistant_error_code": error_code or None,
                },
                suggestions=[
                    "Check Home Assistant permissions and logs",
                    "Retry after confirming the Home Assistant connection is healthy",
                ],
            )
        )

    config = response.get("result") if isinstance(response, dict) else response
    if not isinstance(config, dict):
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Dashboard '{dashboard_url_path}' returned an invalid config payload.",
                context={
                    "dashboard_url_path": dashboard_url_path,
                    "payload_type": type(config).__name__,
                },
            )
        )
    return cast(dict[str, Any], config)


def resolve_dashboard_view(
    dashboard_url_path: str,
    config: dict[str, Any],
    view_path: str | None,
) -> DashboardRenderTarget:
    """Resolve one named Lovelace view to a canonical frontend route."""
    base_path = dashboard_frontend_path(dashboard_url_path)
    if view_path is None:
        views = config.get("views")
        has_static_views = isinstance(views, list) and bool(views)
        if "strategy" in config:
            warning = (
                "No view_path was supplied; this strategy dashboard generates "
                "its views at runtime, so only the base route is available."
            )
        elif has_static_views:
            warning = (
                "No view_path was supplied; the dashboard base route renders "
                "the first view visible to Puppet's Home Assistant user."
            )
        else:
            warning = (
                "No view_path was supplied and the dashboard has no static views; "
                "only the base route is available."
            )
        return DashboardRenderTarget(
            dashboard_url_path=base_path,
            view_path=None,
            render_path=base_path,
            view_index=None,
            stable=False,
            warnings=(warning,),
        )

    cleaned_view_path = view_path
    if not cleaned_view_path.strip():
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "view_path cannot be empty.",
                context={"view_path": view_path},
            )
        )
    if "strategy" in config:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "Strategy dashboards do not expose static named view paths.",
                context={
                    "dashboard_url_path": dashboard_url_path,
                    "view_path": cleaned_view_path,
                },
                suggestions=["Use dashboard_path with the frontend route instead"],
            )
        )
    views = config.get("views")
    if not isinstance(views, list):
        views = []
    matches = [
        (index, view)
        for index, view in enumerate(views)
        if _configured_view_path(view) == cleaned_view_path
    ]
    if len(matches) != 1:
        available = [
            view.get("path")
            for view in views
            if isinstance(view, dict) and isinstance(view.get("path"), str)
        ]
        reason = "not found" if not matches else "ambiguous"
        raise_tool_error(
            create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND,
                f"Dashboard view path '{cleaned_view_path}' is {reason}.",
                context={
                    "dashboard_url_path": dashboard_url_path,
                    "view_path": cleaned_view_path,
                    "available_view_paths": available,
                },
                suggestions=["Use a view_path returned by ha_config_get_dashboard"],
            )
        )

    view_index, _ = matches[0]
    render_path = _safe_named_render_path(base_path, cleaned_view_path)
    if render_path is None:
        return _fallback_view_target(
            base_path=base_path,
            view_path=cleaned_view_path,
            view_index=view_index,
            views=views,
            reason="is unsafe for screenshot transport",
        )
    if _view_path_requires_numeric_fallback(cleaned_view_path, view_index, views):
        return _fallback_view_target(
            base_path=base_path,
            view_path=cleaned_view_path,
            view_index=view_index,
            views=views,
            reason=(
                "cannot be selected reliably as a named Home Assistant frontend route"
            ),
        )
    return DashboardRenderTarget(
        dashboard_url_path=base_path,
        view_path=cleaned_view_path,
        render_path=render_path,
        view_index=view_index,
        stable=True,
    )


async def _resolve_legacy_dashboard_target(
    client: Any, dashboard_path: str | None
) -> DashboardRenderTarget:
    """Validate and resolve the backward-compatible raw route form."""
    render_path = _normalize_dashboard_path(dashboard_path or "")
    parts = render_path.split("/")
    dashboard_root = parts[0]
    if len(parts) > 1 and parts[1] in _RESERVED_VIEW_PATHS:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Raw view suffix '{parts[1]}' is reserved by Home Assistant.",
                context={"dashboard_url_path": dashboard_root},
            )
        )
    validated_config = await _validate_legacy_dashboard_root(client, render_path)
    if validated_config is None:
        raw_view = parts[1] if len(parts) > 1 else None
        numeric_index = _numeric_view_index(raw_view) if raw_view is not None else None
        return DashboardRenderTarget(
            dashboard_url_path="lovelace",
            view_path=None,
            render_path=render_path,
            view_index=numeric_index,
            stable=False,
            warnings=(
                "Legacy built-in Lovelace routes are accepted without a stored "
                "dashboard config; the selected view cannot be verified.",
            ),
        )
    if len(parts) == 1:
        return resolve_dashboard_view(dashboard_root, validated_config, None)

    raw_view = parts[1]
    if "strategy" in validated_config:
        return DashboardRenderTarget(
            dashboard_url_path=dashboard_root,
            view_path=None,
            render_path=render_path,
            view_index=None,
            stable=False,
            warnings=(
                "This strategy dashboard generates views at runtime; the raw "
                "view suffix cannot be verified against static config.",
            ),
        )
    numeric_index = _numeric_view_index(raw_view)
    if numeric_index is None:
        views = validated_config.get("views")
        view_index = (
            _raw_frontend_view_match(raw_view, views)
            if isinstance(views, list)
            else None
        )
        if view_index is None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    f"Dashboard view path '{raw_view}' was not found.",
                    context={
                        "dashboard_url_path": dashboard_root,
                        "view_path": raw_view,
                        "available_view_paths": [
                            path
                            for view in views or []
                            if (path := _configured_view_path(view)) is not None
                        ],
                    },
                    suggestions=[
                        "Use a render path returned by ha_config_get_dashboard"
                    ],
                )
            )
        assert isinstance(views, list)
        duplicate_count = sum(_configured_view_path(view) == raw_view for view in views)
        if duplicate_count > 1:
            return DashboardRenderTarget(
                dashboard_url_path=dashboard_root,
                view_path=raw_view,
                render_path=_normalize_dashboard_path(f"{dashboard_root}/{raw_view}"),
                view_index=view_index,
                stable=False,
                warnings=(
                    f"Configured view path '{raw_view}' is duplicated; Home "
                    "Assistant selects the first matching view in config order.",
                ),
            )
        return resolve_dashboard_view(dashboard_root, validated_config, raw_view)

    views = validated_config.get("views")
    view_index = (
        _raw_frontend_view_match(raw_view, views) if isinstance(views, list) else None
    )
    if not isinstance(views, list) or view_index is None:
        raise_tool_error(
            create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND,
                f"Dashboard view index '{raw_view}' was not found.",
                context={
                    "dashboard_url_path": dashboard_root,
                    "view_index": numeric_index,
                    "available_view_count": len(views)
                    if isinstance(views, list)
                    else 0,
                },
                suggestions=["Use a render path returned by ha_config_get_dashboard"],
            )
        )
    # Canonicalize to the verified numeric route so a stray trailing segment
    # (e.g. "wall-panel/1/debug") cannot reach the engine unverified — only
    # parts[1] was validated against the dashboard config.
    render_path = _normalize_dashboard_path(f"{dashboard_root}/{raw_view}")
    warnings = await _numeric_view_warning(
        client,
        render_path,
        config=validated_config,
        matched_view_index=view_index,
    )
    if not warnings:
        warnings.append(
            f"Numeric view index '{render_path}' is fragile; assign the view a "
            "unique views[].path and use dashboard_url_path/view_path addressing."
        )
    return DashboardRenderTarget(
        dashboard_url_path=dashboard_root,
        view_path=None,
        render_path=render_path,
        view_index=view_index,
        stable=False,
        warnings=tuple(warnings),
    )


async def resolve_dashboard_render_target(
    client: Any,
    *,
    dashboard_path: str | None,
    dashboard_url_path: str | None,
    view_path: str | None,
) -> DashboardRenderTarget:
    """Resolve legacy raw or structured screenshot addressing."""
    if dashboard_path is not None and dashboard_url_path is not None:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "Use dashboard_path or dashboard_url_path/view_path, not both.",
                context={
                    "dashboard_path": dashboard_path,
                    "dashboard_url_path": dashboard_url_path,
                    "view_path": view_path,
                },
            )
        )
    if dashboard_url_path is None:
        if view_path is not None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_MISSING_PARAMETER,
                    "dashboard_url_path is required when view_path is supplied.",
                    context={"view_path": view_path},
                )
            )
        return await _resolve_legacy_dashboard_target(client, dashboard_path)

    if not dashboard_url_path.strip():
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "dashboard_url_path cannot be empty.",
                context={"dashboard_url_path": dashboard_url_path},
            )
        )
    if dashboard_frontend_path(dashboard_url_path) == "lovelace" and view_path is None:
        return DashboardRenderTarget(
            dashboard_url_path="lovelace",
            view_path=None,
            render_path="lovelace",
            view_index=None,
            stable=False,
            warnings=(
                "No named view path was supplied; the built-in dashboard base route "
                "renders the first view visible to Puppet's Home Assistant user.",
            ),
        )
    config = await fetch_dashboard_render_config(client, dashboard_url_path)
    return resolve_dashboard_view(dashboard_url_path, config, view_path)


async def _validate_legacy_dashboard_root(
    client: Any, render_path: str
) -> dict[str, Any] | None:
    """Prove a custom raw-route root resolves as a Lovelace dashboard.

    Fetching ``lovelace/config`` works for storage- and YAML-mode dashboards,
    unlike the storage registry list. The returned config is reused when a
    numeric view needs a stable-alias warning.
    """
    dashboard_root = render_path.split("/", maxsplit=1)[0]
    if dashboard_root == "lovelace":
        try:
            return await fetch_dashboard_render_config(client, dashboard_root)
        except ToolError as exc:
            try:
                error_payload = json.loads(str(exc))
            except (json.JSONDecodeError, TypeError):
                raise
            if (
                isinstance(error_payload, dict)
                and isinstance(error_payload.get("error"), dict)
                and error_payload["error"].get("code") == "RESOURCE_NOT_FOUND"
            ):
                return None
            raise
    return await fetch_dashboard_render_config(client, dashboard_root)


async def _numeric_view_warning(
    client: Any,
    render_path: str,
    *,
    config: dict[str, Any] | None = None,
    matched_view_index: int | None = None,
) -> list[str]:
    """Suggest a stable named route when a legacy numeric index has one."""
    parts = render_path.split("/", maxsplit=1)
    if len(parts) < 2:
        return []
    dashboard_url_path, raw_view = parts
    index = _numeric_view_index(raw_view)
    if index is None:
        return []
    if config is None:
        config = await fetch_dashboard_render_config(client, dashboard_url_path)
    views = config.get("views")
    if not isinstance(views, list):
        return []
    view_index = index if matched_view_index is None else matched_view_index
    if view_index >= len(views):
        return []
    view = views[view_index]
    stable_path = view.get("path") if isinstance(view, dict) else None
    if not isinstance(stable_path, str) or not stable_path.strip():
        return []
    if _view_path_requires_numeric_fallback(stable_path, view_index, views):
        return []
    matching_paths = [
        candidate_path
        for candidate in views
        if (candidate_path := _configured_view_path(candidate)) is not None
    ]
    if matching_paths.count(stable_path) != 1:
        return []
    canonical = _safe_named_render_path(
        dashboard_frontend_path(dashboard_url_path), stable_path
    )
    if canonical is None:
        return []
    return [
        f"Numeric view index '{render_path}' is fragile; use the stable render "
        + f"path '{canonical}' or dashboard_url_path/view_path addressing."
    ]
