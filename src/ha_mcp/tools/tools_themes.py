"""Frontend theme management tools for Home Assistant.

Themes are YAML/file-based: Home Assistant itself exposes no API to create or
edit theme files. This module covers what CAN be managed at runtime - listing
the installed themes and selecting the backend default themes through Core's
``frontend.set_theme``. Theme files are edited with the file and YAML tools and
installed through HACS.
"""

import logging
from typing import Annotated, Any, Literal

from pydantic import Field

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool

from ..client.rest_client import (
    HomeAssistantAdminRequiredError,
    HomeAssistantCommandNotSent,
    HomeAssistantConnectionError,
)
from ..errors import ErrorCode, create_error_response
from .coercion import JSON_STRING_COERCION
from .helpers import (
    core_error_message,
    exception_to_structured_error,
    log_tool_usage,
    raise_core_refusal,
    raise_tool_error,
    register_tool_methods,
)
from .tool_hints import write_hints
from .util_helpers import summarize_theme_listing, websocket_error_message

logger = logging.getLogger(__name__)

ThemeAction = Literal["list", "set", "get_engine_theme", "set_engine_theme"]


class ThemesTools:
    """Frontend theme listing and selection tools."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def _engine_credential(self) -> Any:
        """Resolve the screenshot engine user's credential, or fail clearly.

        The engine writes its own token user's profile, which in a dedicated
        engine-account setup is NOT the account ha-mcp itself authenticates
        as -- so these actions cannot simply reuse ha-mcp's own client.
        """
        from ..config import get_global_settings
        from ..dashboard_screenshot.provision import resolve_engine
        from ..dashboard_screenshot.theme_guard import ThemeGuard

        explicit_url = (
            get_global_settings().dashboard_screenshot_engine_url or ""
        ).strip()
        engine_target = await resolve_engine()
        # An explicit URL is never provably paired with a credential. Even on
        # HA OS, _addon_credential_best_effort() hands back the DISCOVERED
        # Puppet app's credential without checking it identifies the engine
        # that URL points at -- so a sidecar URL plus a running app yields a
        # credential for the wrong account. That was a safe no-op while the
        # guard only wrote back its own snapshot; it is not safe now that
        # these actions read and write a profile on request.
        if explicit_url or engine_target.addon_credential is None:
            # An explicitly configured engine URL yields no addon_credential,
            # so the only fallback is ha-mcp's OWN credential -- which under
            # the dedicated-engine-account setup this feature recommends is a
            # different Home Assistant user. Acting on it would read and
            # overwrite the wrong profile while leaving the engine account
            # untouched. expected_current cannot save us: it compares values,
            # not identity, and two users sharing a theme compare equal.
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "The screenshot engine's Home Assistant account cannot be "
                    "identified, so its theme cannot be read or written.",
                    suggestions=[
                        (
                            "This applies whenever an engine URL is set "
                            + "explicitly: the engine's own token is not "
                            + "discoverable, and a credential discovered via "
                            + "the Supervisor is not provably that engine's."
                        ),
                        (
                            "Restore the theme from that account's own "
                            + "session: Profile > General in the Home "
                            + "Assistant UI."
                        ),
                    ],
                )
            )
        guard = ThemeGuard.for_capture(engine_target.addon_credential, self._client)
        if guard.credential is None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "No screenshot-engine credential is discoverable in this "
                    "deployment, so the engine account's theme cannot be read "
                    "or written.",
                    suggestions=[
                        "This action only applies where a screenshot engine "
                        "is configured.",
                    ],
                )
            )
        return guard.credential

    async def _get_engine_theme(self) -> dict[str, Any]:
        """Read the screenshot engine account's saved per-user theme."""
        from ..dashboard_screenshot.theme_guard import read_engine_theme

        credential = await self._engine_credential()
        return {"success": True, "data": {"theme": await read_engine_theme(credential)}}

    async def _set_engine_theme(
        self,
        action: str,
        value: dict[str, Any] | None,
        expected_current: dict[str, Any] | None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Restore the screenshot engine account's saved per-user theme."""
        from ..dashboard_screenshot.theme_guard import (
            ThemeChangedError,
            write_engine_theme,
        )

        if value is None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_MISSING_PARAMETER,
                    "value is required when action='set_engine_theme'",
                    context={"action": action},
                    suggestions=[
                        "Use the value quoted in the screenshot tool's "
                        + "warning, e.g. {'theme': '', 'dark': False}",
                    ],
                )
            )
        credential = await self._engine_credential()
        try:
            # expected_current=None is an explicit "expect no stored theme",
            # not an omission -- only force skips the guard.
            await write_engine_theme(
                credential, value, expected_current=expected_current, force=force
            )
        except ThemeChangedError as guard_error:
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "The engine account's saved theme is no longer the value "
                    "this restore expected, so it was not overwritten.",
                    context={
                        "action": action,
                        "expected_current": expected_current,
                        "actual_current": guard_error.actual,
                    },
                    suggestions=[
                        (
                            "Something else changed the theme since the "
                            + "warning was emitted -- confirm the intended "
                            + "value before retrying."
                        ),
                        (
                            "Re-read it with ha_manage_theme("
                            + "action='get_engine_theme')."
                        ),
                    ],
                )
            )
        return {"success": True, "data": {"theme": value}}

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

    async def _set_theme(
        self, theme_name: str | None, theme_name_dark: str | None
    ) -> dict[str, Any]:
        """Call ``frontend.set_theme`` over WebSocket, where Core's refusal keeps
        its text (REST answers it with a bare 400), then read back the defaults
        Core stored."""
        service_data = {
            key: value
            for key, value in (("name", theme_name), ("name_dark", theme_name_dark))
            if value is not None
        }
        context = {
            "action": "set",
            "theme_name": theme_name,
            "theme_name_dark": theme_name_dark,
        }
        list_hint = "Check with ha_manage_theme(action='list')"
        try:
            # Core refuses an unknown or missing theme as invalid_format: the
            # caller's input, answered below, not a server fault to log.
            result = await self._client.send_websocket_message(
                {
                    "type": "call_service",
                    "domain": "frontend",
                    "service": "set_theme",
                    "service_data": service_data,
                },
                expected_error_codes=frozenset({"invalid_format"}),
            )
        except HomeAssistantCommandNotSent:
            raise
        except HomeAssistantConnectionError as e:
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "frontend.set_theme got no answer, so whether the theme was "
                    f"set is unknown: {e}",
                    context={**context, "write_outcome_unknown": True},
                    suggestions=[f"{list_hint} before retrying"],
                )
            )
        if not result.get("success"):
            # Core refuses a non-admin with a generic error code; only the
            # text says the token lacks admin rights.
            if core_error_message(result.get("error", "")) == "Unauthorized":
                raise HomeAssistantAdminRequiredError(
                    "Service frontend.set_theme is admin-only in Home Assistant "
                    "and this token's user is not an administrator"
                )
            raise_core_refusal(
                "frontend.set_theme failed",
                result,
                context,
                [
                    "Core's 'name' is theme_name and 'name_dark' is theme_name_dark",
                    "Call ha_manage_theme(action='list') to see installed themes",
                ],
            )
        try:
            listing = await self._list_themes()
        except HomeAssistantConnectionError as e:
            raise_tool_error(
                create_error_response(
                    ErrorCode.CONNECTION_FAILED,
                    "frontend.set_theme was applied, but the defaults could "
                    f"not be re-read: {e}",
                    context=context,
                    suggestions=[list_hint],
                )
            )
        return {
            "success": True,
            "data": {
                "default_theme": listing["default_theme"],
                "default_dark_theme": listing["default_dark_theme"],
            },
        }

    @tool(
        name="ha_manage_theme",
        tags={"System"},
        annotations=write_hints(
            "Manage Frontend Themes",
            destructive=True,
            idempotent=True,
            open_world=False,
        ),
    )
    @log_tool_usage
    async def ha_manage_theme(
        self,
        action: Annotated[
            ThemeAction,
            Field(
                description=(
                    "Theme operation: 'list' installed themes, 'set' the "
                    "backend default themes, or read/restore the screenshot "
                    "engine account's own per-user theme with "
                    "'get_engine_theme' / 'set_engine_theme'."
                ),
            ),
        ],
        theme_name: Annotated[
            str | None,
            Field(
                description=(
                    "action='set': the default theme, sent as "
                    "frontend.set_theme's 'name'."
                ),
                default=None,
            ),
        ] = None,
        theme_name_dark: Annotated[
            str | None,
            Field(
                description=(
                    "action='set': the dark-mode default theme, sent as "
                    "frontend.set_theme's 'name_dark'; 'none' removes the "
                    "dark-mode override."
                ),
                default=None,
            ),
        ] = None,
        expected_current: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Guard for action='set_engine_theme': the stored theme is read "
                    "immediately before the write and the write is skipped if it no longer "
                    "equals this. Omitting this value or passing null both mean 'expect no "
                    "stored theme', enforced like any other value; the guard is always "
                    "applied unless force is set. Best-effort, not atomic: a change landing"
                    " between that read and the write is not caught."
                ),
                default=None,
            ),
        ] = None,
        force: Annotated[
            bool,
            Field(
                description=(
                    "action='set_engine_theme' only: skip the expected_current guard and "
                    "overwrite unconditionally."
                ),
                default=False,
            ),
        ] = False,
        value: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Frontend user-data theme object when "
                    "action='set_engine_theme', e.g. {'theme': '', "
                    "'dark': False}. An empty dict restores default/auto "
                    "behavior."
                ),
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Manage Home Assistant frontend themes.

        When NOT to use: themes are YAML files - Home Assistant has no API to
        create or edit them. Installing community themes goes through HACS
        (the Marketplace on 2026.11+, both via ha_manage_hacs); theme files
        are edited with ha_write_file or ha_config_set_yaml (beta), which
        reload themes after the edit. After any other change to theme files,
        ha_reload_core(target='themes') loads them.

        When to use: action='list' discovers installed theme names, the modes
        each theme declares and the current defaults; action='set' selects
        the backend default theme, the dark-mode one, or both in one call,
        and returns the defaults Home Assistant then holds. Home Assistant
        describes frontend.set_theme's fields in
        ha_list_services(domain='frontend', detail_level='full').

        SCREENSHOT-ENGINE ACTIONS (per-user, not the backend default):
        Taking a dashboard screenshot makes the Puppet engine write the saved
        theme of the Home Assistant user its token belongs to, which also
        flips that user's live web and mobile sessions. The screenshot tools
        are read-only and only *report* this; use action='set_engine_theme'
        with the value quoted in their warning to put it back, and
        action='get_engine_theme' to inspect it. These act on that engine
        account's per-user profile via frontend/set_user_data, which is a
        different layer from the backend default that action='set' changes.
        Giving the engine its own dedicated user and token avoids the issue
        entirely.

        Caveats: action='set' changes the backend-selected defaults only -
        users who explicitly picked a theme in their profile keep their
        choice.

        EXAMPLES:
        - List themes: ha_manage_theme(action="list")
        - Set default theme: ha_manage_theme(action="set", theme_name="nord")
        - Set light and dark defaults: ha_manage_theme(
              action="set", theme_name="nord", theme_name_dark="nord_dark")
        - Inspect the engine account's theme: ha_manage_theme(
              action="get_engine_theme")
        - Undo a screenshot's theme change (pass BOTH values from the
          warning, so a theme changed since then is not overwritten):
          ha_manage_theme(action="set_engine_theme",
              value={"theme": "", "dark": False},
              expected_current={"theme": "default", "dark": True})
        """
        try:
            if action == "list":
                return {"success": True, "data": await self._list_themes()}

            if action == "get_engine_theme":
                return await self._get_engine_theme()

            if action == "set_engine_theme":
                return await self._set_engine_theme(
                    action, value, expected_current, force
                )

            return await self._set_theme(theme_name, theme_name_dark)
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            exception_to_structured_error(
                e,
                context={
                    "action": action,
                    "theme_name": theme_name,
                    "theme_name_dark": theme_name_dark,
                },
                suggestions=[
                    "Call ha_manage_theme(action='list') to see installed themes",
                    "Verify the Home Assistant connection",
                ],
            )
            return None  # unreachable: exception_to_structured_error always raises


def register_themes_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register frontend theme management tools."""
    register_tool_methods(mcp, ThemesTools(client))
