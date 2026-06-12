"""Frontend theme management tools for Home Assistant.

Themes are YAML/file-based: Home Assistant itself exposes no API to create or
edit theme files. This module covers what CAN be managed at runtime - listing
the installed themes and selecting the backend default theme. Creating or
editing custom theme files goes through ha_config_set_yaml (beta, ha-mcp
custom component); installing community themes goes through HACS.
"""

import logging
from typing import Annotated, Any, Literal

from fastmcp.exceptions import ToolError
from fastmcp.tools import tool
from pydantic import Field

from ..client.rest_client import HomeAssistantAPIError
from ..errors import ErrorCode, create_error_response
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)
from .util_helpers import summarize_theme_listing, websocket_error_message

logger = logging.getLogger(__name__)

ThemeAction = Literal["list", "set"]


class ThemesTools:
    """Frontend theme listing and selection tools."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def _list_themes(self) -> dict[str, Any]:
        """Fetch installed theme names and defaults via websocket."""
        result = await self._client.send_websocket_message(
            {"type": "frontend/get_themes"}
        )
        if not result.get("success"):
            error_msg = websocket_error_message(result.get("error", "Operation failed"))
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Failed to list themes: {error_msg}",
                    context={"action": "list"},
                )
            )
        return summarize_theme_listing(result.get("result") or {})

    @tool(
        name="ha_manage_theme",
        tags={"System"},
        annotations={
            "destructiveHint": True,
            "idempotentHint": True,
            "readOnlyHint": False,
            "title": "Manage Frontend Themes",
        },
    )
    @log_tool_usage
    async def ha_manage_theme(
        self,
        action: Annotated[
            ThemeAction,
            Field(
                description=(
                    "Theme operation: list installed themes or set the default theme."
                ),
            ),
        ],
        theme_name: Annotated[
            str | None,
            Field(
                description=(
                    "Theme name when action='set'. Must be an installed theme; "
                    "'default' restores the built-in theme, 'none' resets the "
                    "chosen mode to the built-in default."
                ),
                default=None,
            ),
        ] = None,
        mode: Annotated[
            Literal["light", "dark"] | None,
            Field(
                description=(
                    "Which mode the theme applies to when action='set'. "
                    "Defaults to light."
                ),
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Manage Home Assistant frontend themes.

        When NOT to use: themes are YAML files - Home Assistant has no API to
        create or edit them. Installing community themes goes through HACS
        (ha_manage_hacs); editing custom theme files goes through
        ha_config_set_yaml (beta, edits themes/<name>.yaml keyed by theme name
        and reloads themes automatically).

        When to use: action='list' discovers installed theme names and the
        current defaults; action='set' selects the backend default theme
        (optionally per light/dark mode).

        Caveats: action='set' changes the backend-selected default only -
        users who explicitly picked a theme in their profile keep their
        choice. Theme names are validated by Home Assistant at call time.

        EXAMPLES:
        - List themes: ha_manage_theme(action="list")
        - Set default theme: ha_manage_theme(action="set", theme_name="nord")
        - Set dark-mode theme: ha_manage_theme(
              action="set", theme_name="nord", mode="dark")
        - Restore built-in default: ha_manage_theme(
              action="set", theme_name="default")
        """
        try:
            if action == "list":
                return {"success": True, "data": await self._list_themes()}

            if not theme_name:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_MISSING_PARAMETER,
                        "theme_name is required when action='set'",
                        context={"action": action},
                        suggestions=[
                            "Call ha_manage_theme(action='list') to see "
                            "installed themes",
                        ],
                    )
                )

            service_data: dict[str, Any] = {"name": theme_name}
            if mode is not None:
                service_data["mode"] = mode
            try:
                await self._client.call_service("frontend", "set_theme", service_data)
            except HomeAssistantAPIError as e:
                # HA's REST layer collapses the service's validation detail
                # ("Theme X not found") into a bare 400 - name the theme here.
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        f"Failed to set theme '{theme_name}': {e}. "
                        "The theme may not be installed.",
                        context={
                            "action": action,
                            "theme_name": theme_name,
                            "mode": mode,
                        },
                        suggestions=[
                            "Call ha_manage_theme(action='list') to see "
                            "installed themes",
                        ],
                    )
                )

            # Re-read the defaults so the agent sees the effective state.
            listing = await self._list_themes()
            return {
                "success": True,
                "data": {
                    "theme": theme_name,
                    # HA applies the theme to light mode when mode is omitted.
                    "mode": mode or "light",
                    "default_theme": listing.get("default_theme"),
                    "default_dark_theme": listing.get("default_dark_theme"),
                },
            }
        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={"action": action, "theme_name": theme_name, "mode": mode},
                suggestions=[
                    "Call ha_manage_theme(action='list') to see installed themes",
                    "Verify the Home Assistant connection",
                ],
            )
            return None  # unreachable: exception_to_structured_error always raises


def register_themes_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register frontend theme management tools."""
    register_tool_methods(mcp, ThemesTools(client))
