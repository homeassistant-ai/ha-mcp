"""Core's own record of a flow in progress, for ``ha_mcp_tools/flow_record``.

Core's REST and WebSocket APIs answer a flow's step but not what the flow was
started for: its source, the entry a reconfigure flow edits, or the issue a
Repairs fix flow fixes. The flow manager holding the flow knows them, so a
server that is handed a ``flow_id`` it did not open itself (it restarted
since, or Core opened the flow, as it opens a reauth flow) reads the target
here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.data_entry_flow import FlowManager

WS_FLOW_RECORD = "ha_mcp_tools/flow_record"
# Advertised by websocket_api's info; without it the server continues only the
# flows it opened since it started.
CAPABILITY = "flow_record"
# Advertised once the ``repairs`` manager is served: an older component's
# schema refuses it.
REPAIRS_CAPABILITY = "flow_record_repairs"
# Wire name -> the attribute of ``hass.config_entries`` holding that manager.
_MANAGERS = {"config": "flow", "options": "options", "subentries": "subentries"}
_REPAIRS = "repairs"
MANAGERS = (*_MANAGERS, _REPAIRS)


def flow_manager(hass: HomeAssistant, name: str) -> FlowManager[Any, Any, Any] | None:
    if name == _REPAIRS:
        from homeassistant.components.repairs import repairs_flow_manager

        if _REPAIRS not in hass.config.components:
            return None
        return repairs_flow_manager(hass)
    manager: FlowManager[Any, Any, Any] = getattr(hass.config_entries, _MANAGERS[name])
    return manager


def flow_record(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    """The handler, source, entry and issue anchors of ``flow_id`` in ``manager``."""
    from homeassistant.data_entry_flow import UnknownFlow

    manager = flow_manager(hass, msg["manager"])
    if manager is None:  # Repairs is not set up, so it holds no flow.
        return {"found": False}
    try:
        flow = manager.async_get(msg["flow_id"])
    except UnknownFlow:
        return {"found": False}
    context = flow["context"]
    handler = flow["handler"]
    return {
        "found": True,
        # A subentry flow's handler is the pair (entry_id, subentry_type).
        "handler": list(handler) if isinstance(handler, tuple) else handler,
        "source": context.get("source"),
        "entry_id": context.get("entry_id"),
        "subentry_id": context.get("subentry_id"),
        "issue_id": context.get("issue_id"),
    }


def command_specs(vol: Any) -> list[tuple[dict[Any, Any], Any, Any]]:
    """The (schema, handler, async prep) row websocket_api registers."""
    return [
        (
            {
                vol.Required("type"): WS_FLOW_RECORD,
                vol.Required("manager"): vol.In(MANAGERS),
                vol.Required("flow_id"): str,
            },
            flow_record,
            None,
        )
    ]
