"""Config-entry-flow helpers and config subentries for ha_config_set_helper."""

import asyncio
import logging
from typing import Any

from ...errors import ErrorCode, create_error_response
from ...redaction import redact_flow_schema, redaction_enabled
from ..config_entry_flow import (
    create_flow_helper,
    get_user_step_field_names,
    set_config_subentry,
    update_flow_helper,
)
from ..config_entry_flow_walker import fetch_helper_flow_info
from ..helpers import raise_tool_error, validate_identifier_not_empty
from ..util_helpers import parse_json_param, parse_string_list_param
from .registry import (
    _apply_registry_updates_to_entity,
    _get_entities_for_config_entry,
    validate_registry_ids,
)
from .schemas import _attach_helper_skill, _helper_response

logger = logging.getLogger(__name__)


# Flow helper types whose top-level config-flow step is a MENU rather than a
# FORM — for these, ``fetch_helper_flow_info`` cannot return a ``data_schema``
# without a menu choice (``next_step_id`` / ``group_type`` / ``menu_option``).
# The pre-flow gates in ``_handle_flow_helper`` use this set to surface a
# ``data_schema_unavailable_reason: "menu_helper_requires_branch"`` marker
# alongside the legal sub-types under ``menu_options`` so the LLM can pick
# a branch on the next try without a separate discovery round-trip. Hint
# set — extending it only sharpens the signal, missing entries fall back
# to silent ``None``.
_MENU_ROOTED_FLOW_HELPER_TYPES: frozenset[str] = frozenset({"template", "group"})


# Keys callers may pass inside ``config`` to select a menu branch — mirrors
# ``_MENU_SELECTION_KEY_ORDER`` in ``config_entry_flow_menu.py`` (kept in parallel
# rather than imported to avoid widening that module's surface). The ORDER is
# load-bearing on both sides: it fixes which key wins when a config carries
# more than one selection key.
_MENU_CHOICE_CONFIG_KEYS: tuple[str, ...] = (
    "group_type",
    "next_step_id",
    "menu_option",
)


def _extract_menu_choice_from_config(
    config_dict: dict[str, Any] | None,
) -> str | None:
    """Best-effort menu-choice extraction for pre-flow error context.

    Walks ``_MENU_CHOICE_CONFIG_KEYS`` in order and returns the first usable
    selection, stringified: a scalar value, or the first element of a list of
    successive selections (flows that revisit menus, issue #2116). Empty and
    missing values fall through to the next key; ``None`` when nothing
    usable is supplied. Follows ``_handle_menu_step``'s key precedence —
    without this, ``_flow_helper_error_context`` falls back to
    ``menu_choice=None`` and silently omits ``data_schema`` for menu-rooted
    types (``template``/``group`` — the most common ones).
    """
    if not config_dict:
        return None
    for key in _MENU_CHOICE_CONFIG_KEYS:
        value = config_dict.get(key)
        if isinstance(value, list):
            value = value[0] if value else None
        if value is None or value == "":
            continue
        return str(value)
    return None


