"""Dashboard screenshot support (opt-in, beta).

Renders Home Assistant Lovelace dashboard views via a separate,
single-purpose headless-Chromium screenshot engine (balloob's Puppet add-on,
or a docker-compose sidecar).

The heavy browser runtime lives entirely in that separate engine — this
package is a thin async HTTP client plus deployment-mode discovery, so the
default ha-mcp install stays lightweight unless the user opts in.
"""

from __future__ import annotations

from .capture import (
    DEFAULT_HEIGHT,
    DEFAULT_RENDER_TIMEOUT_SECONDS,
    DEFAULT_WAIT_MS,
    DEFAULT_WIDTH,
    DashboardImageCapture,
    capture_dashboard_images,
)
from .provision import EngineTarget, resolve_engine
from .theme_guard import EngineCredential, ThemeGuard

__all__ = [
    "DEFAULT_HEIGHT",
    "DEFAULT_RENDER_TIMEOUT_SECONDS",
    "DEFAULT_WAIT_MS",
    "DEFAULT_WIDTH",
    "DashboardImageCapture",
    "EngineCredential",
    "EngineTarget",
    "ThemeGuard",
    "capture_dashboard_images",
    "resolve_engine",
]
