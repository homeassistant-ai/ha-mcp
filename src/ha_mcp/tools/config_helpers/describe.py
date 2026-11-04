"""Describe a helper's fields from Home Assistant itself (issue #2632).

Flow-based helpers are described from the live config flow (or the options
flow of an existing entry), which is what the HA UI renders. Storage helpers
are described from Core's own create schema, served by the ``ha_mcp_tools``
component (tools entry or embedded server entry). Without the component, the
static ``SIMPLE_HELPER_SCHEMAS`` table is the fallback, and it also fills in
the few fields whose Core validator does not serialize (e.g. schedule days).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, NoReturn

from ha_mcp._vendor.fastmcp.exceptions import ToolError

from ...errors import ErrorCode, create_error_response
from ...redaction import redact_flow_schema, redaction_enabled
from ..component_helper_collections import fetch_helper_schemas, read_helper_item
from ..config_entry_flow import config_entry_of_domain
from ..config_entry_flow_errors import (
    FlowRef,
    abort_reason_text,
    progress_action_text,
)
from ..config_entry_flow_introspect import abort_probe_flow, menu_choices
from ..flow_translations import (
    fetch_flow_strings,
    field_help,
    strings_unread_warning,
)
from ..helpers import exception_to_structured_error, raise_tool_error
from .listing import listed_items
from .schemas import SIMPLE_HELPER_SCHEMAS, SIMPLE_HELPER_TYPES

# Long select lists (units, device classes) are truncated: the first few
# values show the format, and HA reports the full list if a value is rejected.
MAX_OPTIONS = 12

_Submit = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]

_SELECTOR_KEYS = (
    "min",
    "max",
    "step",
    "unit_of_measurement",
    "multiple",
    "custom_value",
    "domain",
    "device_class",
    "mode",
    "filter",
)
_SERIALIZED_KEYS = ("valueMin", "valueMax", "lengthMin", "lengthMax", "allow_none")


def _option_values(options: list[Any]) -> list[Any]:
    values = [
        o[0]
        if isinstance(o, list | tuple)
        else o.get("value")
        if isinstance(o, dict)
        else o
        for o in options
    ]
    if len(values) <= MAX_OPTIONS:
        return values
    return [*values[:MAX_OPTIONS], f"...{len(values) - MAX_OPTIONS} more"]


def _compact_selector(selector: dict[str, Any]) -> dict[str, Any]:
    kind, cfg = next(iter(selector.items()))
    cfg = cfg if isinstance(cfg, dict) else {}
    out: dict[str, Any] = {"type": kind}
    # A text selector's own type (password, email, url) is kept: it is how a
    # secret field stays recognisable.
    if isinstance(cfg.get("type"), str):
        out["input_type"] = cfg["type"]
    if isinstance(cfg.get("options"), list | tuple):
        out["options"] = _option_values(cfg["options"])
    out.update({k: cfg[k] for k in _SELECTOR_KEYS if cfg.get(k) not in (None, False)})
    return out


def compact_field(field: dict[str, Any]) -> dict[str, Any]:
    """One HA schema field (flow selector or serialized voluptuous) in brief."""
    out: dict[str, Any] = {"name": field.get("name")}
    if field.get("required"):
        out["required"] = True
    if isinstance(field.get("selector"), dict) and field["selector"]:
        out.update(_compact_selector(field["selector"]))
    elif field.get("type") in {"expandable", "grid"}:
        out["type"] = "section"
        out["fields"] = [compact_field(f) for f in field.get("schema", [])]
    else:
        if "type" in field:
            out["type"] = field["type"]
        if isinstance(field.get("options"), list | tuple):
            out["options"] = _option_values(field["options"])
        out.update({k: field[k] for k in _SERIALIZED_KEYS if k in field})
    if field.get("default") is not None:
        out["default"] = field["default"]
    description = field.get("description")
    if isinstance(description, str):
        out["description"] = description
    elif isinstance(description, dict) and "suggested_value" in description:
        out["current"] = description["suggested_value"]
    return out


def compact_schema(schema: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if redaction_enabled():
        schema = redact_flow_schema(schema)
    return [compact_field(f) for f in schema if isinstance(f, dict)]


def _with_static_hints(
    helper_type: str, core_fields: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Core's fields, typed from the static table where Core's validator is opaque."""
    static = {f["name"]: f for f in SIMPLE_HELPER_SCHEMAS.get(helper_type, [])}
    # Core's field order is arbitrary (schedule weekdays come back shuffled);
    # follow the static table's order where it knows the field.
    order = {name: i for i, name in enumerate(static)}

    core_fields = sorted(
        core_fields, key=lambda f: order.get(f.get("name", ""), len(order))
    )
    merged = []
    for field in core_fields:
        hint = static.get(field.get("name", ""))
        opaque = "type" not in field and "selector" not in field
        merged.append(
            {
                **field,
                "selector": hint["selector"],
                "description": hint.get("description"),
            }
            if opaque and hint
            else field
        )
    return merged


