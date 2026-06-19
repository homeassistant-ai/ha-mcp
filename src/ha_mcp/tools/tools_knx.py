"""
KNX project inspection tools for Home Assistant.

Exposes the group-address table that the KNX integration parses from an
uploaded ETS ``.knxproj`` file. The data is read through the
``knx/get_knx_project`` WebSocket command — the same command the official
KNX panel (knx-frontend) uses. See
``homeassistant/components/knx/websocket.py`` and ``project.py`` in
home-assistant/core for the upstream handler.

The command is admin-only and strictly read-only: it never touches the KNX
bus or the integration configuration. When the KNX integration is not loaded,
the upstream ``provide_knx`` decorator returns ``ERR_HOME_ASSISTANT_ERROR``
with the message ``"KNX integration not loaded."``; this tool maps that to a
clear ``COMPONENT_NOT_INSTALLED`` error. When the integration is loaded but no
project has been uploaded yet, ``project.get_knxproject()`` returns ``None``;
this tool maps that to an empty result with an explanatory note (mirroring the
"never configured" handling in ``tools_energy.py``).
"""

import logging
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.tools import tool

from ..errors import ErrorCode, create_error_response
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)

logger = logging.getLogger(__name__)

# Sentinel emitted by HA Core's ``provide_knx`` decorator when the KNX
# integration is not loaded. ``send_command`` wraps it as
# ``"Command failed: KNX integration not loaded."`` and
# ``send_websocket_message`` surfaces that string in the ``error`` field.
_KNX_NOT_LOADED_SENTINEL = "KNX integration not loaded."


def _is_knx_not_loaded_error(error_msg: str) -> bool:
    """Return True if the WebSocket error indicates the KNX integration is
    not loaded (as opposed to some other failure)."""
    return _KNX_NOT_LOADED_SENTINEL in error_msg


class KnxTools:
    """KNX project inspection tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @tool(
        name="ha_knx_get_project",
        tags={"KNX"},
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "title": "Get KNX Project",
        },
    )
    @log_tool_usage
    async def ha_knx_get_project(self) -> dict[str, Any]:
        """
        Get the KNX group addresses parsed from the uploaded ETS project.

        Returns the group-address table (address, name, DPT, description) plus
        the group-range hierarchy and project metadata, read via the
        ``knx/get_knx_project`` WebSocket command — the same data the KNX
        panel displays. This is the only way to inspect the parsed ``.knxproj``
        contents programmatically.

        The result's ``group_addresses`` is a map keyed by the group-address
        string (e.g. ``"1/2/3"``); each entry carries its ``name``, ``dpt``
        (``{"main", "sub"}`` or null), ``description`` and other ETS metadata.
        It contains no Home Assistant entity-assignment data — reconcile group
        addresses with configured entities client-side if needed.

        Read-only: this never modifies the KNX bus or configuration. Requires
        an admin token. If the KNX integration is not installed, this raises a
        COMPONENT_NOT_INSTALLED error. If the integration is loaded but no
        project has been uploaded, it returns an empty group-address map with
        an explanatory note.
        """
        try:
            result = await self._client.send_websocket_message(
                {"type": "knx/get_knx_project"}
            )

            if not result.get("success"):
                error_msg = str(result.get("error", ""))
                if _is_knx_not_loaded_error(error_msg):
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.COMPONENT_NOT_INSTALLED,
                            "KNX integration is not loaded.",
                            context={"tool": "ha_knx_get_project"},
                            suggestions=[
                                "Add the KNX integration in Home Assistant "
                                "(Settings → Devices & Services)",
                                "Restart Home Assistant after configuring KNX",
                            ],
                        )
                    )
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        f"Failed to get KNX project: {error_msg or 'Unknown error'}",
                        context={"tool": "ha_knx_get_project"},
                        suggestions=[
                            "Verify the token has admin privileges "
                            "(knx/get_knx_project is admin-only)",
                            "Check Home Assistant connection",
                        ],
                    )
                )

            project = result.get("result")
            if not project:
                # ``project.get_knxproject()`` returns ``None`` when no
                # ``.knxproj`` has been uploaded yet. Map to an empty result
                # with a note rather than raising, mirroring the energy tool's
                # "never configured" handling.
                return {
                    "success": True,
                    "count": 0,
                    "group_addresses": {},
                    "group_ranges": {},
                    "info": {},
                    "note": (
                        "The KNX integration is loaded but no ETS project has "
                        "been uploaded yet. Upload a .knxproj file via the KNX "
                        "panel to populate the group-address table."
                    ),
                }

            group_addresses = project.get("group_addresses", {})
            return {
                "success": True,
                "count": len(group_addresses),
                "group_addresses": group_addresses,
                "group_ranges": project.get("group_ranges", {}),
                "info": project.get("info", {}),
            }

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error getting KNX project: {e}")
            exception_to_structured_error(
                e,
                context={"tool": "ha_knx_get_project"},
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify WebSocket connection is active",
                    "Verify the token has admin privileges",
                ],
            )
            # ``exception_to_structured_error`` always raises (NoReturn); this
            # explicit raise makes the function's exit unambiguous (no implicit
            # ``return None`` fall-through) and is never reached at runtime.
            raise


def register_knx_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register KNX project inspection tools."""
    register_tool_methods(mcp, KnxTools(client))
