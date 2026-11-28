"""Fail a test whose tool answered from its no-component fallback (#2632).

Where the ``ha_mcp_tools`` component answers, a tool that takes its REST,
WebSocket or proxy route has skipped or lost its bridge. The route still
returns data, so only the warning it carries shows it.
"""

from __future__ import annotations

from typing import Any

from ha_mcp.tools.component_api import COMPONENT_VIA_MARKETPLACE

from .topology import component_surface_available

# component_api.component_unasked_warning has no shared ending to match on.
_UNASKED = "ha_mcp_tools component could not be asked"


def component_fallback_warnings(result: dict[str, Any]) -> list[str]:
    """The warnings saying the component's part of ``result`` is missing."""
    return [
        warning
        for warning in result.get("warnings", [])
        if COMPONENT_VIA_MARKETPLACE in warning or _UNASKED in warning
    ]


def assert_component_path(result: dict[str, Any]) -> None:
    """On a lane with the component, ``result`` must not come from a fallback."""
    if not component_surface_available():
        return
    fallbacks = component_fallback_warnings(result)
    assert not fallbacks, (
        "The component answers on this lane, but the tool used its fallback: "
        f"{fallbacks}"
    )
