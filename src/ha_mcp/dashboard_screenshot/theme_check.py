"""Check a screenshot's requested theme against Home Assistant's own themes.

The engine renders an unknown theme name as the default theme and reports no
error, so the name is checked against Core's ``frontend/get_themes`` first.
"""

from __future__ import annotations

import logging
from typing import Any

from ..errors import ErrorCode, create_error_response
from ..tools.helpers import raise_tool_error
from ..tools.util_helpers import websocket_error_message

logger = logging.getLogger(__name__)

# The frontend's built-in theme; Core's theme list holds only installed ones.
_BUILT_IN_THEME = "default"


def _unchecked(theme: str, reason: str) -> list[str]:
    return [
        f"Home Assistant's theme list could not be read ({reason}), so theme "
        f"{theme!r} was not checked; an unknown name renders the default theme."
    ]


async def check_requested_theme(
    client: Any, theme: str | None, dark_mode: bool
) -> list[str]:
    """Refuse a theme Home Assistant does not have.

    Returns warnings for a theme list that could not be read and for a
    ``dark_mode`` the theme's modes override (the frontend renders a theme
    without a dark mode light, and one with only a dark mode dark).
    """
    if theme is None or client is None or theme == _BUILT_IN_THEME:
        return []
    try:
        result = await client.send_websocket_message({"type": "frontend/get_themes"})
    except Exception as exc:  # noqa: BLE001  # an optional pre-check must not fail the render
        logger.warning(
            "frontend/get_themes failed; theme %r not checked: %s", theme, exc
        )
        return _unchecked(theme, str(exc))
    if isinstance(result, dict) and result.get("success") is False:
        return _unchecked(theme, websocket_error_message(result.get("error")))
    listing = result.get("result") if isinstance(result, dict) else None
    themes = listing.get("themes") if isinstance(listing, dict) else None
    if not isinstance(themes, dict):
        return _unchecked(theme, "unexpected response")
    if theme not in themes:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Home Assistant has no theme named {theme!r}",
                context={"installed_themes": [_BUILT_IN_THEME, *themes]},
                suggestions=["Use one of installed_themes, or omit theme"],
            )
        )
    entry = themes[theme]
    modes = entry.get("modes") if isinstance(entry, dict) else None
    if not isinstance(modes, dict):
        modes = {}
    if dark_mode and "dark" not in modes:
        return [
            f"Theme {theme!r} defines no dark mode in Home Assistant, so the "
            "frontend renders it in light mode."
        ]
    if not dark_mode and modes and "light" not in modes:
        return [
            f"Theme {theme!r} defines only a dark mode in Home Assistant, so the "
            "frontend renders it in dark mode."
        ]
    return []
