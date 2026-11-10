"""Config-entry-flow helpers and config subentries for ha_config_set_helper.

Each call answers one step of Home Assistant's flow, as the HA UI does
(``integration_flow_steps``); the registry settings are applied once the flow
has created or updated the helper.
"""

import asyncio
import logging
from functools import partial
from typing import Any

from ...errors import ErrorCode, create_error_response
from ..coercion import parse_json_param, parse_string_list_param
from ..config_entry_flow import (
    _SUBENTRY_CREATE_LOCKS,
    _created_subentry_id,
    _subentry_ids,
)
from ..helpers import raise_tool_error, validate_identifier_not_empty
from ..integration_flow_steps import (
    FlowTarget,
    abort_open_flow,
    advance_flow,
    open_step_answer,
    options_target,
    start_setup_flow,
)
from .registry import (
    _apply_registry_updates_to_entity,
    _check_name_collision,
    _get_entities_for_config_entry,
    validate_registry_ids,
)
from .schemas import _attach_helper_skill, _helper_response

logger = logging.getLogger(__name__)

_OPEN_STEP_TYPES = ("form", "menu")


def _flow_answers(
    config: str | dict[str, Any] | None, name: str | None, **context: Any
) -> dict[str, Any] | None:
    """The call's answer to a form: ``config``, with ``name`` as its ``name``
    field when it sets none. ``None`` answers no form."""
    parsed: Any = None if config == "" else config
    if isinstance(parsed, str):
        try:
            parsed = parse_json_param(parsed, "config")
        except ValueError as err:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    str(err),
                    context={**context, "parameter": "config"},
                )
            )
    if parsed is None:
        return None
    if not isinstance(parsed, dict):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"config must be an object, got {type(parsed).__name__}",
                context={**context, "parameter": "config"},
            )
        )
    answers = dict(parsed)
    if name is not None and name.strip() and "name" not in answers:
        answers["name"] = name
    return answers


