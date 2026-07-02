"""Unified radio management tool: ``ha_manage_radio``.

One multi-modal tool for managing every radio Home Assistant speaks — Z-Wave
(zwave_js), Zigbee (ZHA), Matter, and Thread/OTBR. Read-only diagnostics also
live in ``ha_get_device`` and ``ha_get_system_health`` (next to the existing
Z-Wave/Zigbee surfaces); they are mirrored here so they are at hand mid-
management. This tool additionally performs writes those read tools do not, so
it is not a subset of either.

Per-radio logic lives in ``radio/<name>.py``; this module is the dispatcher.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from fastmcp.exceptions import ToolError
from fastmcp.tools import tool
from pydantic import Field

from ..errors import ErrorCode, create_error_response
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)
from .radio import matter as matter_handler
from .radio import thread as thread_handler
from .radio import zigbee as zigbee_handler
from .radio import zwave as zwave_handler
from .radio.base import confirm_required, require
from .util_helpers import JSON_STRING_COERCION

logger = logging.getLogger(__name__)

HANDLERS: dict[str, Any] = {
    "zwave": zwave_handler,
    "zigbee": zigbee_handler,
    "matter": matter_handler,
    "thread": thread_handler,
}


class RadioTools:
    """``ha_manage_radio`` and its dispatch helpers."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def _resolve_entity_device(self, entity_id: str) -> str:
        """Resolve an entity_id to its device_id via the entity registry."""
        result = await self._client.send_websocket_message(
            {"type": "config/entity_registry/list"}
        )
        if result.get("success"):
            for entry in result.get("result", []):
                if entry.get("entity_id") == entity_id and entry.get("device_id"):
                    return str(entry["device_id"])
        raise_tool_error(
            create_error_response(
                ErrorCode.ENTITY_NOT_FOUND,
                f"Entity '{entity_id}' not found or has no associated device",
                context={"entity_id": entity_id},
                suggestions=["Use ha_search() to find a valid entity_id"],
            )
        )
        raise AssertionError  # py/mixed-returns terminal: raise_tool_error is NoReturn

    @tool(
        name="ha_manage_radio",
        tags={"Radio Management", "Z-Wave", "Zigbee", "Matter", "Thread"},
        annotations={
            "destructiveHint": True,
            "idempotentHint": False,
            "title": "Manage Radios (Z-Wave / Zigbee / Matter / Thread)",
        },
    )
    @log_tool_usage
    async def ha_manage_radio(
        self,
        radio: Annotated[
            Literal["zwave", "zigbee", "matter", "thread"],
            Field(description="Which radio to manage."),
        ],
        action: Annotated[
            str,
            Field(
                description=(
                    "Operation to perform. Actions vary per radio; an unknown "
                    "action returns the supported list for that radio. Common: "
                    "'diagnostics', 'network_status', 'ping', 'add'/'commission', "
                    "'remove_device', 'reinterview'/'reconfigure', 'firmware_update'."
                )
            ),
        ],
        device_id: Annotated[
            str | None,
            Field(
                description="Target device (node) for node-scoped actions.",
                default=None,
            ),
        ] = None,
        entity_id: Annotated[
            str | None,
            Field(
                description="Resolve the device from this entity for node-scoped actions.",
                default=None,
            ),
        ] = None,
        params: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Action-specific parameters (e.g. code, pin, channel, "
                    "property, value). An unknown action returns that radio's "
                    "supported action list with one-line summaries."
                ),
                default=None,
            ),
        ] = None,
        confirm: Annotated[
            bool,
            Field(
                description="Required (True) to run destructive actions.", default=False
            ),
        ] = False,
    ) -> dict[str, Any]:
        """Manage Home Assistant radios — Z-Wave, Zigbee, Matter, and Thread.

        For read-only inspection prefer ha_get_device / ha_get_system_health,
        which mirror the 'diagnostics' and 'network_status' actions; use this
        tool for writes and the active 'ping' probe (unique to this tool). Write
        actions perform inclusion/commissioning, removal, healing,
        reconfiguration, firmware updates and credential provisioning.

        Caveats: destructive actions (e.g. remove_device, network restore,
        change_channel, hard_reset, remove_fabric) require confirm=True.
        Long-running actions (inclusion, rebuild routes, firmware) start the
        operation and return immediately with long_running=true; completion
        happens out-of-band. Interactive Z-Wave S2 secure inclusion (read-the-
        PIN pairing) is not scriptable — use SmartStart/QR provisioning here or
        the HA UI.
        """
        try:
            handler = HANDLERS[radio]
            spec = handler.SUPPORTED.get(action)
            if spec is None:
                supported = sorted(handler.SUPPORTED)
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"Unknown action '{action}' for radio '{radio}'",
                        context={
                            "radio": radio,
                            "action": action,
                            "supported": supported,
                        },
                        # Surface each action's one-line summary so the caller
                        # can pick the right one without another round-trip.
                        suggestions=[
                            f"{name}: {handler.SUPPORTED[name].summary}"
                            for name in supported
                        ],
                    )
                )

            args: dict[str, Any] = dict(params or {})
            if device_id:
                args.setdefault("device_id", device_id)
            if entity_id and not args.get("device_id"):
                args["device_id"] = await self._resolve_entity_device(entity_id)

            require(args, spec, radio, action)
            if spec.destructive and not confirm:
                confirm_required(radio, action)

            result: dict[str, Any] = await handler.handle(self._client, action, args)
            # ActionSpec is the single source of truth for long-running; fill it
            # in even on branches that did not set it (e.g. force-remove paths).
            if spec.long_running:
                result.setdefault("long_running", True)
            return result

        except ToolError:
            raise
        except Exception as e:
            # exception_to_structured_error owns logging (it stays quiet for
            # classified errors and logs unclassified ones with a traceback);
            # a manual log here would double-log. Let the helper own it.
            exception_to_structured_error(e, context={"radio": radio, "action": action})
            return None  # unreachable: exception_to_structured_error always raises


def register_radio_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register the radio management tool (auto-discovered by the registry)."""
    register_tool_methods(mcp, RadioTools(client))
