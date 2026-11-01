"""The reconfigure preview reads the integration's reconfigure form.

The preview leg of ``ha_set_integration(reconfigure=True)`` starts the
official reconfigure flow, describes its first step the way
``ha_get_integration(include_schema=True)`` describes a form (fields, current
values, Home Assistant's help text) and aborts it, so nothing is submitted.
The caller's config keys are compared with that form: a key it does not
declare may belong to a later form, unless the form is the flow's last.
"""

from __future__ import annotations

from typing import Any

from ..errors import ErrorCode, create_error_response
from .config_entry_flow import _abort_flow_best_effort
from .config_entry_flow_errors import FlowRef, ReconfigureStatus, abort_reason_text
from .config_entry_flow_form import _RESERVED_CONFIG_KEYS, _extract_schema_field_names
from .config_entry_flow_introspect import menu_choices
from .config_entry_flow_walker import _RECONFIGURE_SUCCESS_REASONS
from .config_helpers.describe import describe_form, later_forms
from .helpers import raise_tool_error


def _declared_keys(schema: Any) -> set[str]:
    """Names a config may use: leaf fields, and a section's own name for a
    section passed as one dict."""
    names = _extract_schema_field_names(schema) or set()
    for field in schema if isinstance(schema, list) else []:
        if (
            isinstance(field, dict)
            and isinstance(field.get("schema"), list)
            and field.get("name") is not None
        ):
            names.add(str(field["name"]))
    return names


async def read_reconfigure_form(
    client: Any, *, entry_id: str, domain: str, config: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Describe the reconfigure flow's first step; return it and key warnings.

    Refuses the preview when Home Assistant aborts the flow at once, or when
    ``config`` names keys the flow's last form does not declare.
    """
    step = await client.start_reconfigure_flow(domain, entry_id)
    flow = FlowRef(domain, "config")
    if step.get("type") == "abort":
        reason = step.get("reason")
        # Core aborts with a success reason only after it updated and
        # reloaded the entry, without any value from the caller.
        applied = reason in _RECONFIGURE_SUCCESS_REASONS
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Home Assistant finished the {domain} reconfigure flow at its "
                f"start ({reason}): the entry may have been updated, but not "
                "with your config."
                if applied
                else f"Home Assistant aborted the {domain} reconfigure flow at "
                f"its start: {reason}",
                context={
                    "entry_id": entry_id,
                    "domain": domain,
                    "status": ReconfigureStatus.APPLIED_BUT_UNVERIFIED
                    if applied
                    else ReconfigureStatus.FLOW_ABORTED_BEFORE_APPLY,
                    "reason": reason,
                    **await abort_reason_text(client, flow, step),
                },
            )
        )
    form: dict[str, Any] = {
        "flow_type": step.get("type"),
        "step_id": step.get("step_id"),
    }
    try:
        if step.get("type") == "menu":
            form["menu_options"] = menu_choices(step)
            form["note"] = (
                "The flow opens with a menu: pass the option as next_step_id "
                "in config. The form behind it is checked when you confirm."
            )
        elif step.get("type") == "form":
            warnings: list[str] = []
            form["fields"] = await describe_form(
                client, domain, flow.path, step, warnings
            )
            form.update(later_forms(step))
            if warnings:
                form["warnings"] = warnings
    finally:
        await _abort_flow_best_effort(client, step.get("flow_id"))
    if step.get("type") != "form":
        return form, []

    declared = _declared_keys(step.get("data_schema")) | _RESERVED_CONFIG_KEYS
    unknown = sorted(key for key in config if key not in declared)
    if not unknown:
        return form, []
    if step.get("last_step") is True:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"{domain}'s reconfigure form does not declare "
                f"{', '.join(unknown)}, and it is the flow's last form, so "
                "these keys would not be applied.",
                suggestions=["Use the field names in reconfigure_form.fields"],
                context={
                    "entry_id": entry_id,
                    "domain": domain,
                    "status": ReconfigureStatus.FLOW_ABORTED_BEFORE_APPLY,
                    "unknown_config_keys": unknown,
                    "reconfigure_form": form,
                },
            )
        )
    return form, [
        f"The first reconfigure form ({step.get('step_id')}) does not declare "
        f"{', '.join(repr(k) for k in unknown)}. A later form may; a key no "
        "form declares is not applied on confirm."
    ]
