"""The options and subentry flow schemas of ha_get_integration, and the
subentry form of ha_set_integration(describe=True).

A schema only exists inside an open flow: the entry's options or subentry
flow is started, its form described in the same compact shape the helper
describe uses (fields, HA's help text, current values), and the flow
aborted. A flow that opens with a menu lists its
branches; ``menu_choice`` (ha_set_integration only) submits one of them, which
runs that step in Home Assistant, and describes the form behind it. A flow
Home Assistant aborts is reported with Core's reason text. A step without a
form fails the call.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, NoReturn

from ha_mcp._vendor.fastmcp.exceptions import ToolError

from ..errors import ErrorCode, create_error_response
from ..redaction import (
    harvest_secret_strings,
    is_password_flow_field,
    redact_options_by_flow_schema,
    redaction_enabled,
    sentinel_replacement,
)
from .config_entry_flow_errors import (
    FlowRef,
    abort_reason_text,
    progress_action_text,
)
from .config_entry_flow_form import iter_schema_fields
from .config_entry_flow_introspect import abort_probe_flow, menu_choices
from .config_helpers.describe import describe_form
from .helpers import raise_tool_error, validate_identifier_not_empty

logger = logging.getLogger(__name__)

_Submit = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def options_from_form_flow(flow: dict[str, Any]) -> dict[str, Any]:
    """Extract ``{field_name: current_value}`` from a form-type OptionsFlow.

    Reads each ``data_schema`` entry's ``description.suggested_value``
    first: HA's ``add_suggested_values_to_schema`` injects the entry's
    *persisted* option there (voluptuous renders ``suggested_value=...``
    into the ``description`` sub-object, not as a top-level field key),
    and it is what the HA UI renders as the current value. Falls back to
    ``default`` (the static schema default a brand-new form would show)
    and then ``value`` (constant-type fields ship ``value`` instead of
    ``default``). A field can carry both ``suggested_value`` and
    ``default`` at once — e.g. a group helper's ``hide_members`` stored as
    ``True`` over a schema default of ``False`` — and the stored value
    must win (issue #1575). Nested section fields are flattened into the
    returned top-level map. Fields with a missing or ``None`` value are skipped.
    """
    out: dict[str, Any] = {}
    # Defensive: HA should always return a list of dict fields, but guard
    # against malformed shapes so a bad response degrades to {} instead of
    # raising AttributeError (e.g. a string data_schema would iterate chars).
    data_schema = flow.get("data_schema")
    if not isinstance(data_schema, list):
        return out
    redact = redaction_enabled()
    for field in iter_schema_fields(data_schema):
        name = field.get("name")
        if name is None:
            continue
        value = None
        description = field.get("description")
        if isinstance(description, dict):
            value = description.get("suggested_value")
        if value is None:
            value = field.get("default", field.get("value"))
        if value is not None:
            if redact and is_password_flow_field(field):
                # Password-marked field (issue #2157): remember the live
                # value(s) for the global known-value scrub, then emit
                # set/empty sentinel(s) instead of the value itself
                # (multiple-value selectors keep their list shape).
                harvest_secret_strings(value)
                out[name] = sentinel_replacement(value)
            else:
                out[name] = value
    return out


async def _options_form_schema(client: Any, entry_id: Any) -> list[Any] | None:
    """The options flow's first-form schema, or None when it cannot be read."""
    data_schema: list[Any] | None = None
    flow_id = None
    try:
        flow = await client.start_options_flow(entry_id)
        flow_id = flow.get("flow_id")
        if flow.get("type") == "form":
            # An empty/missing schema on a form (e.g. a confirm-only
            # step) classifies nothing — persisted options can outlive
            # fields removed from the flow, so treat it as unreadable
            # and take the fail-closed branch instead of "redacting"
            # zero fields.
            candidate = flow.get("data_schema")
            if isinstance(candidate, list) and candidate:
                data_schema = candidate
    except Exception as exc:
        logger.warning(
            "redact_secrets: options-flow probe failed for %s: %r",
            entry_id,
            exc,
            exc_info=True,
        )
    finally:
        if flow_id:
            await abort_probe_flow(client.abort_options_flow, flow_id)
    return data_schema


async def redact_component_options(
    client: Any, entry_id: Any, entry: dict[str, Any], warnings: list[str]
) -> None:
    """Redact password-marked fields on component-served options (#2157).

    The component row carries raw persisted options with no schema, so
    the password markers must come from a live options-flow probe. When
    the entry supports an options flow but no form schema could be read
    (probe failed, or the flow opens with a menu), every option value is
    replaced with a set/empty sentinel — without the schema the password
    fields cannot be told apart, so this fails closed. Entries without
    an options flow carry no password markers anywhere (the documented
    metadata limit of the toggle); their options pass through and the
    known-value scrub remains the only line for them.
    """
    options = entry.get("options")
    if not isinstance(options, dict) or not options:
        return
    if not entry.get("supports_options"):
        return
    data_schema = await _options_form_schema(client, entry_id)
    if isinstance(data_schema, list):
        entry["options"] = redact_options_by_flow_schema(options, data_schema)
    else:
        entry["options"] = {
            key: sentinel_replacement(value) for key, value in options.items()
        }
        warnings.append(
            f"redact_secrets: no options schema readable for {entry_id} — "
            "every option value was redacted conservatively (the password "
            "fields cannot be told apart without the schema)"
        )


def _entry_domain(entry: dict[str, Any] | None) -> str | None:
    # None, never "": flow texts (errors, aborts, progress) then read the
    # domain by entry_id; describe_form shows no help text without one.
    domain = (entry or {}).get("domain")
    return domain if isinstance(domain, str) and domain else None


def _refuse_menu_choice(menu_choice: str, why: str, **context: Any) -> NoReturn:
    raise_tool_error(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            f"menu_choice {menu_choice!r} does not apply: {why}.",
            context={"menu_choice": menu_choice, **context},
            suggestions=["Drop menu_choice, or pass one of menu_options"],
        )
    )


