"""ha_get_integration's setup, options and subentry flow schemas (issue #2632).

A schema only exists inside an open flow: the domain's config flow (or the
entry's options or subentry flow) is started, its form described in the same
compact shape the helper describe uses (fields, HA's help text, current
values), and the flow aborted. A flow that opens with a menu lists its
branches; ``menu_choice`` submits one of them and describes the form behind
it. A flow Home Assistant aborts is reported with Core's reason text.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ha_mcp._vendor.fastmcp.exceptions import ToolError

from ..client.rest_client import HomeAssistantAPIError
from ..errors import ErrorCode, create_error_response
from ..redaction import (
    harvest_secret_strings,
    is_password_flow_field,
    redact_options_by_flow_schema,
    redaction_enabled,
    sentinel_replacement,
)
from .config_entry_flow_errors import FlowRef, abort_reason_text
from .config_entry_flow_form import iter_schema_fields
from .config_entry_flow_introspect import menu_choices
from .config_helpers.describe import describe_form, later_forms
from .helpers import raise_tool_error

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
    data_schema: Any = None
    flow_id = None
    try:
        flow = await client.start_options_flow(entry_id)
        flow_id = flow.get("flow_id")
        if flow.get("type") == "form":
            # An empty/missing schema on a form (e.g. a confirm-only
            # step) classifies nothing — persisted options can outlive
            # fields removed from the flow, so treat it as unreadable
            # and take the fail-closed branch below instead of
            # "redacting" zero fields.
            candidate = flow.get("data_schema")
            if isinstance(candidate, list) and candidate:
                data_schema = candidate
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "redact_secrets: options-flow probe failed for %s: %r", entry_id, exc
        )
    finally:
        if flow_id:
            try:
                await client.abort_options_flow(flow_id)
            except Exception as abort_err:  # noqa: BLE001
                logger.warning(
                    "redact_secrets: failed to abort probe flow %s: %r",
                    flow_id,
                    abort_err,
                )
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


def _refuse_menu_choice(menu_choice: str, why: str, **context: Any) -> None:
    raise_tool_error(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            f"menu_choice {menu_choice!r} does not apply: {why}.",
            context={"menu_choice": menu_choice, **context},
            suggestions=["Drop menu_choice, or pass one of menu_options"],
        )
    )


async def _describe_step(
    client: Any,
    step: dict[str, Any],
    *,
    domain: str,
    path: str,
    menu_choice: str | None,
    submit: _Submit,
) -> dict[str, Any]:
    """Describe a flow's first step, or the form behind ``menu_choice``."""
    chosen: dict[str, Any] = {}
    if step.get("type") == "menu":
        choices = menu_choices(step)
        if menu_choice is None:
            return {
                "flow_type": "menu",
                "step_id": step.get("step_id"),
                "menu_options": choices,
                "note": "Pass menu_choice to describe one of these forms.",
            }
        if menu_choice not in choices:
            _refuse_menu_choice(
                menu_choice, "the flow's menu does not offer it", menu_options=choices
            )
        step = await submit(step["flow_id"], {"next_step_id": menu_choice})
        chosen = {"menu_choice": menu_choice}
    elif menu_choice is not None and step.get("type") != "abort":
        _refuse_menu_choice(
            menu_choice, f"the flow opens with a {step.get('type')}, not a menu"
        )
    if step.get("type") == "abort":
        return {
            "flow_type": "abort",
            **chosen,
            "aborted": step.get("reason"),
            **await abort_reason_text(client, FlowRef(domain, path), step),
        }
    if step.get("type") != "form":
        return {
            "flow_type": step.get("type"),
            "step_id": step.get("step_id"),
            **chosen,
            "details": step,
        }
    return {
        "flow_type": "form",
        "step_id": step.get("step_id"),
        **chosen,
        "fields": await describe_form(client, domain, path, step),
        **later_forms(step),
    }