def _with_help(
    fields: list[dict[str, Any]], help_text: dict[str, str], prefix: str = ""
) -> None:
    for field in fields:
        key = f"{prefix}{field['name']}"
        if key in help_text and "description" not in field:
            field["description"] = help_text[key]
        _with_help(field.get("fields", []), help_text, f"{field['name']}.")


async def describe_form(
    client: Any, domain: str | None, path: str, step: dict[str, Any]
) -> dict[str, Any]:
    """A flow form in brief: ``fields`` with HA's help text for each,
    ``more_forms``/``note`` unless the form is the flow's last, and
    ``warnings`` when Core's strings could not be read.

    ``path`` is the flow's place in ``strings.json``: ``config``, ``options``
    or ``config_subentries.<type>``. Without a ``domain`` there is no help
    text to read.
    """
    fields = compact_schema(step.get("data_schema") or [])
    described: dict[str, Any] = {"fields": fields, **_later_forms(step)}
    step_id = step.get("step_id")
    if step_id and domain:
        strings = await fetch_flow_strings(client, domain, path.partition(".")[0])
        if strings is None:
            described["warnings"] = [strings_unread_warning(domain)]
        else:
            _with_help(fields, field_help(strings, domain, path, step_id))
    return described


_LATER_FORM_TAIL = (
    "Pass them in the same config; a missing one comes back in the error with "
    "that form's fields."
)
# ``last_step`` is False when a later form follows and None when the flow does
# not say, which many flows do, single-form ones included.
_MORE_FORMS = {
    False: (
        "yes",
        "Home Assistant asks for more fields on a later form, whose fields can "
        "depend on these answers. ",
    ),
    None: (
        "depends_on_answers",
        "Home Assistant did not say whether more forms follow. If one does, its "
        "fields can depend on these answers. ",
    ),
}


def _later_forms(step: dict[str, Any]) -> dict[str, Any]:
    """Say whether HA asks for more fields on a later form, or leaves it open."""
    more = _MORE_FORMS.get(step.get("last_step"))
    if more is None:
        return {}
    answer, lead = more
    return {"more_forms": answer, "note": lead + _LATER_FORM_TAIL}


def refuse_menu_choice(menu_choice: str, why: str, **context: Any) -> NoReturn:
    raise_tool_error(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            f"menu_choice {menu_choice!r} does not apply: {why}.",
            context={"menu_choice": menu_choice, **context},
            suggestions=[
                "Drop menu_choice"
                + (", or pass one of menu_options" if "menu_options" in context else "")
            ],
        )
    )


async def _progress_context(
    client: Any, flow: FlowRef, step: dict[str, Any]
) -> dict[str, Any]:
    """A progress step's ``progress_action`` and its text; else nothing."""
    if step.get("type") != "progress":
        return {}
    return {
        "progress_action": step.get("progress_action"),
        **await progress_action_text(client, flow, step),
    }


async def describe_step(
    client: Any,
    step: dict[str, Any],
    *,
    flow: FlowRef,
    menu_choice: str | None,
    submit: _Submit,
    abort: Callable[[str], Awaitable[Any]],
    menu_note: str,
) -> dict[str, Any]:
    """Describe a flow's first step, or the form behind ``menu_choice``, then
    abort the flow, also when describing it fails.

    A step that is not a form, menu or abort raises, except a
    ``create_entry`` reached through ``menu_choice``, which is reported.
    """
    flow_id = step.get("flow_id")
    try:
        chosen: dict[str, Any] = {}
        if step.get("type") == "menu":
            choices = menu_choices(step)
            if menu_choice is None:
                return {
                    "flow_type": "menu",
                    "step_id": step.get("step_id"),
                    "menu_options": choices,
                    "note": menu_note,
                }
            if menu_choice not in choices:
                refuse_menu_choice(
                    menu_choice,
                    "the flow's menu does not offer it",
                    menu_options=choices,
                )
            step = await submit(step["flow_id"], {"next_step_id": menu_choice})
            chosen = {"menu_choice": menu_choice}
        elif menu_choice is not None and step.get("type") not in (
            "abort",
            "create_entry",
        ):
            # Not refused: an abort is reported below, and a finished flow
            # gets the no-form error.
            refuse_menu_choice(
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
        if step_type == "create_entry" and chosen:
            # Core saved what the menu choice selected.
            return {"flow_type": "create_entry", **chosen, "title": step.get("title")}
        if step_type != "form":
            progress = await _progress_context(client, flow, step)
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
    finally:
        if flow_id:
            await abort_probe_flow(abort, flow_id)


async def _describe_flow(
    client: Any, helper_type: str, menu_choice: str | None, entry_id: str | None
) -> dict[str, Any]:
    if entry_id is not None:
        if menu_choice:
            refuse_menu_choice(
                menu_choice,
                "an existing helper is described by its options",
                helper_type=helper_type,
            )
        await config_entry_of_domain(client, entry_id, helper_type)
        return {
            "source": "options_flow",
            **await describe_step(
                client,
                await client.start_options_flow(entry_id),
                flow=FlowRef(helper_type, "options", entry_id),
                menu_choice=None,
                submit=client.submit_options_flow_step,
                abort=client.abort_options_flow,
                menu_note="This helper's options flow opens with a menu; its "
                "forms are not described.",
            ),
        }

    try:
        return {
            "source": "config_flow",
            **await describe_step(
                client,
                await client.start_config_flow(helper_type),
                flow=FlowRef(helper_type, "config"),
                menu_choice=menu_choice,
                submit=client.submit_config_flow_step,
                abort=client.abort_config_flow,
                menu_note="Pass menu_choice to describe one of these sub-types.",
            ),
        }
    except ToolError:
        raise
    except Exception:  # noqa: BLE001
        # The flow could not be started or read: a menu_choice HA rejects, an
        # integration that fails to load, a timeout. Never "no fields".
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER
                if menu_choice
                else ErrorCode.SERVICE_CALL_FAILED,
                f"Home Assistant returned no setup form for {helper_type}"
                + (f" with menu_choice {menu_choice!r}" if menu_choice else "")
                + ".",
                context={"helper_type": helper_type, "menu_choice": menu_choice},
                suggestions=[
                    f"ha_config_list_helpers({helper_type!r}, describe=True) "
                    "without menu_choice lists the sub-types"
                ]
                if menu_choice
                else ["Check the Home Assistant log for the integration's setup"],
            )
        )