async def _flow_helper_error_context(
    client: Any,
    helper_type: str,
    *,
    menu_choice: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build a validation-error `context` dict carrying the flow data_schema.

    Complements ``_simple_helper_error_context`` for the FLOW pre-flow
    validation gates in ``_handle_flow_helper`` — those fire before HA
    itself sees the request, so the auto-attach in ``_raise_flow_api_error``
    never runs.

    For menu-rooted helpers (``template``, ``group``) without a derivable
    ``menu_choice``, the schema can't be fetched without picking a branch;
    a ``data_schema_unavailable_reason: "menu_helper_requires_branch"``
    marker is added instead, along with the legal sub-types under
    ``menu_options`` (issue #1186), so the caller can pick a branch on
    the next try without a separate discovery round-trip.
    """
    context: dict[str, Any] = {"helper_type": helper_type}
    try:
        info = await fetch_helper_flow_info(
            client, helper_type, menu_choice=menu_choice
        )
    except Exception as e:  # noqa: BLE001
        # Mirror the breadcrumb in ``abort_config_flow``'s own swallow
        # (config_entry_flow_walker), so a fetch failure here doesn't
        # disappear silently — this PR raises the call rate by 5 sites
        # and the swallow needs an audit-trail entry.
        logger.debug(
            "_flow_helper_error_context: flow-info fetch failed for "
            "helper_type=%r menu_choice=%r: %s",
            helper_type,
            menu_choice,
            e,
        )
        info = {}
    if "schema" in info:
        # Under redact_secrets, password-field current values must not ride
        # into the error context verbatim (#2157).
        context["data_schema"] = (
            redact_flow_schema(info["schema"])
            if redaction_enabled()
            else info["schema"]
        )
    elif helper_type in _MENU_ROOTED_FLOW_HELPER_TYPES and not menu_choice:
        context["data_schema_unavailable_reason"] = "menu_helper_requires_branch"
        if "menu_options" in info:
            context["menu_options"] = info["menu_options"]
    context.update(extra)
    return context


def _resolve_flow_action(
    action: str | None, helper_id: str | None, helper_type: str
) -> str:
    """Resolve action from explicit value or implicit discriminator (presence of helper_id).

    Defence in depth: when reached via the legacy implicit-action path, an
    empty/whitespace helper_id would otherwise be falsy and silently route to
    create — same destructive intent-loss class as the registry-metadata twins.
    None stays the documented "create-new" sentinel.
    """
    if action is not None:
        return action
    if helper_id is not None:
        validate_identifier_not_empty(
            helper_id,
            "helper_id",
            suggestions=[
                "Omit helper_id entirely to create a new flow helper",
                "Pass a valid helper_id to update an existing one",
                "Or pass action='create' / action='update' explicitly",
            ],
            context={"helper_type": helper_type},
        )
    return "update" if helper_id else "create"


async def _normalize_flow_config(
    config: str | dict[str, Any] | None,
    client: Any,
    helper_type: str,
) -> dict[str, Any]:
    """Normalize config to a dict. Raises ToolError on invalid input."""
    # Treat empty string as "nothing passed" — parse_json_param("") would raise
    # a confusing "Invalid JSON" error rather than signalling an absent config.
    if config == "":
        return {}
    if isinstance(config, str):
        parsed = parse_json_param(config)
        if not isinstance(parsed, dict):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "config must be a JSON object (dict) for flow-based helpers",
                    suggestions=[
                        'Example: {"name": "my_helper", "source": "sensor.x"}'
                    ],
                    context=await _flow_helper_error_context(client, helper_type),
                )
            )
        return parsed
    if isinstance(config, dict):
        return dict(config)  # shallow copy — we may mutate
    if config is None:
        return {}
    raise_tool_error(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            f"config must be a dict or JSON string, got {type(config).__name__}",
            context=await _flow_helper_error_context(client, helper_type),
        )
    )
    return {}  # unreachable; satisfies type checker


async def _inject_or_strip_name(
    action: str,
    name: str | None,
    config_dict: dict[str, Any],
    client: Any,
    helper_type: str,
) -> tuple[dict[str, Any], list[str]]:
    """Inject name on create or strip it on update, returning (config_dict, pre_warnings).

    CREATE: most flow helpers accept `name` as a top-level form field, so the
    tool folds the top-level `name` parameter into the form payload. But some
    helpers (switch_as_x) reject `name` as an extra key — probe the user-step
    schema first; only inject if the schema actually accepts a `name` field.
    If introspection fails or the top step is a menu (template, group), fall
    back to injecting — those helpers are known to accept `name`.

    UPDATE: options flows are strict about extra keys; HA rejects any
    caller-supplied `name`. Strip it and emit a warning so the caller learns
    their attempted rename was a no-op.
    """
    pre_warnings: list[str] = []
    if action == "create" and name and name.strip() and "name" not in config_dict:
        schema_fields = await get_user_step_field_names(client, helper_type)
        if schema_fields is None or "name" in schema_fields:
            config_dict["name"] = name
    elif action == "update" and "name" in config_dict:
        stripped_name = config_dict.pop("name")
        pre_warnings.append(
            f"Ignored 'name' in config: flow helper options flows do not "
            f"support renaming (attempted name={stripped_name!r}). Use "
            f"ha_set_entity to change the friendly name of the resulting entity."
        )
    return config_dict, pre_warnings


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
    config: str | dict | None,
    area_id: str | None,
    labels: str | list[str] | None,
    category: str | None,
    wait: bool,
    icon: str | None = None,
    action: str | None = None,
) -> dict[str, Any]:
    """Create or update a flow-based helper and apply registry updates to all entities.

    Routes between create_flow_helper and update_flow_helper based on helper_id,
    then resolves the resulting config_entry_id to its entity(ies) and applies
    area_id / labels / category / icon across the full set.

    For utility_meter with tariffs, this means the same label/area/icon is
    applied to every tariff sensor (and the select entity) uniformly.

    `action` may be passed by the caller (Bug 11 explicit-intent path) — when
    None, falls back to the legacy implicit discriminator (presence of
    helper_id => update). Validation that the (action, helper_id) combination
    is consistent has already happened upstream in ha_config_set_helper.
    """
    action = _resolve_flow_action(action, helper_id, helper_type)
    config_dict = await _normalize_flow_config(config, client, helper_type)
    config_dict, pre_warnings = await _inject_or_strip_name(
        action, name, config_dict, client, helper_type
    )

    try:
        labels_list = parse_string_list_param(labels, "labels")
    except ValueError as e:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Invalid labels parameter: {e}",
                context=await _flow_helper_error_context(
                    client,
                    helper_type,
                    menu_choice=_extract_menu_choice_from_config(config_dict),
                ),
            )
        )

    # Bug 16 (issue #1150): validate registry IDs BEFORE creating the config entry.
    await validate_registry_ids(
        client,
        area_id,
        labels_list,
        {"helpers": category},
        fail_closed=True,
    )

    if action == "create":
        # Validate against EITHER the top-level `name` arg OR `config_dict["name"]`.
        # Some helpers (switch_as_x) deliberately don't have `name` injected into
        # config_dict because their schema rejects it — but the tool still
        # requires `name` to be supplied so callers fail fast and consistently.
        config_name = config_dict.get("name")
        config_name_ok = isinstance(config_name, str) and bool(config_name.strip())
        name_ok = name is not None and bool(name.strip())
        if not (name_ok or config_name_ok):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f'name is required for create action. Include "name" as a '
                    f'top-level argument, e.g. {{"helper_type": "{helper_type}", "name": "My Helper"}}.',
                    suggestions=[
                        'Add "name": "My Helper" at the top level of the JSON arguments',
                        'Or include "name": "My Helper" inside the "config" dict',
                    ],
                    context=await _flow_helper_error_context(
                        client,
                        helper_type,
                        menu_choice=_extract_menu_choice_from_config(config_dict),
                    ),
                )
            )
        flow_result = await create_flow_helper(client, helper_type, config_dict)
    else:
        flow_result = await update_flow_helper(
            client,
            helper_type,
            config_dict,
            helper_id,  # type: ignore[arg-type]
        )

    entry_id = flow_result.get("entry_id")
    title = flow_result.get("title")
    warnings = list(pre_warnings)
    warnings.extend(flow_result.get("warnings", []))
    entities, wait_warnings = await _wait_for_flow_entities(
        client, entry_id, action, wait
    )
    warnings.extend(wait_warnings)
    entity_ids = [e["entity_id"] for e in entities if e.get("entity_id")]

    extras: dict[str, Any] = {
        "method": "config_flow",
        "entry_id": entry_id,
        "title": title,
        "entity_ids": entity_ids,
    }
    if action == "update":
        extras["updated"] = True

    await _apply_flow_registry_updates(
        client, entity_ids, area_id, labels_list, category, icon, extras, warnings
    )

    return _helper_response(
        action,
        helper_type,
        data={"entry_id": entry_id, "title": title},
        message=flow_result.get("message"),
        warnings=warnings,
        **extras,
    )


# ---------------------------------------------------------------------------
# ha_config_set_helper DISPATCH HELPERS
# ---------------------------------------------------------------------------


async def _handle_set_config_subentry(
    client: Any,
    action: str | None,
    entry_id: str | None,
    subentry_type: str | None,
    subentry_id: str | None,
    show_advanced_options: bool,
    config: dict[str, Any] | str | None,
    MandatoryBPS: bool,
) -> dict[str, Any]:
    """Handle the config_subentry branch of ha_config_set_helper."""
    if action is not None:
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
    else:
        action = "update" if subentry_id else "create"

    entry_id = validate_identifier_not_empty(
        entry_id,
        "entry_id",
        suggestions=["Use ha_get_integration() to find the parent config entry ID"],
        context={"helper_type": "config_subentry", "action": action},
    )
    subentry_type = validate_identifier_not_empty(
        subentry_type,
        "subentry_type",
        suggestions=[
            "Use ha_get_integration(entry_id=..., include_subentries=True, "
            "include_subentry_schema=True) to inspect available subentry metadata.",
        ],
        context={"helper_type": "config_subentry", "action": action},
    )
    if subentry_id is not None:
        subentry_id = validate_identifier_not_empty(
            subentry_id,
            "subentry_id",
            context={"helper_type": "config_subentry", "action": action},
        )

    if not isinstance(config, dict):
        try:
            config_dict = parse_json_param(config, "config") if config else {}
        except ValueError as err:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    str(err),
                    context={
                        "helper_type": "config_subentry",
                        "action": action,
                        "parameter": "config",
                    },
                )
            )
    else:
        config_dict = config
    if not isinstance(config_dict, dict):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "config must be an object for config_subentry",
                context={"helper_type": "config_subentry", "action": action},
            )
        )

    subentry_response = await set_config_subentry(
        client,
        entry_id,
        subentry_type,
        config_dict,
        subentry_id=subentry_id,
        show_advanced_options=show_advanced_options,
    )
    _attach_helper_skill(subentry_response, MandatoryBPS)
    return subentry_response