async def fetch_options_schema(
    client: Any,
    entry_id: str,
    resp: dict[str, Any],
    *,
    populate_options: bool = True,
    menu_choice: str | None = None,
) -> None:
    """Describe the entry's options flow as ``resp["options_schema"]``.

    Also populates ``resp["entry"]["options"]`` from a first-step form so
    callers requesting both schema and options don't pay for two round-trips.
    Pass ``populate_options=False`` on the component-served path, where
    ``options`` already carry the raw persisted values and must NOT be
    overwritten by the OptionsFlow-derived suggested-value shape.
    """
    entry = resp.get("entry") if isinstance(resp.get("entry"), dict) else None
    domain = str((entry or {}).get("domain") or "")
    flow_id = None
    try:
        flow_result = await client.start_options_flow(entry_id)
        flow_id = flow_result.get("flow_id")
        flow_type = flow_result.get("type")
        if flow_type not in ("form", "menu", "abort"):
            return
        if flow_type == "form" and entry is not None:
            if populate_options:
                entry["options"] = options_from_form_flow(flow_result)
            elif redaction_enabled():
                # Component-served path: options carry raw persisted values
                # (the component only scrubs resolved !secret references).
                # Now that the flow schema is in hand, redact the fields it
                # marks as passwords.
                entry["options"] = redact_options_by_flow_schema(
                    entry.get("options"), flow_result.get("data_schema", [])
                )
        resp["options_schema"] = await _describe_step(
            client,
            flow_result,
            domain=domain,
            path="options",
            menu_choice=menu_choice,
            submit=client.submit_options_flow_step,
        )
    except ToolError:
        raise
    except Exception as schema_err:  # noqa: BLE001
        logger.warning(
            f"Failed to fetch options schema for {entry_id}: "
            f"{type(schema_err).__name__}: {schema_err}"
        )
        schema_warnings: list[str] = resp.setdefault("warnings", [])
        schema_warnings.append(
            f"options schema probe failed for {entry_id}: "
            f"{type(schema_err).__name__} — 'options_schema' is missing "
            "and 'options' may be incomplete"
        )
    finally:
        if flow_id:
            try:
                await client.abort_options_flow(flow_id)
            except Exception as abort_err:  # noqa: BLE001
                logger.warning(
                    f"Failed to abort options flow {flow_id}: "
                    f"{type(abort_err).__name__}: {abort_err}"
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
                "subentry_type is required when include_subentry_schema=True",
                suggestions=[
                    "Call ha_get_integration(entry_id=..., "
                    "include_subentries=True) to inspect existing subentry "
                    "types, then retry with subentry_type.",
                ],
                context={"entry_id": entry_id},
            )
        )

    entry = resp.get("entry") if isinstance(resp.get("entry"), dict) else None
    flow_id = None
    try:
        flow_result = await client.start_config_subentry_flow(
            entry_id,
            subentry_type,
            subentry_id=subentry_id,
            show_advanced_options=show_advanced_options,
        )
        flow_id = flow_result.get("flow_id")
        resp["subentry_schema"] = {
            "subentry_type": subentry_type,
            "subentry_id": subentry_id,
            **await _describe_step(
                client,
                flow_result,
                domain=str((entry or {}).get("domain") or ""),
                path=f"config_subentries.{subentry_type}",
                menu_choice=menu_choice,
                submit=client.submit_config_subentry_flow_step,
            ),
        }
    finally:
        if flow_id:
            try:
                await asyncio.wait_for(
                    client.abort_config_subentry_flow(flow_id), timeout=5.0
                )
            except Exception as abort_err:  # noqa: BLE001
                logger.warning(
                    "Failed to abort config subentry introspection flow %s: %s",
                    flow_id,
                    abort_err,
                )


async def fetch_config_flow_schema(
    client: Any, domain: str, resp: dict[str, Any], *, menu_choice: str | None = None
) -> None:
    """Describe ``domain``'s setup (config) flow as ``resp["config_flow_schema"]``.

    The flow is started, its first step (or the form behind ``menu_choice``)
    described, and the flow aborted, so nothing is set up.
    """
    try:
        first_step = await client.start_config_flow(domain)
    except HomeAssistantAPIError as err:
        # Core answers 404 "Invalid handler specified" for a domain without a
        # config flow and 400 for failed dependencies.
        if err.status_code not in (400, 404):
            raise
        resp.setdefault("warnings", []).append(
            f"config_flow_schema: Home Assistant could not start a config flow "
            f"for {domain!r} ({err})"
        )
        return
    flow_id = first_step.get("flow_id")
    try:
        schema = await _describe_step(
            client,
            first_step,
            domain=domain,
            path="config",
            menu_choice=menu_choice,
            submit=client.submit_config_flow_step,
        )
    finally:
        if flow_id:
            try:
                await asyncio.wait_for(client.abort_config_flow(flow_id), timeout=5.0)
            except Exception as abort_err:  # noqa: BLE001
                # A flow that already finished (abort, create_entry) is gone.
                logger.debug("config flow %s abort failed: %s", flow_id, abort_err)
    resp["config_flow_schema"] = schema
    if schema.get("flow_type") == "create_entry":
        result = schema.get("details", {}).get("result")
        entry_id = result.get("entry_id") if isinstance(result, dict) else None
        resp.setdefault("warnings", []).append(
            f"{domain}'s config flow finished without asking anything and Home "
            f"Assistant created a config entry (entry_id {entry_id}). It was "
            "left in place; remove it with ha_remove_helpers_integrations if "
            "it is not wanted."
        )