async def _stored_item(
    client: Any, helper_type: str, helper_id: str
) -> dict[str, Any] | None:
    by_entity = "." in helper_id
    read = await read_helper_item(
        client,
        helper_type,
        entity_id=helper_id if by_entity else None,
        item_id=None if by_entity else helper_id,
    )
    if read is not None:
        item = read.get("item", read)
        return item if isinstance(item, dict) else None
    item_id: Any = helper_id
    if by_entity:  # the legacy list is keyed by the entity's unique_id
        entry = await client.send_websocket_message(
            {"type": "config/entity_registry/get", "entity_id": helper_id}
        )
        registered = entry.get("result") if isinstance(entry, dict) else None
        item_id = registered.get("unique_id") if isinstance(registered, dict) else None
        if not item_id:
            return None
    result = await client.send_websocket_message({"type": f"{helper_type}/list"})
    items = listed_items(result.get("result") if isinstance(result, dict) else result)
    return next(
        (i for i in items if isinstance(i, dict) and i.get("id") == item_id), None
    )


async def _describe_simple(
    client: Any, helper_type: str, helper_id: str | None
) -> dict[str, Any]:
    core = await fetch_helper_schemas(client)
    core_fields = (core or {}).get(helper_type, {}).get("create")
    if core_fields:
        out: dict[str, Any] = {
            "source": "core_schema",
            "fields": compact_schema(_with_static_hints(helper_type, core_fields)),
        }
    else:
        out = {
            "source": "static_fallback",
            "fields": compact_schema(
                [dict(f) for f in SIMPLE_HELPER_SCHEMAS.get(helper_type, [])]
            ),
        }
    if helper_id:
        item = await _stored_item(client, helper_type, helper_id)
        if item is None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    f"No {helper_type} with id or entity_id {helper_id!r} was found.",
                    context={"helper_type": helper_type, "helper_id": helper_id},
                    suggestions=[f"ha_config_list_helpers({helper_type!r}) lists them"],
                )
            )
        else:
            for field in out["fields"]:
                if field["name"] in item:
                    field["current"] = item[field["name"]]
    return out


async def describe_helper(
    client: Any,
    helper_type: str,
    *,
    menu_choice: str | None = None,
    helper_id: str | None = None,
) -> dict[str, Any]:
    """The fields ha_config_set_helper takes in ``config`` for ``helper_type``.

    ``helper_id`` adds each field's current value: the config entry id for a
    flow helper (read via its options flow), or the storage id / entity_id
    for a storage helper.
    """
    # ha_config_list_helpers checked the type: not storage means a helper flow.
    if helper_type not in SIMPLE_HELPER_TYPES:
        described = await _describe_flow(client, helper_type, menu_choice, helper_id)
    else:
        if menu_choice:
            refuse_menu_choice(
                menu_choice,
                f"{helper_type} is a storage helper",
                helper_type=helper_type,
            )
        described = await _describe_simple(client, helper_type, helper_id)
    return {"helper_type": helper_type, **described}


async def describe_helper_response(
    client: Any,
    helper_type: str,
    menu_choice: str | None,
    helper_id: str | None,
    *,
    describe: bool = True,
) -> dict[str, Any]:
    """``ha_config_list_helpers(describe=True)``: the tool response.

    Also reached when a describe-only parameter is passed without describe,
    which is refused rather than silently listing.
    """
    if not describe:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "menu_choice and helper_id apply only with describe=True.",
                context={"helper_type": helper_type},
                suggestions=["Pass describe=True, or drop these parameters"],
            )
        )
    if helper_type == "all":
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "describe needs a single helper_type, not 'all'.",
            )
        )
    try:
        described = await describe_helper(
            client, helper_type, menu_choice=menu_choice, helper_id=helper_id
        )
    except ToolError:
        raise
    except Exception as e:  # noqa: BLE001
        exception_to_structured_error(
            e, context={"helper_type": helper_type, "helper_id": helper_id}
        )
    return {"success": True, **described}
