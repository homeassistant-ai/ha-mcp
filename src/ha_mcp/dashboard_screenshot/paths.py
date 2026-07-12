"""Stable Lovelace render-path resolution for dashboard screenshots."""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from typing import Any, cast

from fastmcp.exceptions import ToolError

from ..errors import ErrorCode, create_error_response
from ..tools.helpers import raise_tool_error
from .capture import _normalize_dashboard_path

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DashboardRenderTarget:
    """Resolved frontend route and audit metadata for a screenshot request."""

    dashboard_url_path: str
    view_path: str | None
    render_path: str
    view_index: int | None
    stable: bool
    warnings: tuple[str, ...] = ()


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
    return path.strip()


def _render_path_metadata(
    base_path: str,
    index: int,
    view: Any,
    path_counts: Counter[str],
) -> tuple[dict[str, Any], bool, bool]:
    """Resolve one view to stable or safe numeric render metadata."""
    view_config = view if isinstance(view, dict) else {}
    stable_path = _configured_view_path(view)
    ambiguous_path = stable_path is not None and path_counts[stable_path] > 1
    suffix = (
        stable_path if stable_path is not None and not ambiguous_path else str(index)
    )
    invalid_path = False
    try:
        render_path = _normalize_dashboard_path(f"{base_path}/{suffix}")
    except ToolError:
        # The configured path is unusable, but its numeric index remains a safe
        # route to the same view.
        invalid_path = True
        render_path = _normalize_dashboard_path(f"{base_path}/{index}")

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
                "dashboard base render path is available."
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
            base_path, index, view, path_counts
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
        message = error.get("message", str(error)) if isinstance(error, dict) else error
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

    config = response.get("result") if isinstance(response, dict) else response
    if not isinstance(config, dict):
        raise_tool_error(
            create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND,
                f"Dashboard '{dashboard_url_path}' has no renderable config.",
                context={"dashboard_url_path": dashboard_url_path},
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
        has_static_first_view = isinstance(views, list) and bool(views)
        if "strategy" in config:
            warning = (
                "No view_path was supplied; this strategy dashboard generates "
                "its views at runtime, so only the base route is available."
            )
        elif has_static_first_view:
            warning = (
                "No view_path was supplied; the dashboard base route renders "
                "whichever view is currently first."
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
            view_index=0 if has_static_first_view else None,
            stable=False,
            warnings=(warning,),
        )

    cleaned_view_path = view_path.strip()
    if not cleaned_view_path:
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
        if isinstance(view, dict)
        and isinstance(view.get("path"), str)
        and view["path"].strip() == cleaned_view_path
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
    render_path = _normalize_dashboard_path(f"{base_path}/{cleaned_view_path}")
    return DashboardRenderTarget(
        dashboard_url_path=base_path,
        view_path=cleaned_view_path,
        render_path=render_path,
        view_index=view_index,
        stable=True,
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
        render_path = _normalize_dashboard_path(dashboard_path or "")
        validated_config = await _validate_legacy_dashboard_root(client, render_path)
        warnings = await _numeric_view_warning(
            client, render_path, config=validated_config
        )
        parts = render_path.split("/")
        is_dashboard_base = len(parts) == 1
        if is_dashboard_base:
            warnings.append(
                "No named view path was supplied; the dashboard base route renders "
                "whichever view is currently first."
            )
        return DashboardRenderTarget(
            dashboard_url_path="/".join(parts[:-1]) if len(parts) > 1 else parts[0],
            view_path=parts[-1] if len(parts) > 1 and not parts[-1].isdigit() else None,
            render_path=render_path,
            view_index=(
                int(parts[-1]) if len(parts) > 1 and parts[-1].isdigit() else None
            ),
            stable=not is_dashboard_base
            and not (len(parts) > 1 and parts[-1].isdigit()),
            warnings=tuple(warnings),
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
        return None
    return await fetch_dashboard_render_config(client, dashboard_root)


async def _numeric_view_warning(
    client: Any,
    render_path: str,
    *,
    config: dict[str, Any] | None = None,
) -> list[str]:
    """Suggest a stable named route when a legacy numeric index has one."""
    parts = render_path.split("/")
    if len(parts) < 2 or not parts[-1].isdigit():
        return []
    index = int(parts[-1])
    dashboard_url_path = "/".join(parts[:-1])
    try:
        if config is None:
            config = await fetch_dashboard_render_config(client, dashboard_url_path)
        views = config.get("views")
        if not isinstance(views, list) or index >= len(views):
            return []
        view = views[index]
        stable_path = view.get("path") if isinstance(view, dict) else None
        if not isinstance(stable_path, str) or not stable_path.strip():
            return []
        stable_path = stable_path.strip()
        canonical = _normalize_dashboard_path(
            f"{dashboard_frontend_path(dashboard_url_path)}/{stable_path}"
        )
    except Exception as exc:
        logger.debug(
            "Could not inspect numeric dashboard path %s for a stable alias: %s",
            render_path,
            exc,
        )
        return []
    return [
        f"Numeric view index '{render_path}' is fragile; use the stable render "
        f"path '{canonical}' or dashboard_url_path/view_path addressing."
    ]