def _put_payload(resp: dict[str, Any], key: str, payload: dict[str, Any]) -> None:
    """``payload`` as ``resp[key]``, its ``warnings`` moved to the response's."""
    if warnings := payload.pop("warnings", None):
        resp.setdefault("warnings", []).extend(warnings)
    resp[key] = payload


async def _describe_step(
    client: Any,
    step: dict[str, Any],
    *,
    flow: FlowRef,
    menu_choice: str | None,
    submit: _Submit,
) -> dict[str, Any]:
    """Describe a flow's first step, or the form behind ``menu_choice``.

    A step that is not a form, menu or abort raises; the callers abort the flow.
    """
    chosen: dict[str, Any] = {}
    if step.get("type") == "menu":
        choices = menu_choices(step)
        if menu_choice is None:
            return {
                "flow_type": "menu",
                "step_id": step.get("step_id"),
                "menu_options": choices,
                "note": "Pass one as menu_choice to ha_set_integration("
                "describe=True) to describe its form.",
            }
        if menu_choice not in choices:
            _refuse_menu_choice(
                menu_choice, "the flow's menu does not offer it", menu_options=choices
            )
        step = await submit(step["flow_id"], {"next_step_id": menu_choice})
        chosen = {"menu_choice": menu_choice}
    elif menu_choice is not None and step.get("type") not in ("abort", "create_entry"):
        # An abort or a finished flow is reported below, not hidden by this.
        _refuse_menu_choice(
            menu_choice, f"the flow opens with a {step.get('type')}, not a menu"
        )
    step_type = step.get("type")
    if step_type == "abort":
        return {
            "flow_type": "abort",
            **chosen,
            "aborted": step.get("reason"),
            **await abort_reason_text(client, flow, step),
        }
    if step_type == "menu":
        return {
            "flow_type": "menu",
            "step_id": step.get("step_id"),
            **chosen,
            "menu_options": menu_choices(step),
        }
    if step_type != "form":
        progress: dict[str, Any] = {}
        if step_type == "progress":
            progress = {
                "progress_action": step.get("progress_action"),
                **await progress_action_text(client, flow, step),
            }
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"The flow reached a {step_type!r} step, which has no form to "
                "describe; the flow is being aborted",
                suggestions=[
                    "Run this flow in the Home Assistant UI "
                    "(Settings > Devices & Services)."
                ],
                context={"step_id": step.get("step_id"), **chosen, **progress},
            )
        )
    return {
        "flow_type": "form",
        "step_id": step.get("step_id"),
        **chosen,
        **await describe_form(client, flow.domain, flow.path, step),
    }


