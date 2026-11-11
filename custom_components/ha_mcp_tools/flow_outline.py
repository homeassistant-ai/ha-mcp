"""The steps Core defines for a schema-based flow, for ``ha_mcp_tools/flow_outline``.

Core's flow endpoints answer one step at a time, so a form after the first is
only seen by answering the ones before it. Most helper flows are declared as
a mapping of steps (``SchemaConfigFlowHandler.config_flow`` and
``options_flow``), which this command reads from the loaded handler class on
every call and serializes the way Core serializes a form it shows. A form or
a next step that Core computes from the answers given so far is reported as
such, not evaluated. No flow is started and nothing here is a copy: a Core
update changes the answer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .helper_collections import _convert, _optional_attr

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

WS_FLOW_OUTLINE = "ha_mcp_tools/flow_outline"
# Advertised by websocket_api's info; without it the server describes only
# the form a flow is at.
CAPABILITY = "flow_outline"
# What Core's flow view adds to a serialized form; absent before Core 2026.9.
_ADD_VISIBLE_CONDITIONS = _optional_attr(
    "homeassistant.data_entry_flow", "add_visible_conditions_to_serialized_schema"
)


def _fields(schema: Any) -> list[dict[str, Any]]:
    fields = _convert(schema)
    if _ADD_VISIBLE_CONDITIONS is not None:
        _ADD_VISIBLE_CONDITIONS(schema, fields)
    return fields


def _step(scf: Any, step_id: Any, step: Any) -> dict[str, Any]:
    """One step as Core declares it. ``*_from_answers`` marks what Core
    computes when the flow runs."""
    out: dict[str, Any] = {"step_id": str(step_id)}
    if isinstance(step, scf.SchemaFlowMenuStep):
        out["type"] = "menu"
        if callable(step.options):
            out["options_from_answers"] = True
        else:
            out["menu_options"] = [str(option) for option in step.options]
        return out
    out["type"] = "form"
    # A schema object is callable too; only it has the ``schema`` it wraps.
    if step.schema is None or hasattr(step.schema, "schema"):
        # None: Core shows no form at this step and moves on.
        out["data_schema"] = None if step.schema is None else _fields(step.schema)
    else:
        out["schema_from_answers"] = True
    if callable(step.next_step):
        out["next_from_answers"] = True
    else:
        # None: the flow finishes after this step.
        out["next_step"] = step.next_step
    return out


async def async_flow_outline(
    hass: HomeAssistant, msg: dict[str, Any]
) -> dict[str, Any]:
    """The declared steps of ``domain``'s config or options flow, in Core's
    order; ``supported`` is false for a flow that is not declared that way."""
    from homeassistant import config_entries, loader
    from homeassistant.helpers import schema_config_entry_flow as scf

    domain = msg["domain"]
    try:
        integration = await loader.async_get_integration(hass, domain)
        await integration.async_get_platform("config_flow")
    except loader.IntegrationNotFound, ImportError:
        return {"supported": False}
    handler = config_entries.HANDLERS.get(domain)
    if handler is None or not issubclass(handler, scf.SchemaConfigFlowHandler):
        return {"supported": False}
    steps = handler.config_flow if msg["flow"] == "config" else handler.options_flow
    if not steps:
        return {"supported": False}
    return {
        "supported": True,
        "steps": [_step(scf, step_id, step) for step_id, step in steps.items()],
    }


def command_specs(vol: Any) -> list[tuple[dict[Any, Any], Any, Any]]:
    """The (schema, handler, async prep) row websocket_api registers."""

    async def prep(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
        return {"result": await async_flow_outline(hass, msg)}

    def do_flow_outline(
        hass: HomeAssistant, msg: dict[str, Any], *, result: dict[str, Any]
    ) -> dict[str, Any]:
        return result

    return [
        (
            {
                vol.Required("type"): WS_FLOW_OUTLINE,
                vol.Required("domain"): str,
                vol.Required("flow"): vol.In(("config", "options")),
            },
            do_flow_outline,
            prep,
        )
    ]