async def _wait_for_flow_entities(
    client: Any,
    entry_id: str | None,
    action: str,
    wait: bool,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Resolve entities for a config entry; poll briefly on create+wait.

    Graduated polling: short intervals for the first retries catch local/small
    instances quickly; steady 500ms matches typical entity_registry/list
    latency on larger remote setups without missing entities near the deadline.
    Silent retries — a transient WS failure on attempt #1 often recovers by
    the deadline, and 14 identical warnings would just flood the response.
    """
    warnings: list[str] = []
    entities: list[dict[str, Any]] = []
    if not entry_id:
        return entities, warnings
    if action == "create" and wait:
        deadline, elapsed, attempt = 5.0, 0.0, 0
        intervals = [0.2, 0.3]
        steady_interval = 0.5
        poll_warnings: list[str] = []
        while elapsed < deadline:
            poll_warnings = []
            entities = await _get_entities_for_config_entry(
                client, entry_id, poll_warnings
            )
            if entities:
                break
            step = intervals[attempt] if attempt < len(intervals) else steady_interval
            await asyncio.sleep(step)
            elapsed += step
            attempt += 1
        if not entities and poll_warnings:
            warnings.extend(poll_warnings)
    else:
        entities = await _get_entities_for_config_entry(client, entry_id, warnings)
    return entities, warnings


async def _apply_flow_registry_updates(
    client: Any,
    entity_ids: list[str],
    area_id: str | None,
    labels_list: list[str] | None,
    category: str | None,
    icon: str | None,
    extras: dict[str, Any],
    warnings: list[str],
) -> None:
    """Apply area/labels/category/icon to every entity from a flow helper, in parallel."""
    if not (
        entity_ids
        and (
            area_id is not None
            or labels_list is not None
            or category is not None
            or icon is not None
        )
    ):
        return
    applied_per_entity = list(
        await asyncio.gather(
            *(
                _apply_registry_updates_to_entity(
                    client, eid, area_id, labels_list, category, icon, warnings
                )
                for eid in entity_ids
            )
        )
    )
    # Echo a top-level convenience key only when it was actually applied to at
    # least one entity. The per-entity ``applied`` dicts are the source of
    # truth — a failed entity_registry/update leaves its key out and records a
    # warning instead — so echoing straight from the inputs would assert
    # success that the accompanying warnings contradict.
    applied_keys = {k for entry in applied_per_entity for k in entry}
    if area_id is not None and "area_id" in applied_keys:
        extras["area_id"] = area_id if area_id else None
    if labels_list is not None and "labels" in applied_keys:
        extras["labels"] = labels_list
    if category and "category" in applied_keys:
        extras["category"] = category
    if icon is not None and "icon" in applied_keys:
        extras["icon"] = icon if icon else None
    extras["applied"] = applied_per_entity


async def _handle_flow_helper(
    client: Any,
    helper_type: str,
    name: str | None,
    helper_id: str | None,
    config: str | dict[str, Any] | None,
    area_id: str | None,
    labels: str | list[str] | None,
    category: str | None,
    wait: bool,
    icon: str | None = None,
    *,
    action: str,
    MandatoryBPS: bool = False,
    flow_id: str | None = None,
    menu_choice: str | None = None,
    abort: bool = False,
) -> dict[str, Any]:
    """One step of a flow helper's config flow (create) or options flow
    (update). Once Home Assistant finishes the flow, area_id / labels /
    category / icon are applied to every entity of the helper's config entry
    (a utility_meter's tariff sensors and its select alike).
    """
    if action == "create":
        target = FlowTarget("setup", helper_type)
        start: Any = partial(start_setup_flow, client, helper_type)
    else:
        entry_id = validate_identifier_not_empty(
            helper_id, "helper_id", context={"helper_type": helper_type}
        )
        target = await options_target(client, entry_id, helper_type)
        start = partial(client.start_options_flow, entry_id)
    if abort:
        return await abort_open_flow(client, target, flow_id)

    # The top-level name is the new helper's: an options form has no name
    # field unless its integration declares one, which then goes in config.
    answers = _flow_answers(
        config, name if action == "create" else None, helper_type=helper_type
    )
    try:
        labels_list = parse_string_list_param(labels, "labels")
    except ValueError as e:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Invalid labels parameter: {e}",
                context={"helper_type": helper_type},
            )
        )
    # Bug 12: a new helper named like an existing one is refused before a
    # flow is opened, rather than created as a "_2" duplicate.
    if action == "create" and flow_id is None:
        new_name = (answers or {}).get("name", name)
        await _check_name_collision(
            client, helper_type, new_name if isinstance(new_name, str) else None
        )
    # Bug 16 (issue #1150): validate registry IDs BEFORE creating the config entry.
    await validate_registry_ids(
        client, area_id, labels_list, {"helpers": category}, fail_closed=True
    )

    flow_id, step = await advance_flow(
        client,
        target,
        flow_id=flow_id,
        config=answers,
        menu_choice=menu_choice,
        start=start,
    )
    registry_given = [
        param
        for param, value in (
            ("area_id", area_id),
            ("labels", labels_list),
            ("category", category),
            ("icon", icon),
        )
        if value is not None
    ]
    if step.get("type") in _OPEN_STEP_TYPES:
        response = await open_step_answer(client, target, flow_id, step)
        response.update(action=action, helper_type=helper_type)
        if registry_given:
            response.setdefault("warnings", []).append(
                f"{', '.join(registry_given)} not applied yet: pass them "
                "again with the call that finishes the flow, which applies "
                "them to the helper's entities."
            )
        return response

    result = step.get("result")
    result = result if isinstance(result, dict) else {}
    saved_id = result.get("entry_id") if action == "create" else target.entry_id
    title = result.get("title", step.get("title"))
    entities, warnings = await _wait_for_flow_entities(client, saved_id, action, wait)
    entity_ids = [e["entity_id"] for e in entities if e.get("entity_id")]

    extras: dict[str, Any] = {
        "method": "config_flow",
        "entry_id": saved_id,
        "title": title,
        "entity_ids": entity_ids,
    }
    if action == "update":
        extras["updated"] = True

    await _apply_flow_registry_updates(
        client, entity_ids, area_id, labels_list, category, icon, extras, warnings
    )

    response = _helper_response(
        action,
        helper_type,
        data={"entry_id": saved_id, "title": title},
        message=f"{helper_type} helper {action}d successfully",
        warnings=warnings,
        **extras,
    )
    _attach_helper_skill(response, MandatoryBPS)
    return response


def _subentry_action(action: str | None, subentry_id: str | None) -> str:
    if action is None:
        return "update" if subentry_id else "create"
    if action == "create" and subentry_id is not None:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "action='create' was passed with subentry_id. "
                "Omit subentry_id to create a new subentry.",
                context={
                    "helper_type": "config_subentry",
                    "action": action,
                    "subentry_id": subentry_id,
                },
            )
        )
    if action == "update" and subentry_id is None:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "action='update' requires subentry_id.",
                context={"helper_type": "config_subentry", "action": action},
            )
        )
    return action


async def _handle_set_config_subentry(
    client: Any,
    action: str | None,
    entry_id: str | None,
    subentry_type: str | None,
    subentry_id: str | None,
    config: dict[str, Any] | str | None,
    MandatoryBPS: bool,
    *,
    flow_id: str | None = None,
    menu_choice: str | None = None,
    abort: bool = False,
) -> dict[str, Any]:
    """One step of a config subentry's flow: its create flow, or with
    ``subentry_id`` its reconfigure flow."""
    action = _subentry_action(action, subentry_id)
    context = {"helper_type": "config_subentry", "action": action}
    entry_id = validate_identifier_not_empty(
        entry_id,
        "entry_id",
        suggestions=["Use ha_get_integration() to find the parent config entry ID"],
        context=context,
    )
    subentry_type = validate_identifier_not_empty(
        subentry_type,
        "subentry_type",
        suggestions=[
            "Use ha_get_integration(entry_id=..., include_subentries=True, "
            "include_subentry_schema=True) to inspect available subentry metadata.",
        ],
        context=context,
    )
    if subentry_id is not None:
        subentry_id = validate_identifier_not_empty(
            subentry_id, "subentry_id", context=context
        )
    answers = _flow_answers(config, None, **context)

    entry = await client.get_config_entry(entry_id)
    domain = entry.get("domain")
    target = FlowTarget(
        "subentry",
        domain if isinstance(domain, str) else None,
        entry_id,
        subentry_type=subentry_type,
        subentry_id=subentry_id,
    )
    if abort:
        return await abort_open_flow(client, target, flow_id)

    start = partial(
        client.start_config_subentry_flow,
        entry_id,
        subentry_type,
        subentry_id=subentry_id,
    )

    async def advance() -> tuple[str, dict[str, Any]]:
        return await advance_flow(
            client,
            target,
            flow_id=flow_id,
            config=answers,
            menu_choice=menu_choice,
            start=start,
        )

    if subentry_id is not None:
        flow_id, step = await advance()
    else:
        # Core's create result carries no subentry_id: the new one is the
        # difference in the parent's listing, so creates under one parent
        # finish one at a time.
        async with _SUBENTRY_CREATE_LOCKS[entry_id]:
            before = await _subentry_ids(client, entry_id)
            flow_id, step = await advance()
            if before is not None and step.get("type") == "create_entry":
                subentry_id = await _created_subentry_id(client, entry_id, before)

    if step.get("type") in _OPEN_STEP_TYPES:
        response = await open_step_answer(client, target, flow_id, step)
        response.update(
            action=action,
            helper_type="config_subentry",
            entry_id=entry_id,
            subentry_type=subentry_type,
        )
        return response

    operation = "reconfigured" if action == "update" else "created"
    response = {
        "success": True,
        "entry_id": entry_id,
        "subentry_type": subentry_type,
        "subentry_id": subentry_id,
        "operation": operation,
        "flow_result": step,
        "message": f"Config subentry {operation} successfully",
    }
    _attach_helper_skill(response, MandatoryBPS)
    return response
