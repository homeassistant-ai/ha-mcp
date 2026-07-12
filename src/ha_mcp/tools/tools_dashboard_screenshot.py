"""Dashboard screenshot tool (opt-in, beta).

Gated behind ``enable_dashboard_screenshot``. Renders one or more Lovelace
dashboard images via the separate screenshot engine add-on (or a docker-compose
sidecar) and returns native MCP image blocks for visual verification.

The companion ``include_screenshot`` / ``return_screenshot`` parameters on
``ha_config_get_dashboard`` / ``ha_config_set_dashboard`` share the same
capture path (see ``ha_mcp.dashboard_screenshot.capture``).
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any, Literal, NoReturn

from fastmcp.exceptions import ToolError
from fastmcp.tools import tool
from fastmcp.tools.tool import ToolResult
from pydantic import Field

from ..dashboard_screenshot.capture import (
    DEFAULT_HEIGHT,
    DEFAULT_RENDER_TIMEOUT_SECONDS,
    DEFAULT_WAIT_MS,
    DEFAULT_WIDTH,
    FULL_PAGE_PARAM_DESC,
    Orientation,
    ScreenshotFormat,
    ViewportPreset,
    capture_dashboard_images,
    validate_capture_parameters,
)
from ..dashboard_screenshot.content import (
    dashboard_image_content,
    dashboard_screenshot_metadata,
    dashboard_screenshot_warnings,
)
from ..dashboard_screenshot.paths import (
    resolve_dashboard_render_target,
)
from ..errors import ErrorCode, create_error_response
from .helpers import log_tool_usage, raise_tool_error, register_tool_methods
from .util_helpers import JSON_STRING_COERCION

logger = logging.getLogger(__name__)


def _raise_with_puppet_configuration(
    error: Exception, puppet_configuration: dict[str, Any]
) -> NoReturn:
    """Preserve a completed Puppet mutation when a later capture step fails."""
    try:
        payload = json.loads(str(error))
    except (json.JSONDecodeError, TypeError):
        payload = None
    if isinstance(payload, dict):
        payload["puppet_configuration_applied"] = puppet_configuration
        raise_tool_error(payload)
    raise_tool_error(
        create_error_response(
            ErrorCode.INTERNAL_ERROR,
            "Puppet configuration completed, but the subsequent screenshot failed.",
            details=str(error),
            context={"puppet_configuration_applied": puppet_configuration},
        )
    )
    raise AssertionError("unreachable: raise_tool_error always raises")


def _management_only_ignored_options(
    *,
    width: int,
    height: int | Literal["auto"],
    viewport_presets: list[ViewportPreset] | None,
    orientation: Orientation | None,
    zoom: float,
    wait_ms: int,
    full_page: bool,
    theme: str | None,
    dark_mode: bool,
    language: str | None,
    image_format: ScreenshotFormat,
    render_timeout_seconds: float,
) -> list[str]:
    """List render arguments that management-only mode would otherwise drop."""
    supplied = {
        "width": width != DEFAULT_WIDTH,
        "height": height != DEFAULT_HEIGHT,
        "viewport_presets": viewport_presets is not None,
        "orientation": orientation is not None,
        "zoom": zoom != 1.0,
        "wait_ms": wait_ms != DEFAULT_WAIT_MS,
        "full_page": full_page,
        "theme": theme is not None,
        "dark_mode": dark_mode,
        "language": language is not None,
        "image_format": image_format != "png",
        "render_timeout_seconds": (
            render_timeout_seconds != DEFAULT_RENDER_TIMEOUT_SECONDS
        ),
    }
    return [name for name, is_supplied in supplied.items() if is_supplied]


def _package_screenshot_result(
    *,
    captures: list[Any],
    target: Any,
    puppet_configuration: dict[str, Any] | None,
    capture_failures: list[dict[str, Any]],
) -> ToolResult:
    """Build the standalone native-image result with structured failures."""
    try:
        structured_content: dict[str, Any] = {
            "success": True,
            "dashboard_url_path": target.dashboard_url_path,
            "view_path": target.view_path,
            "view_index": target.view_index,
            "render_path": target.render_path,
            "stable_addressing": target.stable,
            "screenshot_count": len(captures),
            "screenshots": dashboard_screenshot_metadata(captures, target.render_path),
        }
        if capture_failures:
            structured_content["partial"] = True
            structured_content["screenshot_failures"] = capture_failures
        warnings = [*target.warnings, *dashboard_screenshot_warnings(captures)]
        if warnings:
            structured_content["warnings"] = warnings
        if puppet_configuration is not None:
            structured_content["puppet_configuration"] = puppet_configuration
        return ToolResult(
            content=dashboard_image_content(captures),
            structured_content=structured_content,
        )
    except ToolError as error:
        if puppet_configuration is not None:
            _raise_with_puppet_configuration(error, puppet_configuration)
        raise
    except Exception as exc:
        raise_tool_error(
            create_error_response(
                ErrorCode.IMAGE_SERIALIZATION_FAILED,
                "Rendered dashboard images could not be packaged into the MCP response.",
                details=str(exc),
                context={
                    "capture_count": len(captures),
                    "render_path": target.render_path,
                    "puppet_configuration_applied": puppet_configuration,
                },
            )
        )
    raise AssertionError("unreachable: raise_tool_error always raises")


class DashboardScreenshotTools:
    """Opt-in dashboard screenshot tool (gated by enable_dashboard_screenshot)."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @tool(
        name="ha_get_dashboard_screenshot",
        tags={"Dashboard", "beta"},
        annotations={
            "destructiveHint": False,
            "title": "Get Dashboard Screenshot",
        },
    )
    @log_tool_usage
    async def ha_get_dashboard_screenshot(
        self,
        dashboard_path: Annotated[
            str | None,
            Field(
                description="Legacy Lovelace frontend path to render, e.g. "
                "'lovelace/0' (default dashboard, first view), "
                "'lovelace-home/kitchen', or 'my-dashboard'. Leading slash "
                "optional. Prefer dashboard_url_path + view_path for a stable "
                "named view. Mutually exclusive with dashboard_url_path."
            ),
        ] = None,
        dashboard_url_path: Annotated[
            str | None,
            Field(
                description="Stable dashboard URL path, e.g. 'lovelace-home' "
                "or 'default'. Use with view_path instead of dashboard_path."
            ),
        ] = None,
        view_path: Annotated[
            str | None,
            Field(
                description="Stable Lovelace views[].path value to render. "
                "Requires dashboard_url_path."
            ),
        ] = None,
        width: Annotated[
            int, Field(description="Viewport width in px.", ge=64, le=4096)
        ] = DEFAULT_WIDTH,
        height: Annotated[
            int | Literal["auto"],
            Field(
                description="Viewport height in px (64-4096), or 'auto' for "
                "content height. full_page=True is a compatibility alias for "
                "'auto'."
            ),
        ] = DEFAULT_HEIGHT,
        viewport_presets: Annotated[
            list[ViewportPreset] | None,
            JSON_STRING_COERCION,
            Field(
                description="Render one or more named responsive viewports in "
                "this order: mobile (390x844), tablet (768x1024), desktop "
                "(1280x800). Overrides width/height.",
                min_length=1,
                max_length=3,
            ),
        ] = None,
        orientation: Annotated[
            Orientation | None,
            Field(
                description="Optional responsive orientation. Swaps viewport "
                "dimensions when needed; this does not rotate final pixels."
            ),
        ] = None,
        zoom: Annotated[
            float, Field(description="Page zoom factor (1.0 = 100%).", ge=0.1, le=5.0)
        ] = 1.0,
        wait_ms: Annotated[
            int,
            Field(
                description="Extra render-settle time (ms) after the dashboard "
                "reports loaded. Raise it if a chart card (ApexCharts, "
                "mini-graph, history-graph) comes back blank.",
                ge=0,
                le=30000,
            ),
        ] = DEFAULT_WAIT_MS,
        full_page: Annotated[
            bool,
            Field(
                description=f"{FULL_PAGE_PARAM_DESC[:1].upper()}"
                f"{FULL_PAGE_PARAM_DESC[1:]}. Uses Puppet's native auto-height "
                "capture (currently capped at 4000 px); raise wait_ms for "
                "lazy cards."
            ),
        ] = False,
        theme: Annotated[
            str | None,
            Field(
                description="Installed Home Assistant frontend theme name. Puppet "
                "persists this selection on the frontend profile used by its token."
            ),
        ] = None,
        dark_mode: Annotated[
            bool,
            Field(
                description="Render the requested theme in dark mode. Puppet may "
                "persist the theme/dark preference on the profile used by its token."
            ),
        ] = False,
        language: Annotated[
            str | None,
            Field(description="Frontend language code, e.g. 'en' or 'de'."),
        ] = None,
        image_format: Annotated[
            ScreenshotFormat,
            Field(description="Image format: png, jpeg, webp, or bmp."),
        ] = "png",
        render_timeout_seconds: Annotated[
            float,
            Field(
                description="HTTP render timeout in seconds.",
                ge=1,
                le=300,
            ),
        ] = DEFAULT_RENDER_TIMEOUT_SECONDS,
        puppet_keep_browser_open: Annotated[
            bool | None,
            Field(
                description="Optional Puppet-only setting update. True keeps its "
                "Chromium session open between captures for faster repeat renders; "
                "False closes it after each capture. No add-on slug is accepted, "
                "and access_token/home_assistant_url cannot be changed here."
            ),
        ] = None,
        puppet_restart: Annotated[
            bool,
            Field(
                description="Restart only the discovered, schema-verified Puppet "
                "add-on after applying puppet_keep_browser_open. May also be used "
                "alone to restart Puppet; never targets another add-on."
            ),
        ] = False,
    ) -> ToolResult:
        """Get rendered images of a Home Assistant Lovelace dashboard view.

        When not to use: while reading or writing dashboard configuration, use
        ha_config_get_dashboard(include_screenshot=True) or
        ha_config_set_dashboard(return_screenshot=True) for a single workflow.

        Use it for repeatable visual checks, including ordered mobile, tablet,
        and desktop captures. Puppet reports image bytes but does not confirm
        that the frontend accepted a requested theme or language; structured
        metadata therefore records the values sent to the engine.

        Supplying Puppet management parameters without a dashboard target runs
        a management-only operation and returns no image. With a target, the
        schema-verified Puppet setting/restart is applied before capture.
        """
        management_requested = puppet_keep_browser_open is not None or puppet_restart
        management_only = (
            management_requested
            and dashboard_path is None
            and dashboard_url_path is None
            and view_path is None
        )
        puppet_configuration: dict[str, Any] | None = None
        if management_only:
            ignored = _management_only_ignored_options(
                width=width,
                height=height,
                viewport_presets=viewport_presets,
                orientation=orientation,
                zoom=zoom,
                wait_ms=wait_ms,
                full_page=full_page,
                theme=theme,
                dark_mode=dark_mode,
                language=language,
                image_format=image_format,
                render_timeout_seconds=render_timeout_seconds,
            )
            if ignored:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "Screenshot render options require a dashboard target.",
                        context={"ignored_screenshot_options": ignored},
                    )
                )
            from ..dashboard_screenshot.provision import configure_puppet_addon

            puppet_configuration = await configure_puppet_addon(
                self._client,
                keep_browser_open=puppet_keep_browser_open,
                restart=puppet_restart,
            )

            return ToolResult(
                content=[],
                structured_content={
                    "success": True,
                    "action": "configure_puppet",
                    "puppet_configuration": puppet_configuration,
                    "screenshot_count": 0,
                },
            )

        target = await resolve_dashboard_render_target(
            self._client,
            dashboard_path=dashboard_path,
            dashboard_url_path=dashboard_url_path,
            view_path=view_path,
        )
        validate_capture_parameters(
            width=width,
            height=height,
            viewport_presets=viewport_presets,
            orientation=orientation,
            zoom=zoom,
            wait_ms=wait_ms,
            full_page=full_page,
            theme=theme,
            dark_mode=dark_mode,
            language=language,
            image_format=image_format,
            render_timeout_seconds=render_timeout_seconds,
        )
        if management_requested:
            from ..dashboard_screenshot.provision import configure_puppet_addon

            puppet_configuration = await configure_puppet_addon(
                self._client,
                keep_browser_open=puppet_keep_browser_open,
                restart=puppet_restart,
            )
        try:
            capture_failures: list[dict[str, Any]] = []
            captures = await capture_dashboard_images(
                target.render_path,
                width=width,
                height=height,
                viewport_presets=viewport_presets,
                orientation=orientation,
                zoom=zoom,
                wait_ms=wait_ms,
                full_page=full_page,
                theme=theme,
                dark_mode=dark_mode,
                language=language,
                image_format=image_format,
                render_timeout_seconds=render_timeout_seconds,
                partial_failures=capture_failures,
            )
        except ToolError as exc:
            if puppet_configuration is not None:
                _raise_with_puppet_configuration(exc, puppet_configuration)
            raise
        except Exception as exc:
            if puppet_configuration is not None:
                _raise_with_puppet_configuration(exc, puppet_configuration)
            raise
        return _package_screenshot_result(
            captures=captures,
            target=target,
            puppet_configuration=puppet_configuration,
            capture_failures=capture_failures,
        )


def register_dashboard_screenshot_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register the dashboard screenshot tool when the feature flag is on.

    Set HAMCP_ENABLE_DASHBOARD_SCREENSHOT=true (beta) to enable. Mirrors the
    early-return gate used by register_filesystem_tools.
    """
    from ..config import get_global_settings

    if not get_global_settings().enable_dashboard_screenshot:
        logger.debug(
            "Dashboard screenshot tool disabled "
            "(set HAMCP_ENABLE_DASHBOARD_SCREENSHOT=true to enable)"
        )
        return

    register_tool_methods(mcp, DashboardScreenshotTools(client))