async def fetch_options_schema(
    client: Any,
    entry_id: str,
    resp: dict[str, Any],
    *,
    populate_options: bool = True,
) -> None:
    """Describe the entry's options flow as ``resp["options_schema"]``.

    Also populates ``resp["entry"]["options"]`` from a first-step form so
    callers requesting both schema and options don't pay for two round-trips.
    Pass ``populate_options=False`` on the component-served path, where
    ``options`` already carry the raw persisted values and must NOT be
    overwritten by the OptionsFlow-derived suggested-value shape.
    """
    entry = resp.get("entry") if isinstance(resp.get("entry"), dict) else None
    try:
        flow_result = await client.start_options_flow(entry_id)
        flow_id = flow_result.get("flow_id")
        try:
            if flow_result.get("type") == "form" and entry is not None:
                if populate_options:
                    entry["options"] = options_from_form_flow(flow_result)
                elif redaction_enabled():
                    # Component-served path: options carry raw persisted
                    # values (the component only scrubs resolved !secret
                    # references). Now that the flow schema is in hand,
                    # redact the fields it marks as passwords.
                    entry["options"] = redact_options_by_flow_schema(
                        entry.get("options"), flow_result.get("data_schema", [])
                    )
            elif populate_options and entry is not None:
                # A branch form behind a menu holds that branch's fields, not
                # the entry's options.
                resp.setdefault("warnings", []).append(
                    f"options for {entry_id} could not be read: the options "
                    f"flow opens with a {flow_result.get('type')!r} step, not a "
                    "form, so empty 'options' does not mean the entry has none"
                )
            _put_payload(
                resp,
                "options_schema",
                await _describe_step(
                    client,
                    flow_result,
                    flow=FlowRef(_entry_domain(entry), "options", entry_id),
                    menu_choice=None,
                    submit=client.submit_options_flow_step,
                ),
            )
        finally:
            if flow_id:
                await abort_probe_flow(client.abort_options_flow, flow_id)
    except ToolError:
        raise
    except Exception as schema_err:
        logger.warning(
            f"Failed to fetch options schema for {entry_id}: "
            f"{type(schema_err).__name__}: {schema_err}",
            exc_info=True,
        )
        schema_warnings: list[str] = resp.setdefault("warnings", [])
        schema_warnings.append(
            f"options schema probe failed for {entry_id}: "
            f"{type(schema_err).__name__}: {schema_err} — 'options_schema' is "
            "missing and 'options' may be incomplete"
        )


async def fetch_config_subentry_schema(
    client: Any,
    entry_id: str,
    resp: dict[str, Any],
    *,
    subentry_type: str | None,
    subentry_id: str | None,
    show_advanced_options: bool,
    menu_choice: str | None = None,
) -> None:
    """Describe a config subentry flow as ``resp["subentry_schema"]``."""
    if not subentry_type:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "subentry_type is required to describe a subentry flow",
                suggestions=[
                    "Call ha_get_integration(entry_id=..., "
                    "include_subentries=True) to inspect existing subentry "
                    "types, then retry with subentry_type.",
                ],
                context={"entry_id": entry_id},
            )
        )

    entry = resp.get("entry") if isinstance(resp.get("entry"), dict) else None
    flow_result = await client.start_config_subentry_flow(
        entry_id,
        subentry_type,
        subentry_id=subentry_id,
        show_advanced_options=show_advanced_options,
    )
    flow_id = flow_result.get("flow_id")
    try:
        _put_payload(
            resp,
            "subentry_schema",
            {
                "subentry_type": subentry_type,
                "subentry_id": subentry_id,
                **await _describe_step(
                    client,
                    flow_result,
                    flow=FlowRef(
                        _entry_domain(entry),
                        f"config_subentries.{subentry_type}",
                        entry_id,
                    ),
                    menu_choice=menu_choice,
                    submit=client.submit_config_subentry_flow_step,
                ),
            },
        )
    finally:
        if flow_id:
            await abort_probe_flow(client.abort_config_subentry_flow, flow_id)


async def describe_flow_form(
    client: Any,
    *,
    describe: bool,
    domain: str | None,
    entry_id: str | None,
    subentry_type: str | None,
    subentry_id: str | None,
    menu_choice: str | None,
    other_modes: list[str],
) -> dict[str, Any]:
    """``ha_set_integration(describe=True, subentry_type=...)``: describe
    ``entry_id``'s subentry form, with ``menu_choice`` submitted when given.
    The flow is aborted afterwards."""
    if not describe or domain is not None or entry_id is None or other_modes:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "subentry_type and subentry_id apply only with describe=True "
                "and entry_id (subentry_id for an existing subentry), and "
                "optionally menu_choice"
                + (f"; drop {', '.join(other_modes)}" if other_modes else ""),
                suggestions=[
                    "Example: ha_set_integration(entry_id='abc123', "
                    "describe=True, subentry_type='conversation')"
                ],
            )
        )
    entry_id = validate_identifier_not_empty(entry_id, "entry_id")
    # The entry names the domain whose strings hold the help text.
    resp: dict[str, Any] = {"success": True, "entry_id": entry_id}
    resp["entry"] = await client.get_config_entry(entry_id)
    await fetch_config_subentry_schema(
        client,
        entry_id,
        resp,
        subentry_type=subentry_type,
        subentry_id=subentry_id,
        show_advanced_options=False,
        menu_choice=menu_choice,
    )
    del resp["entry"]
    return resp
