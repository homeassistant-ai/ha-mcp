"""Describe a helper's fields from Home Assistant itself (issue #2632).

Flow-based helpers are described from the live config flow (or the options
flow of an existing entry), which is what the HA UI renders. Storage helpers
are described from Core's own create schema, served by the ``ha_mcp_tools``
component (tools entry or embedded server entry). Without the component, the
static ``SIMPLE_HELPER_SCHEMAS`` table is the fallback, and it also fills in
the few fields whose Core validator does not serialize (e.g. schedule days).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, NoReturn

from ha_mcp._vendor.fastmcp.exceptions import ToolError

from ...errors import ErrorCode, create_error_response
from ...redaction import redact_flow_schema, redaction_enabled
from ..component_helper_collections import fetch_helper_schemas, read_helper_item
from ..config_entry_flow import FLOW_HELPER_TYPES, config_entry_of_domain
from ..config_entry_flow_introspect import fetch_helper_flow_info, menu_choices
from ..helpers import exception_to_structured_error, raise_tool_error
from .listing import listed_items
from .schemas import SIMPLE_HELPER_SCHEMAS

logger = logging.getLogger(__name__)

# Long select lists (units, device classes) are truncated: the first few
# values show the format, and HA reports the full list if a value is rejected.
MAX_OPTIONS = 12

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


def compact_field(field: dict[str, Any]) -> dict[str, Any]:
    """One HA schema field (flow selector or serialized voluptuous) in brief."""
    out: dict[str, Any] = {"name": field.get("name")}
    if field.get("required"):
        out["required"] = True
    if isinstance(field.get("selector"), dict) and field["selector"]:
        kind, cfg = next(iter(field["selector"].items()))
        cfg = cfg if isinstance(cfg, dict) else {}
        out["type"] = kind
        if isinstance(cfg.get("options"), list | tuple):
            out["options"] = _option_values(cfg["options"])
        out.update(
            {k: cfg[k] for k in _SELECTOR_KEYS if cfg.get(k) not in (None, False)}
        )
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


async def _field_help(
    client: Any, handler: str, category: str, step_id: str | None
) -> dict[str, str]:
    """HA's own help text for a flow step's fields, keyed by field name.

    These are the strings the UI shows under each field; every integration
    ships and maintains them. Best-effort: missing strings leave no help.
    """
    if not step_id:
        return {}
    try:
        result = await client.send_websocket_message(
            {
                "type": "frontend/get_translations",
                "language": "en",
                "category": category,
                "integration": [handler],
            }
        )
    except Exception as err:  # noqa: BLE001
        logger.debug("describe: translations for %s failed: %s", handler, err)
        return {}
    if not isinstance(result, dict):
        return {}
    payload = result.get("result") or result
    resources = payload.get("resources") if isinstance(payload, dict) else None
    if not isinstance(resources, dict):
        return {}
    prefix = f"component.{handler}.{category}.step.{step_id}."
    descriptions: dict[str, str] = {}
    labels: dict[str, str] = {}
    for key, text in resources.items():
        if not key.startswith(prefix) or not isinstance(text, str):
            continue
        # [sections.<section>.]data_description.<field> is the help text;
        # [sections.<section>.]data.<field> the field's label, used when a
        # field has no help text of its own.
        parts = key[len(prefix) :].split(".")
        if len(parts) >= 2 and parts[-2] == "data_description":
            descriptions[parts[-1]] = text
        elif len(parts) >= 2 and parts[-2] == "data":
            labels[parts[-1]] = text
    return {**labels, **descriptions}


def _with_help(fields: list[dict[str, Any]], help_text: dict[str, str]) -> None:
    for field in fields:
        if field["name"] in help_text and "description" not in field:
            field["description"] = help_text[field["name"]]
        _with_help(field.get("fields", []), help_text)


async def _describe_form(
    client: Any, helper_type: str, category: str, step: dict[str, Any]
) -> list[dict[str, Any]]:
    fields = compact_schema(step.get("schema") or step.get("data_schema") or [])
    _with_help(
        fields, await _field_help(client, helper_type, category, step.get("step_id"))
    )
    return fields


_MORE_FORMS = {False: "yes", None: "depends_on_answers"}


def _later_forms(step: dict[str, Any]) -> dict[str, Any]:
    """Say when HA asks for more fields on a later form of the same flow."""
    more = _MORE_FORMS.get(step.get("last_step"))
    if more is None:
        return {}
    return {
        "more_forms": more,
        "note": "Home Assistant asks for more fields on a later form, whose "
        "fields can depend on these answers. Pass them in the same config; "
        "a missing one comes back in the error with that form's fields.",
    }


async def _describe_flow(
    client: Any, helper_type: str, menu_choice: str | None, entry_id: str | None
) -> dict[str, Any]:
    if entry_id is None:
        info = await fetch_helper_flow_info(client, helper_type, menu_choice)
        if menu_choice and "schema" in info and "branch" not in info:
            _reject_menu_choice(
                helper_type, menu_choice, f"{helper_type} opens with a form, not a menu"
            )
        if "schema" in info:
            return {
                "source": "config_flow",
                "fields": await _describe_form(client, helper_type, "config", info),
                **_later_forms(info),
            }
        if "menu_options" in info:
            return {
                "source": "config_flow",
                "menu_options": info["menu_options"],
                "note": "Pass menu_choice to describe one of these sub-types.",
            }
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

    if menu_choice:
        _reject_menu_choice(
            helper_type, menu_choice, "an existing helper is described by its options"
        )
    await config_entry_of_domain(client, entry_id, helper_type)
    flow_id: str | None = None
    try:
        result = await client.start_options_flow(entry_id)
        flow_id = result.get("flow_id")
        if result.get("type") == "menu":
            choices = menu_choices(result)
            return {
                "source": "options_flow",
                **({"menu_options": choices} if choices else {}),
                "note": "This helper's options flow opens with a menu; its forms "
                "are not described.",
            }
        if result.get("type") != "form":
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"The options flow of {entry_id} did not open with a form "
                    f"({result.get('type')}: {result.get('reason')}).",
                    context={"helper_type": helper_type, "helper_id": entry_id},
                )
            )
        return {
            "source": "options_flow",
            "fields": await _describe_form(client, helper_type, "options", result),
            **_later_forms(result),
        }
    finally:
        if flow_id:
            try:
                await asyncio.wait_for(client.abort_options_flow(flow_id), 5.0)
            except Exception as err:  # noqa: BLE001
                logger.debug("describe: options flow %s abort failed: %s", flow_id, err)


def _reject_menu_choice(helper_type: str, menu_choice: str, why: str) -> NoReturn:
    raise_tool_error(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            f"menu_choice {menu_choice!r} does not apply: {why}.",
            context={"helper_type": helper_type, "menu_choice": menu_choice},
            suggestions=["Drop menu_choice"],
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
    if helper_type in FLOW_HELPER_TYPES:
        described = await _describe_flow(client, helper_type, menu_choice, helper_id)
    else:
        if menu_choice:
            _reject_menu_choice(
                helper_type, menu_choice, f"{helper_type} is a storage helper"
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
