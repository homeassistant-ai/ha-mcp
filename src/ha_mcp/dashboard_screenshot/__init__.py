"""Dashboard screenshot support (opt-in, beta).

Renders a Home Assistant Lovelace dashboard view to a PNG via a separate,
single-purpose headless-Chromium screenshot engine (balloob's Puppet add-on,
or a docker-compose sidecar).

The heavy browser runtime lives entirely in that separate engine — this
package is a thin async HTTP client plus deployment-mode discovery, so the
default ha-mcp install stays lightweight unless the user opts in.
"""

from __future__ import annotations

from .capture import (
    DEFAULT_HEIGHT,
    DEFAULT_WAIT_MS,
    DEFAULT_WIDTH,
    capture_dashboard_png,
)
from .provision import resolve_engine_url

__all__ = [
    "DEFAULT_HEIGHT",
    "DEFAULT_WAIT_MS",
    "DEFAULT_WIDTH",
    "capture_dashboard_png",
    "resolve_engine_url",
]
