"""Dashboard screenshot tool (opt-in, beta).

Gated behind ``enable_dashboard_screenshot``. Renders a Lovelace dashboard
view to a PNG via the separate screenshot engine add-on (or a docker-compose
sidecar) and returns it as an image for visual verification.

The companion ``include_screenshot`` / ``return_screenshot`` parameters on
``ha_config_get_dashboard`` / ``ha_config_set_dashboard`` share the same
capture path (see ``ha_mcp.dashboard_screenshot.capture``).
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastmcp.tools import tool
from fastmcp.utilities.types import Image
from pydantic import Field

from ..dashboard_screenshot.capture import (
    DEFAULT_HEIGHT,
    DEFAULT_WAIT_MS,
    DEFAULT_WIDTH,
    FULL_PAGE_PARAM_DESC,
    capture_dashboard_png,
)
from .helpers import log_tool_usage, register_tool_methods

logger = logging.getLogger(__name__)


class DashboardScreenshotTools:
    """Opt-in dashboard screenshot tool (gated by enable_dashboard_screenshot)."""

    @tool(
        name="ha_get_dashboard_screenshot",
        tags={"Dashboard", "beta"},
        annotations={
            "readOnlyHint": True,
            "idempotentHint": True,
            "title": "Get Dashboard Screenshot",
        },
    )
    @log_tool_usage
    async def ha_get_dashboard_screenshot(
        self,
        dashboard_path: Annotated[
            str,
            Field(
                description="Lovelace frontend path to render, e.g. "
                "'lovelace/0' (default dashboard, first view), "
                "'lovelace-home/kitchen', or 'my-dashboard'. Leading slash "
                "optional."
            ),
        ],
        width: Annotated[
            int, Field(description="Viewport width in px.", ge=64, le=4096)
        ] = DEFAULT_WIDTH,
        height: Annotated[
            int,
            Field(
                description="Viewport height in px. Ignored when "
                "full_page=True (the engine renders a tall page instead).",
                ge=64,
                le=4096,
            ),
        ] = DEFAULT_HEIGHT,
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
                f"{FULL_PAGE_PARAM_DESC[1:]}. Overrides 'height' with a tall "
                "render; raise 'wait_ms' for long dashboards so lazy cards "
                "finish painting."
            ),
        ] = False,
    ) -> Image:
        """Get a rendered PNG image of a Home Assistant Lovelace dashboard view.

        Use it to visually verify a dashboard you just created or edited
        (pair with ha_config_set_dashboard, or use its return_screenshot
        param for a one-call create-and-see). Charts render best-effort —
        raise wait_ms if a chart card is blank. Set full_page=True to capture
        content below the fold.
        """
        png = await capture_dashboard_png(
            dashboard_path,
            width=width,
            height=height,
            zoom=zoom,
            wait_ms=wait_ms,
            full_page=full_page,
        )
        return Image(data=png, format="png")


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

    register_tool_methods(mcp, DashboardScreenshotTools())
