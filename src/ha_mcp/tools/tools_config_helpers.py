"""
Configuration management tools for Home Assistant helpers.

This module provides tools for listing, creating, updating, and removing
Home Assistant helper entities (input_button, input_boolean, input_select,
input_number, input_text, input_datetime, counter, timer, schedule).
"""

import asyncio
import logging
import uuid
from collections.abc import Callable
from typing import Annotated, Any, Literal, TypedDict

from fastmcp.exceptions import ToolError
from fastmcp.tools import tool
from pydantic import AliasChoices, Field

from ..client.rest_client import HomeAssistantAPIError
from ..errors import ErrorCode, create_error_response
from .auto_backup import with_auto_backup
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
    validate_identifier_not_empty,
)
from .tools_config_entry_flow import (
    FLOW_HELPER_TYPES,
    create_flow_helper,
    fetch_helper_flow_info,
    get_user_step_field_names,
    set_config_subentry,
    update_flow_helper,
)
from .util_helpers import (
    apply_entity_category,
    attach_skill_content,
    augment_error_dict_with_skill_content,
    augment_tool_error_with_skill_content,
    parse_json_param,
    parse_string_list_param,
    wait_for_entity_registered,
)

# helper-selection.md guides which helper type fits the agent's use case
# (input_*, counter, timer, template, group, utility_meter, etc.).
_HELPER_SKILL_FILES: tuple[str, ...] = ("references/helper-selection.md",)


def _attach_helper_skill(response: dict[str, Any], MandatoryBPS: bool) -> None:
    """In-place attach skill_content to a helper response when applicable.

    Helper tool has no best-practice checker integration, so
    ``referenced_files`` is always None — embedding is driven purely by
    the ``MandatoryBPS`` flag. Delegates to the shared
    :func:`attach_skill_content` so the missing-vendor-warning path is
    consistent across every write tool.
    """
    attach_skill_content(
        response,
        MandatoryBPS=MandatoryBPS,
        canonical_files=_HELPER_SKILL_FILES,
        referenced_files=None,
    )


# Simple helper types — managed via {type}/create and {type}/update WebSocket APIs
# (not Config Entry Flow). Kept in parallel with FLOW_HELPER_TYPES for routing.
SIMPLE_HELPER_TYPES: frozenset[str] = frozenset(
    {
        "input_button",
        "input_boolean",
        "input_select",
        "input_number",
        "input_text",
        "input_datetime",
        "counter",
        "timer",
        "schedule",
        "zone",
        "person",
        "tag",
    }
)


# Bug 4b/7c/10/14 (issue #1150): per-helper-type allowlists of typed
# parameters. Inapplicable params are rejected at the top of the tool
# instead of being silently dropped. Cross-cutting params (helper_type,
# name, helper_id, area_id, labels, category, wait, config) are always
# accepted and not listed here. `icon` is included where it applies.
_TYPE_TYPED_PARAMS: dict[str, frozenset[str]] = {
    # Simple helpers
    "input_button": frozenset({"icon"}),
    "input_boolean": frozenset({"icon", "initial"}),
    "input_select": frozenset({"icon", "options", "initial"}),
    "input_number": frozenset(
        {
            "icon",
            "min_value",
            "max_value",
            "step",
            "unit_of_measurement",
            "mode",
            "initial",
        }
    ),
    "input_text": frozenset(
        {
            "icon",
            "min_value",
            "max_value",
            "mode",
            "initial",
        }
    ),
    "input_datetime": frozenset({"icon", "has_date", "has_time", "initial"}),
    "counter": frozenset(
        {
            "icon",
            "initial",
            "min_value",
            "max_value",
            "step",
            "restore",
        }
    ),
    "timer": frozenset({"icon", "duration", "restore"}),
    "schedule": frozenset(
        {
            "icon",
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
        }
    ),
    "zone": frozenset(
        {
            "icon",
            "latitude",
            "longitude",
            "radius",
            "passive",
        }
    ),
    "person": frozenset({"user_id", "device_trackers", "picture"}),  # NO icon
    "tag": frozenset({"tag_id", "description"}),  # NO icon
    # Flow types: only `config` (handled separately — see _validate_applicable_params).
}

# Set of typed params that are simple-helper-specific (used to reject when a
# flow type was requested but a simple-helper param was passed).
_ALL_TYPED_PARAMS: frozenset[str] = frozenset().union(*_TYPE_TYPED_PARAMS.values())


class _HelperFieldSpecBase(TypedDict):
    """Required keys for every SIMPLE_HELPER_SCHEMAS field-spec entry."""

    name: str
    required: bool
    selector: dict[str, Any]


class _HelperFieldSpec(_HelperFieldSpecBase, total=False):
    """Optional `description` extension; mirrors HA's flow data_schema."""

    description: str


# Per-simple-type field schemas — list-of-dicts shape mirroring HA's flow
# ``data_schema`` so callers can iterate one shape regardless of helper kind.
# Consumed by ``ha_config_set_helper`` validation errors (relevant entry
# attached to ``context["data_schema"]`` so the LLM sees field shape inline
# with the 4xx that just blocked it).
#
# Each field-spec dict carries:
#   - ``name``        : argument key on ``ha_config_set_helper``.
#   - ``required``    : True iff the tool itself rejects on missing.
#   - ``selector``    : HA-style selector dict — ``{"text": {}}``,
#                       ``{"number": {}}``, ``{"boolean": {}}``,
#                       ``{"text": {"multiple": True}}``, or
#                       ``{"select": {"options": [...]}}`` for fixed-set
#                       strings. Mirrors HA's flow ``data_schema[i]`` shape so
#                       a caller doing ``field['selector']['text']`` works on
#                       both simple and flow helpers.
#   - ``description`` : (optional) short hint focused on what the LLM needs
#                       to send (NOT redundant with the @tool param
#                       description, which a non-toolsearch caller sees).
#
# Source of truth for ``required``: the create-branch raises in
# ``ha_config_set_helper`` itself (``_validate_create_required_fields``,
# ``_validate_input_select_options``, ``_validate_zone_coords``,
# ``_validate_input_datetime_components``, ``_validate_schedule_days``).
# HA-side defaults the tool does not enforce client-side stay
# ``required: False``.
SIMPLE_HELPER_SCHEMAS: dict[str, list[_HelperFieldSpec]] = {
    "input_button": [
        {
            "name": "name",
            "required": True,
            "selector": {"text": {}},
            "description": "Display name.",
        },
        {
            "name": "icon",
            "required": False,
            "selector": {"text": {}},
            "description": "Material Design Icon (e.g. 'mdi:bell').",
        },
    ],
    "input_boolean": [
        {
            "name": "name",
            "required": True,
            "selector": {"text": {}},
            "description": "Display name.",
        },
        {
            "name": "icon",
            "required": False,
            "selector": {"text": {}},
            "description": "Material Design Icon.",
        },
        {
            "name": "initial",
            "required": False,
            "selector": {"boolean": {}},
            "description": (
                "Initial state. Accepts 'true'/'false'/'on'/'off'/'yes'/'no'/'1'/'0'."
            ),
        },
    ],
    "input_select": [
        {
            "name": "name",
            "required": True,
            "selector": {"text": {}},
            "description": "Display name.",
        },
        {
            "name": "options",
            "required": True,
            "selector": {"text": {"multiple": True}},
            "description": (
                "Non-empty list of selectable options. Duplicates rejected."
            ),
        },
        {"name": "icon", "required": False, "selector": {"text": {}}},
        {
            "name": "initial",
            "required": False,
            "selector": {"text": {}},
            "description": "Initial value — must be one of `options`.",
        },
    ],
    "input_number": [
        {
            "name": "name",
            "required": True,
            "selector": {"text": {}},
            "description": "Display name.",
        },
        {
            "name": "min_value",
            "required": False,
            "selector": {"number": {}},
            "description": (
                "Minimum value. Also accepts shorthand `min`. HA defaults if "
                "omitted but supplying both bounds is recommended."
            ),
        },
        {
            "name": "max_value",
            "required": False,
            "selector": {"number": {}},
            "description": "Maximum value. Also accepts shorthand `max`.",
        },
        {
            "name": "step",
            "required": False,
            "selector": {"number": {}},
            "description": (
                "Step/increment. Must be > 0 and ≤ (max-min). Default 1.0."
            ),
        },
        {
            "name": "unit_of_measurement",
            "required": False,
            "selector": {"text": {}},
            "description": "Unit string (e.g. '°C'). Also accepts `unit`.",
        },
        {
            "name": "mode",
            "required": False,
            "selector": {"select": {"options": ["box", "slider"]}},
            "description": "Default 'slider'.",
        },
        {"name": "initial", "required": False, "selector": {"number": {}}},
        {"name": "icon", "required": False, "selector": {"text": {}}},
    ],
    "input_text": [
        {
            "name": "name",
            "required": True,
            "selector": {"text": {}},
            "description": "Display name.",
        },
        {
            "name": "min_value",
            "required": False,
            "selector": {"number": {}},
            "description": "Minimum length (0–255). Also accepts `min`.",
        },
        {
            "name": "max_value",
            "required": False,
            "selector": {"number": {}},
            "description": "Maximum length (0–255). Also accepts `max`.",
        },
        {
            "name": "mode",
            "required": False,
            "selector": {"select": {"options": ["text", "password"]}},
            "description": "Default 'text'.",
        },
        {"name": "initial", "required": False, "selector": {"text": {}}},
        {"name": "icon", "required": False, "selector": {"text": {}}},
    ],
    "input_datetime": [
        {
            "name": "name",
            "required": True,
            "selector": {"text": {}},
            "description": "Display name.",
        },
        {
            "name": "has_date",
            "required": False,
            "selector": {"boolean": {}},
            "description": (
                "Whether the entity carries a date component. At least one of "
                "`has_date` or `has_time` must be true (default: both)."
            ),
        },
        {
            "name": "has_time",
            "required": False,
            "selector": {"boolean": {}},
            "description": (
                "Whether the entity carries a time component. At least one of "
                "`has_date` or `has_time` must be true."
            ),
        },
        {
            "name": "initial",
            "required": False,
            "selector": {"text": {}},
            "description": "Initial value (datetime string).",
        },
        {"name": "icon", "required": False, "selector": {"text": {}}},
    ],
    "counter": [
        {
            "name": "name",
            "required": True,
            "selector": {"text": {}},
            "description": "Display name.",
        },
        {"name": "initial", "required": False, "selector": {"number": {}}},
        {
            "name": "min_value",
            "required": False,
            "selector": {"number": {}},
            "description": "Minimum value. Also accepts `min`.",
        },
        {
            "name": "max_value",
            "required": False,
            "selector": {"number": {}},
            "description": "Maximum value. Also accepts `max`.",
        },
        {
            "name": "step",
            "required": False,
            "selector": {"number": {}},
            "description": "Increment. Must be > 0. Default 1.",
        },
        {
            "name": "restore",
            "required": False,
            "selector": {"boolean": {}},
            "description": "Restore state on restart. Default true.",
        },
        {"name": "icon", "required": False, "selector": {"text": {}}},
    ],
    "timer": [
        {
            "name": "name",
            "required": True,
            "selector": {"text": {}},
            "description": "Display name.",
        },
        {
            "name": "duration",
            "required": False,
            "selector": {"text": {}},
            "description": (
                "Default duration as 'HH:MM:SS' or seconds. Default '00:00:00' "
                "(timer must be started with explicit duration)."
            ),
        },
        {
            "name": "restore",
            "required": False,
            "selector": {"boolean": {}},
            "description": "Restore state on restart. Default false.",
        },
        {"name": "icon", "required": False, "selector": {"text": {}}},
    ],
    "schedule": [
        {
            "name": "name",
            "required": True,
            "selector": {"text": {}},
            "description": "Display name.",
        },
        {
            "name": "monday",
            "required": False,
            "selector": {"object": {"multiple": True}},
            "description": (
                "List of {'from': 'HH:MM', 'to': 'HH:MM'} time ranges. At least "
                "one day across monday–sunday must contain a non-empty range."
            ),
        },
        {
            "name": "tuesday",
            "required": False,
            "selector": {"object": {"multiple": True}},
        },
        {
            "name": "wednesday",
            "required": False,
            "selector": {"object": {"multiple": True}},
        },
        {
            "name": "thursday",
            "required": False,
            "selector": {"object": {"multiple": True}},
        },
        {
            "name": "friday",
            "required": False,
            "selector": {"object": {"multiple": True}},
        },
        {
            "name": "saturday",
            "required": False,
            "selector": {"object": {"multiple": True}},
        },
        {
            "name": "sunday",
            "required": False,
            "selector": {"object": {"multiple": True}},
        },
        {"name": "icon", "required": False, "selector": {"text": {}}},
    ],
    "zone": [
        {
            "name": "name",
            "required": True,
            "selector": {"text": {}},
            "description": "Display name.",
        },
        {
            "name": "latitude",
            "required": True,
            "selector": {"number": {}},
            "description": "Latitude in decimal degrees.",
        },
        {
            "name": "longitude",
            "required": True,
            "selector": {"number": {}},
            "description": "Longitude in decimal degrees.",
        },
        {
            "name": "radius",
            "required": False,
            "selector": {"number": {}},
            "description": "Radius in meters. Default 100.",
        },
        {
            "name": "passive",
            "required": False,
            "selector": {"boolean": {}},
            "description": "Whether the zone is passive. Default false.",
        },
        {"name": "icon", "required": False, "selector": {"text": {}}},
    ],
    "person": [
        {
            "name": "name",
            "required": True,
            "selector": {"text": {}},
            "description": "Display name.",
        },
        {
            "name": "user_id",
            "required": False,
            "selector": {"text": {}},
            "description": "HA user account to link to this person.",
        },
        {
            "name": "device_trackers",
            "required": False,
            "selector": {"text": {"multiple": True}},
            "description": (
                "Entity IDs of device_tracker entities tracking this person."
            ),
        },
        {
            "name": "picture",
            "required": False,
            "selector": {"text": {}},
            "description": "URL or `/local/...` path to the picture.",
        },
    ],
    "tag": [
        {
            "name": "name",
            "required": True,
            "selector": {"text": {}},
            "description": "Display name (stored on the entity registry).",
        },
        {
            "name": "tag_id",
            "required": False,
            "selector": {"text": {}},
            "description": (
                "Stable tag identifier. Auto-generated by the tool if omitted "
                "(HA itself rejects tag/create without one)."
            ),
        },
        {"name": "description", "required": False, "selector": {"text": {}}},
    ],
}

# Dev-time invariant: every type listed in SIMPLE_HELPER_TYPES has a schema.
# Plain ``raise RuntimeError`` rather than ``assert`` because ``python -O``
# strips asserts — without this, a drift would produce a silent ``None`` from
# ``get_simple_helper_schema`` and propagate as "no data_schema attached",
# precisely the silent-failure pattern this dict is meant to eliminate.
if frozenset(SIMPLE_HELPER_SCHEMAS.keys()) != SIMPLE_HELPER_TYPES:
    raise RuntimeError(
        f"SIMPLE_HELPER_TYPES and SIMPLE_HELPER_SCHEMAS are out of sync: "
        f"missing schemas="
        f"{SIMPLE_HELPER_TYPES - frozenset(SIMPLE_HELPER_SCHEMAS.keys())}, "
        f"extra schemas="
        f"{frozenset(SIMPLE_HELPER_SCHEMAS.keys()) - SIMPLE_HELPER_TYPES}"
    )


def get_simple_helper_schema(helper_type: str) -> list[_HelperFieldSpec] | None:
    """Return the simple-helper field schema, or None for non-simple types.

    Callers attach the result to validation-error context as ``data_schema``
    so the LLM sees field shape inline with a 4xx response, matching the
    auto-attach pattern already in use for flow helpers (see
    ``fetch_helper_flow_info`` in ``tools_config_entry_flow``).
    Returns ``None`` for any helper_type not in ``SIMPLE_HELPER_SCHEMAS``,
    so callers can write a single uniform ``if schema is not None: …`` branch.
    """
    return SIMPLE_HELPER_SCHEMAS.get(helper_type)


def _simple_helper_error_context(
    helper_type: str,
    **extra: Any,
) -> dict[str, Any]:
    """Build a validation-error `context` dict carrying the helper's schema.

    Centralises the schema-attach idiom for the simple-helper raise sites in
    `ha_config_set_helper` so they stay one-liners. Returns a dict with
    `helper_type`, `data_schema` (omitted if no schema is registered for the
    type), and any caller-supplied extra fields.
    """
    context: dict[str, Any] = {"helper_type": helper_type}
    schema = get_simple_helper_schema(helper_type)
    if schema is not None:
        context["data_schema"] = schema
    context.update(extra)
    return context


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
# ``_MENU_SELECTION_KEYS`` in ``tools_config_entry_flow.py`` (kept in parallel
# rather than imported to avoid widening that module's surface).
_MENU_CHOICE_CONFIG_KEYS: tuple[str, ...] = (
    "group_type",
    "next_step_id",
    "menu_option",
)


def _extract_menu_choice_from_config(
    config_dict: dict[str, Any] | None,
) -> str | None:
    """Best-effort menu-choice extraction for pre-flow error context.

    Returns the value of the first ``_MENU_CHOICE_CONFIG_KEYS`` key found in
    ``config_dict`` if it's a non-empty string, else ``None``. Mirrors
    ``_handle_menu_step`` in ``tools_config_entry_flow`` — without this,
    ``_flow_helper_error_context`` falls back to ``menu_choice=None`` and
    silently omits ``data_schema`` for menu-rooted types
    (``template``/``group`` — the most common ones).
    """
    if not config_dict:
        return None
    for key in _MENU_CHOICE_CONFIG_KEYS:
        value = config_dict.get(key)
        if isinstance(value, str) and value:
            return value
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
    except Exception as e:
        # Mirror the breadcrumb in ``abort_config_flow``'s own swallow
        # (tools_config_entry_flow), so a fetch failure here doesn't
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
        context["data_schema"] = info["schema"]
    elif helper_type in _MENU_ROOTED_FLOW_HELPER_TYPES and not menu_choice:
        context["data_schema_unavailable_reason"] = "menu_helper_requires_branch"
        if "menu_options" in info:
            context["menu_options"] = info["menu_options"]
    context.update(extra)
    return context


# Bug 6 (issue #1150): valid mode values per helper type. The CREATE and
# UPDATE branches both validate against this; an invalid value is rejected
# instead of silently coerced to HA's default.
_MODE_BY_TYPE: dict[str, tuple[str, ...]] = {
    "input_number": ("box", "slider"),
    "input_text": ("text", "password"),
}


def _validate_mode(helper_type: str, mode: str | None) -> None:
    """Reject an invalid `mode` value for the chosen helper_type (Bug 6)."""
    if mode is None:
        return
    allowed = _MODE_BY_TYPE.get(helper_type)
    if allowed is None or mode in allowed:
        return
    options = " or ".join(repr(m) for m in allowed)
    raise_tool_error(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            f"mode={mode!r} is not valid for {helper_type}. Use {options}.",
            context=_simple_helper_error_context(helper_type, mode=mode),
            suggestions=[f"Pass mode={allowed[0]!r} or mode={allowed[1]!r}"],
        )
    )


def _validate_applicable_params(
    helper_type: str,
    passed: dict[str, Any],
) -> None:
    """Reject typed parameters that don't apply to the chosen helper_type.

    Bug 4b/7c/10/14 (issue #1150): the function signature accepts ~30 typed
    parameters, but each helper_type only legitimately uses 5-10 of them.
    Previously, inapplicable params were silently ignored. Now we raise
    VALIDATION_INVALID_PARAMETER so the caller sees their request was not
    handled, instead of getting `success: true` with the param dropped.

    `passed` is a dict of param_name -> value as the caller provided. None
    values are treated as "not passed" and skipped.
    """
    inapplicable: list[str] = []

    if helper_type in FLOW_HELPER_TYPES:
        # Flow types accept `config` (handled before this call) plus
        # cross-cutting params (name/helper_id/area_id/labels/category/wait).
        # Any simple-helper-typed param passed here is inapplicable.
        inapplicable.extend(
            param_name
            for param_name in _ALL_TYPED_PARAMS
            if passed.get(param_name) is not None
        )
    else:
        applicable = _TYPE_TYPED_PARAMS.get(helper_type, frozenset())
        for param_name, value in passed.items():
            if value is None:
                continue
            if param_name in applicable:
                continue
            inapplicable.append(param_name)

    if not inapplicable:
        return

    inapplicable.sort()
    if helper_type in FLOW_HELPER_TYPES:
        applicable_msg = (
            "config (see data_schema on a validation error for the field set), "
            "name, helper_id, area_id, labels, category, wait"
        )
    else:
        type_specific = sorted(_TYPE_TYPED_PARAMS.get(helper_type, frozenset()))
        type_specific_str = (
            ", ".join(type_specific) if type_specific else "(only name/icon)"
        )
        applicable_msg = (
            f"{type_specific_str}; plus name, helper_id, area_id, labels, "
            f"category, wait"
        )

    suggestions = [
        f"Remove these params for helper_type='{helper_type}': "
        f"{', '.join(inapplicable)}",
    ]
    if helper_type == "person" and "icon" in inapplicable:
        suggestions.append("Person entities use 'picture' (a URL), not 'icon'.")
    if helper_type == "tag" and "icon" in inapplicable:
        suggestions.append("Tags do not support icons.")
    if helper_type in FLOW_HELPER_TYPES:
        suggestions.append(
            f"For flow-based helpers like {helper_type!r}, type-specific config "
            "goes inside the `config` dict; submit with the wrong shape once "
            "and the validation error returns the `data_schema` for that helper."
        )

    raise_tool_error(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            f"The following parameters are not applicable for "
            f"helper_type='{helper_type}': {', '.join(inapplicable)}. "
            f"Applicable parameters: {applicable_msg}.",
            context={
                "helper_type": helper_type,
                "inapplicable_params": inapplicable,
            },
            suggestions=suggestions,
        )
    )


def _validate_numeric_range(
    helper_type: str,
    min_value: float | None,
    max_value: float | None,
    step: float | None,
) -> None:
    """Pre-validate min/max/step ranges for numeric simple helpers.

    Bug 13 (issue #1150): HA rejects several edge cases with cryptic messages
    (or, in the slider-step-too-large case, silently produces a broken
    slider). Surface clear, type-aware errors to the caller before the WS
    round-trip.

    Applies to: input_number (float), counter (int), input_text (length).
    For input_text, min/max are character lengths; values must be in [0, 255]
    and follow the standard min<max strict ordering.
    """
    if helper_type == "input_text":
        if min_value is not None and min_value < 0:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"input_text min_value (length) must be >= 0, got {min_value}.",
                    context=_simple_helper_error_context(
                        helper_type,
                        min_value=min_value,
                    ),
                )
            )
        if max_value is not None and max_value > 255:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"input_text max_value (length) must be <= 255, got {max_value}.",
                    context=_simple_helper_error_context(
                        helper_type,
                        max_value=max_value,
                    ),
                )
            )

    if min_value is not None and max_value is not None:
        if min_value > max_value:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"min_value ({min_value}) cannot be greater than max_value ({max_value}).",
                    context=_simple_helper_error_context(
                        helper_type,
                        min_value=min_value,
                        max_value=max_value,
                    ),
                )
            )
        if min_value == max_value:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"min_value and max_value must differ (both were {min_value}). "
                    f"Pick a non-empty range so the helper has more than one valid value.",
                    context=_simple_helper_error_context(
                        helper_type,
                        min_value=min_value,
                        max_value=max_value,
                    ),
                )
            )

    # Step validation only applies to numeric types (not input_text).
    if helper_type in ("input_number", "counter") and step is not None:
        if step <= 0:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"step must be > 0 for {helper_type} (got {step}).",
                    context=_simple_helper_error_context(helper_type, step=step),
                )
            )
        if (
            min_value is not None
            and max_value is not None
            and (max_value - min_value) > 0
            and step > (max_value - min_value)
        ):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"step ({step}) is larger than the range "
                    f"(max_value - min_value = {max_value - min_value}). "
                    f"HA does not reject this, but the resulting slider/control "
                    f"is unusable. Reduce step or widen the range.",
                    context=_simple_helper_error_context(
                        helper_type,
                        min_value=min_value,
                        max_value=max_value,
                        step=step,
                    ),
                )
            )


def _validate_initial_in_options(
    options: Any, initial: Any, helper_type: str = "input_select"
) -> None:
    """Reject ``initial`` values not in ``options``.

    Called from both create and update branches with the resolved values —
    caller-supplied on create, merged with the existing config on update.
    ``initial=None`` is the unset case and passes through. The
    ``isinstance(options, list)`` early-return mirrors the defensive shape
    check in ``_validate_input_select_options`` below — both validators are
    invariant gates, not type contracts; a future non-list caller is
    silently skipped rather than raising a confusing ``TypeError`` on
    ``initial not in options``.
    """
    if not isinstance(options, list) or initial is None:
        return
    if initial not in options:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"initial={initial!r} must be one of options "
                f"{options!r} for {helper_type}.",
                context=_simple_helper_error_context(
                    helper_type,
                    initial=initial,
                    options=options,
                ),
                suggestions=[
                    "Pick an `initial` value that's in `options`.",
                    "Or omit `initial` to use the default or existing value.",
                ],
            )
        )


def _validate_datetime_has_date_or_time(
    has_date: bool | None, has_time: bool | None
) -> None:
    """Reject ``input_datetime`` payloads where both components are False.

    Treats ``None`` as "not constrained" — only the explicit (False, False)
    case is flagged, since that's what reaches HA as the broken-entity
    payload. Both the create and update branches call this with the
    resolved-after-merge ``has_date`` / ``has_time`` pair.
    """
    if has_date is False and has_time is False:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "At least one of has_date or has_time must be True for input_datetime",
                context=_simple_helper_error_context(
                    "input_datetime",
                    has_date=has_date,
                    has_time=has_time,
                ),
                suggestions=[
                    "Set has_date=True to keep the date component.",
                    "Set has_time=True to keep the time component.",
                ],
            )
        )


def _validate_input_select_options(options: Any) -> None:
    """Reject input_select option lists containing duplicates (Bug 17, issue #1150).

    HA rejects duplicates with "Duplicate options are not allowed", but the
    error path it takes is generic enough that callers tend to misread it.
    Pre-validate so the message is unambiguous.
    """
    if not isinstance(options, list):
        return
    seen: set[Any] = set()
    duplicates: list[Any] = []
    for opt in options:
        if opt in seen and opt not in duplicates:
            duplicates.append(opt)
        else:
            seen.add(opt)
    if duplicates:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"input_select options must be unique. Duplicate option(s): "
                f"{', '.join(repr(d) for d in duplicates)}.",
                context=_simple_helper_error_context(
                    "input_select",
                    duplicates=duplicates,
                ),
                suggestions=["Remove duplicate entries from the options list."],
            )
        )


def _parse_hms(value: Any) -> tuple[int, int, int] | None:
    """Parse 'HH:MM' or 'HH:MM:SS' to a (h, m, s) tuple. Returns None if unparsable."""
    if not isinstance(value, str):
        return None
    parts = value.split(":")
    if len(parts) not in (2, 3):
        return None
    try:
        h = int(parts[0])
        m = int(parts[1])
        s = int(parts[2]) if len(parts) == 3 else 0
    except ValueError:
        return None
    return h, m, s


def _validate_schedule_days(
    monday: list | None,
    tuesday: list | None,
    wednesday: list | None,
    thursday: list | None,
    friday: list | None,
    saturday: list | None,
    sunday: list | None,
) -> None:
    """Pre-validate schedule day-range structure (Bug 17, issue #1150).

    Each range must include 'from' and 'to'; ranges within a single day must
    not overlap. HA reports per-day errors; surface a single clear message
    upfront with the offending day named.
    """
    day_params = {
        "monday": monday,
        "tuesday": tuesday,
        "wednesday": wednesday,
        "thursday": thursday,
        "friday": friday,
        "saturday": saturday,
        "sunday": sunday,
    }
    for day_name, day_schedule in day_params.items():
        if day_schedule is None:
            continue
        if not isinstance(day_schedule, list):
            continue  # let HA report shape errors
        intervals: list[tuple[int, int]] = []  # (from_secs, to_secs)
        for idx, time_range in enumerate(day_schedule):
            if not isinstance(time_range, dict):
                continue
            if "from" not in time_range or "to" not in time_range:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"schedule {day_name}[{idx}] must include both 'from' "
                        f"and 'to' keys, got: {sorted(time_range.keys())}.",
                        context=_simple_helper_error_context(
                            "schedule",
                            day=day_name,
                        ),
                    )
                )
            from_parsed = _parse_hms(time_range["from"])
            to_parsed = _parse_hms(time_range["to"])
            if from_parsed is None or to_parsed is None:
                continue  # let HA report format errors
            from_secs = from_parsed[0] * 3600 + from_parsed[1] * 60 + from_parsed[2]
            to_secs = to_parsed[0] * 3600 + to_parsed[1] * 60 + to_parsed[2]
            intervals.append((from_secs, to_secs))

        # Check overlap by sorting and walking. HA rejects overlap regardless
        # of caller order — we sort here so the error message points at a
        # canonical pair.
        sorted_intervals = sorted(intervals, key=lambda iv: iv[0])
        for i in range(1, len(sorted_intervals)):
            prev_from, prev_to = sorted_intervals[i - 1]
            cur_from, cur_to = sorted_intervals[i]
            if cur_from < prev_to:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"schedule {day_name} has overlapping time ranges "
                        f"({prev_from // 3600:02d}:{(prev_from % 3600) // 60:02d}-"
                        f"{prev_to // 3600:02d}:{(prev_to % 3600) // 60:02d} and "
                        f"{cur_from // 3600:02d}:{(cur_from % 3600) // 60:02d}-"
                        f"{cur_to // 3600:02d}:{(cur_to % 3600) // 60:02d}). "
                        f"HA requires non-overlapping ranges per day.",
                        context=_simple_helper_error_context(
                            "schedule",
                            day=day_name,
                        ),
                    )
                )


logger = logging.getLogger(__name__)


async def _ws_registry_lookup(
    client: Any, message: dict[str, Any]
) -> tuple[bool, list[dict[str, Any]]]:
    """Return (ok, items). ok=False means the lookup itself failed.

    ok=True with empty list means the registry exists and is genuinely empty —
    distinct from failure so phantom IDs can still be rejected. The fail-open
    ok=False path keeps transient HA outages from blocking legitimate calls.
    """
    try:
        result = await client.send_websocket_message(message)
    except Exception as e:
        logger.debug("_ws_registry_lookup: failed for %r: %s", message.get("type"), e)
        return False, []
    if isinstance(result, list):
        return True, result
    if isinstance(result, dict):
        if result.get("success") is False:
            return False, []
        inner = result.get("result", [])
        if isinstance(inner, list):
            return True, inner
    return False, []


def _registry_id_values(items: list[dict[str, Any]], field: str) -> list[str]:
    """Pull non-empty string values of `field` from a list of registry dicts."""
    return [
        v
        for it in items
        if isinstance(it, dict) and isinstance((v := it.get(field)), str)
    ]


def _ws_error_msg(response: dict[str, Any]) -> str:
    """Extract a human-readable error message from a failed WS response dict."""
    error_detail = response.get("error", {})
    if isinstance(error_detail, dict):
        msg: str = error_detail.get("message", "Unknown error")
        return msg
    return str(error_detail) if error_detail else "Unknown error"


def _raise_if_unknown_labels(
    ok: bool, ws_labels: list[dict[str, Any]], labels: list[str] | None
) -> None:
    """Raise VALIDATION_INVALID_PARAMETER if any label_id is not in the registry."""
    valid_label_ids = _registry_id_values(ws_labels, "label_id")
    if ok:
        unknown = [
            label_id
            for label_id in labels or []
            if label_id and label_id not in valid_label_ids
        ]
        if unknown:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Unknown label_id(s): {unknown}. These do not exist in "
                    "the label registry.",
                    context={"labels": labels, "unknown_labels": unknown},
                    suggestions=[
                        "Use ha_config_get_label() to list valid label IDs.",
                        "Use ha_config_set_label() to create a new label.",
                        f"Available label_ids: {sorted(valid_label_ids)}",
                    ],
                )
            )


async def _validate_registry_ids(
    client: Any,
    area_id: str | None,
    labels: list[str] | None,
    category: str | None,
) -> None:
    """Validate that area_id, labels, and category reference existing registry entries.

    Bug 16 (issue #1150): the entity-registry update path previously accepted any
    string and forwarded it to HA, leaving phantom references like
    `area_id="nonexistent_xyz"` in the registry. Validate before sending so the
    caller gets a clear error with the available IDs to choose from.

    Skips:
      - None values (caller did not pass — no change to apply).
      - Empty string area_id / category (these mean "clear" — HA accepts them).
      - Empty list labels (clear semantics).

    Raises VALIDATION_INVALID_PARAMETER on the first unknown ID encountered, with
    the available IDs included in the suggestions list so the caller can correct.
    """
    needs_area = area_id is not None and area_id != ""
    needs_labels = bool(labels)
    needs_category = category is not None and category != ""
    if not (needs_area or needs_labels or needs_category):
        return

    # Run the three registry lookups concurrently — they're independent WS round-trips.
    lookups: list[tuple[str, Any]] = []
    if needs_area:
        lookups.append(
            ("area", _ws_registry_lookup(client, {"type": "config/area_registry/list"}))
        )
    if needs_labels:
        lookups.append(
            (
                "labels",
                _ws_registry_lookup(client, {"type": "config/label_registry/list"}),
            )
        )
    if needs_category:
        lookups.append(
            (
                "category",
                _ws_registry_lookup(
                    client,
                    {"type": "config/category_registry/list", "scope": "helpers"},
                ),
            )
        )
    raw = await asyncio.gather(*(coro for _, coro in lookups))
    by_param = {key: result for (key, _), result in zip(lookups, raw, strict=True)}

    if needs_area:
        ok, areas = by_param["area"]
        valid_area_ids = _registry_id_values(areas, "area_id")
        if ok and area_id not in valid_area_ids:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"area_id={area_id!r} does not exist in the area registry.",
                    context={"area_id": area_id},
                    suggestions=[
                        "Use ha_list_floors_areas() to list valid area IDs.",
                        'Pass area_id="" to clear the area assignment.',
                        f"Available area_ids: {sorted(valid_area_ids)}",
                    ],
                )
            )

    if needs_labels:
        ok, ws_labels = by_param["labels"]
        _raise_if_unknown_labels(ok, ws_labels, labels)

    if needs_category:
        ok, categories = by_param["category"]
        valid_category_ids = _registry_id_values(categories, "category_id")
        if ok and category not in valid_category_ids:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"category={category!r} does not exist in the helpers "
                    "category registry.",
                    context={"category": category},
                    suggestions=[
                        "Use ha_config_get_category(scope='helpers') to list valid category IDs.",
                        "Use ha_config_set_category() to create a new category.",
                        f"Available category_ids: {sorted(valid_category_ids)}",
                    ],
                )
            )


def _slugify_helper_name(name: str) -> str:
    """Derive the slug HA generates from a helper display name.

    Mirrors HA's collection-storage logic: lowercase the name, replace spaces
    with underscores, then strip any non-alphanumeric/underscore characters.
    Used by the Bug 12 collision check so we can compare a caller-supplied
    `name` against existing helpers' IDs without an extra round trip.
    """
    lowered = name.lower().replace(" ", "_")
    return "".join(c for c in lowered if c.isalnum() or c == "_")


async def _find_collision_in_flow_helpers(
    client: Any, helper_type: str, target_slug: str
) -> str | None:
    """Search config-entry registry for a flow helper whose title slugifies to target_slug."""
    try:
        result = await client.send_websocket_message(
            {"type": "config_entries/get", "domain": helper_type}
        )
    except (HomeAssistantAPIError, ConnectionError, TimeoutError):
        # Connectivity issue — fail open so a transient outage doesn't block legit creates;
        # HA will auto-suffix duplicates on its own.
        return None
    entries = result.get("result", []) if isinstance(result, dict) else result
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        title = entry.get("title")
        if isinstance(title, str) and _slugify_helper_name(title) == target_slug:
            return entry.get("entry_id") or entry.get("id")
    return None


def _flatten_helper_list_result(result: Any) -> list[Any]:
    """Flatten a {type}/list WS response into a flat list of helper dicts.

    Handles the person/list shape ({"storage": [...], "config": [...]}) and
    the standard list shape ([...]).
    """
    if isinstance(result, dict):
        inner = result.get("result", [])
        if isinstance(inner, dict):
            items: list[Any] = []
            for key in ("storage", "config"):
                sub = inner.get(key)
                if isinstance(sub, list):
                    items.extend(sub)
            return items
        if isinstance(inner, list):
            return inner
    if isinstance(result, list):
        return result
    return []


async def _find_collision_in_simple_helpers(
    client: Any, helper_type: str, target_slug: str
) -> str | None:
    """Search simple-helper {type}/list for an entry whose id or name slug matches target_slug."""
    try:
        result = await client.send_websocket_message({"type": f"{helper_type}/list"})
    except (HomeAssistantAPIError, ConnectionError, TimeoutError):
        # Connectivity issue — fail open so a transient outage doesn't block legit creates;
        # HA will auto-suffix duplicates on its own.
        return None
    for item in _flatten_helper_list_result(result):
        if not isinstance(item, dict):
            continue
        existing_slug = item.get("id") or item.get("tag_id")
        if isinstance(existing_slug, str) and existing_slug == target_slug:
            return existing_slug
        existing_name = item.get("name")
        if (
            isinstance(existing_name, str)
            and _slugify_helper_name(existing_name) == target_slug
        ):
            return item.get("id") or item.get("tag_id") or target_slug
    return None


async def _check_name_collision(
    client: Any,
    helper_type: str,
    name: str | None,
) -> None:
    """Reject create requests whose name collides with an existing helper (Bug 12).

    HA's create endpoints auto-suffix duplicate names with `_2` / `_3` etc., so
    a caller asking to "create" a helper that already exists silently gets a
    duplicate entity instead of updating the original. Detect and reject before
    we send the create message, pointing the caller at the existing helper_id.

    Empty / missing / whitespace-only `name` is left to the existing
    name-required check downstream so the user sees the standard "name is
    required" error rather than a spurious collision miss (or a wasted WS
    round-trip on a name the validator is about to reject anyway).
    """
    if not name or not name.strip():
        return
    target_slug = _slugify_helper_name(name)
    if not target_slug:
        # Name normalises to empty (e.g. all punctuation). HA's create call
        # will reject; let it surface that error rather than guessing.
        return

    if helper_type in FLOW_HELPER_TYPES:
        existing_id = await _find_collision_in_flow_helpers(
            client, helper_type, target_slug
        )
    else:
        existing_id = await _find_collision_in_simple_helpers(
            client, helper_type, target_slug
        )

    if existing_id is None:
        return

    raise_tool_error(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            f"A {helper_type} helper named {name!r} already exists "
            f"(id: {existing_id!r}). Pass helper_id={existing_id!r} to update it, "
            f"or use a different name to create a new helper.",
            context=_simple_helper_error_context(
                helper_type,
                name=name,
                existing_helper_id=existing_id,
            )
            if helper_type in SIMPLE_HELPER_TYPES
            else {
                "helper_type": helper_type,
                "name": name,
                "existing_helper_id": existing_id,
            },
            suggestions=[
                f"To update the existing helper, pass helper_id={existing_id!r} "
                "(and omit `name`).",
                "To create a separate helper, pick a name whose slug does not "
                f"already exist (current collision: {target_slug!r}).",
            ],
        )
    )


async def _get_entities_for_config_entry(
    client: Any, entry_id: str, warnings: list[str] | None = None
) -> list[dict[str, Any]]:
    """Return all entity_registry entries linked to the given config_entry_id.

    Uses the config/entity_registry/list WebSocket API and filters client-side
    by config_entry_id. Multi-entity helpers (e.g. utility_meter with tariffs)
    are handled naturally — all entities for the same entry are returned.

    On WebSocket failure (e.g. HA mid-restart, auth lost, connection drop) the
    caller would otherwise see `entity_ids: []` and be told that registry-update
    targets like `area_id` / `labels` were silently dropped. If `warnings` is
    provided, append a concrete message so the caller surfaces the partial
    failure instead.
    """
    try:
        result = await client.send_websocket_message(
            {"type": "config/entity_registry/list"}
        )
    except Exception as e:
        if warnings is not None:
            warnings.append(
                f"entity_registry/list failed for config_entry_id={entry_id}: {e}"
            )
        return []

    # Success path: message can come back as a bare list or wrapped in
    # {"success": True, "result": [...]}. Treat a false success flag as an
    # error that should surface in warnings rather than silently returning [].
    if isinstance(result, dict) and result.get("success") is False:
        if warnings is not None:
            warnings.append(
                f"entity_registry/list failed for config_entry_id={entry_id}: "
                f"{_ws_error_msg(result)}"
            )
        return []

    entries = result if isinstance(result, list) else result.get("result", [])
    if not isinstance(entries, list):
        if warnings is not None:
            warnings.append(
                f"entity_registry/list returned unexpected shape for "
                f"config_entry_id={entry_id}"
            )
        return []
    return [e for e in entries if e.get("config_entry_id") == entry_id]


async def _entity_registry_update_coro(
    client: Any, entity_id: str, area_id: str | None, labels: list[str] | None
) -> Any:
    """Build and send a config/entity_registry/update WS message."""
    update_message: dict[str, Any] = {
        "type": "config/entity_registry/update",
        "entity_id": entity_id,
    }
    if area_id is not None:
        update_message["area_id"] = area_id if area_id else None
    if labels is not None:
        update_message["labels"] = labels
    return await client.send_websocket_message(update_message)


async def _category_apply_coro(
    client: Any, entity_id: str, category: str
) -> dict[str, Any]:
    """Apply category to entity and return the ack dict."""
    cat_ack: dict[str, Any] = {}
    await apply_entity_category(
        client, entity_id, category, "helpers", cat_ack, "helper"
    )
    return cat_ack


def _process_reg_update_result(
    reg_result: Any,
    area_id: str | None,
    labels: list[str] | None,
    applied: dict[str, Any],
    entity_id: str,
    warnings: list[str],
) -> None:
    """Update applied dict and warnings list based on entity_registry/update outcome."""
    if isinstance(reg_result, BaseException):
        warnings.append(f"{entity_id}: entity registry update raised: {reg_result}")
    elif reg_result is not None:
        if reg_result.get("success"):
            if area_id is not None:
                applied["area_id"] = area_id if area_id else None
            if labels is not None:
                applied["labels"] = labels
        else:
            warnings.append(
                f"{entity_id}: entity registry update failed: {_ws_error_msg(reg_result)}"
            )


def _process_cat_apply_result(
    cat_result: Any,
    entity_id: str,
    applied: dict[str, Any],
    warnings: list[str],
) -> None:
    """Update applied dict and warnings list based on category apply outcome."""
    if isinstance(cat_result, BaseException):
        warnings.append(f"{entity_id}: category apply raised: {cat_result}")
    elif cat_result is not None:
        if "category" in cat_result:
            applied["category"] = cat_result["category"]
        elif cat_result.get("warnings"):
            warnings.extend(f"{entity_id}: {w}" for w in cat_result["warnings"])


async def _apply_registry_updates_to_entity(
    client: Any,
    entity_id: str,
    area_id: str | None,
    labels: list[str] | None,
    category: str | None,
    warnings: list[str],
) -> dict[str, Any]:
    """Apply area_id/labels (single WS call) and category (shared helper) to one entity.

    Appends human-readable warning strings to `warnings` on any failure.
    Returns a small dict summarizing what was applied (for result building).
    """
    applied: dict[str, Any] = {"entity_id": entity_id}

    # Run the two independent registry calls concurrently.
    # `is not None` distinguishes "not provided" from "explicit clear" (empty
    # string / empty list). A transient raise on either call is captured via
    # return_exceptions so a multi-entity flow helper can still report partial success.
    needs_registry = area_id is not None or labels is not None
    needs_category = bool(category)
    if not (needs_registry or needs_category):
        return applied

    reg_task = (
        _entity_registry_update_coro(client, entity_id, area_id, labels)
        if needs_registry
        else None
    )
    cat_task = (
        _category_apply_coro(client, entity_id, category)  # type: ignore[arg-type]
        if needs_category
        else None
    )
    coros = [c for c in (reg_task, cat_task) if c is not None]
    raw_results: list[Any] = list(await asyncio.gather(*coros, return_exceptions=True))
    reg_result = raw_results.pop(0) if needs_registry else None
    cat_result = raw_results.pop(0) if needs_category else None

    _process_reg_update_result(
        reg_result, area_id, labels, applied, entity_id, warnings
    )
    _process_cat_apply_result(cat_result, entity_id, applied, warnings)
    return applied


class HelperResponse(TypedDict, total=False):
    """Uniform response contract for ``ha_config_set_helper`` (issue #1293).

    Documents the legal key set across all three branches (create, update,
    flow). ``total=False`` because per-branch fields (entity_id, flow extras,
    warnings) are conditional. Consumed by ``_helper_response`` below — all
    return literals in this module funnel through that builder so the shape
    has a single point of construction.
    """

    success: bool
    action: str  # "create" | "update"
    helper_type: str
    data: dict[str, Any]
    entity_id: str  # absent on flow branch (use entity_ids[] for multi-entity)
    message: str | None
    warnings: list[str]  # omitted when empty
    # Flow-helper convenience accessors (only set on the flow branch).
    method: str
    entry_id: str | None
    title: str | None
    updated: bool
    entity_ids: list[str]
    area_id: str | None
    labels: list[str]
    category: str
    applied: list[dict[str, Any]]


def _helper_response(
    action: str,
    helper_type: str,
    *,
    data: dict[str, Any],
    entity_id: str | None = None,
    message: str | None = None,
    warnings: list[str] | None = None,
    **extras: Any,
) -> dict[str, Any]:
    """Single construction point for the ``ha_config_set_helper`` response.

    Enforces the uniform shape from issue #1293: ``success`` → ``action`` →
    ``helper_type`` → ``data`` → ``entity_id`` (when present) → ``message`` →
    flow-helper extras → ``warnings`` (only when non-empty). Returning
    ``dict[str, Any]`` rather than ``HelperResponse`` keeps the call sites
    free of mypy gymnastics around the dynamic ``**extras`` keys; the
    TypedDict serves as the readable contract anchor instead.
    """
    resp: dict[str, Any] = {
        "success": True,
        "action": action,
        "helper_type": helper_type,
        "data": data,
    }
    if entity_id is not None:
        resp["entity_id"] = entity_id
    resp["message"] = message
    resp.update(extras)
    if warnings:
        resp["warnings"] = warnings
    return resp


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
    extras: dict[str, Any],
    warnings: list[str],
) -> None:
    """Apply area/labels/category to every entity from a flow helper, in parallel."""
    if not (
        entity_ids
        and (area_id is not None or labels_list is not None or category is not None)
    ):
        return
    applied_per_entity = list(
        await asyncio.gather(
            *(
                _apply_registry_updates_to_entity(
                    client, eid, area_id, labels_list, category, warnings
                )
                for eid in entity_ids
            )
        )
    )
    if area_id is not None:
        extras["area_id"] = area_id if area_id else None
    if labels_list is not None:
        extras["labels"] = labels_list
    if category:
        extras["category"] = category
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
    action: str | None = None,
) -> dict[str, Any]:
    """Create or update a flow-based helper and apply registry updates to all entities.

    Routes between create_flow_helper and update_flow_helper based on helper_id,
    then resolves the resulting config_entry_id to its entity(ies) and applies
    area_id / labels / category across the full set.

    For utility_meter with tariffs, this means the same label/area is applied
    to every tariff sensor (and the select entity) uniformly.

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
    await _validate_registry_ids(client, area_id, labels_list, category)

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
        client, entity_ids, area_id, labels_list, category, extras, warnings
    )

    return _helper_response(
        action,
        helper_type,
        data={"entry_id": entry_id, "title": title},
        message=flow_result.get("message"),
        warnings=warnings,
        **extras,
    )


def _format_schedule_days(
    monday: list | None,
    tuesday: list | None,
    wednesday: list | None,
    thursday: list | None,
    friday: list | None,
    saturday: list | None,
    sunday: list | None,
) -> dict[str, list[dict[str, Any]]]:
    """Format schedule day data, ensuring time strings include seconds.

    Returns a dict of day_name -> formatted time ranges, only for days
    where data was provided (not None).
    """
    day_params = {
        "monday": monday,
        "tuesday": tuesday,
        "wednesday": wednesday,
        "thursday": thursday,
        "friday": friday,
        "saturday": saturday,
        "sunday": sunday,
    }
    formatted_days: dict[str, list[dict[str, Any]]] = {}
    for day_name, day_schedule in day_params.items():
        if day_schedule is not None:
            formatted_ranges = []
            for time_range in day_schedule:
                formatted_range: dict[str, Any] = {}
                for key in ["from", "to"]:
                    if key in time_range:
                        time_val = time_range[key]
                        if isinstance(time_val, str) and time_val.count(":") == 1:
                            time_val = f"{time_val}:00"
                        formatted_range[key] = time_val
                if "data" in time_range:
                    formatted_range["data"] = time_range["data"]
                formatted_ranges.append(formatted_range)
            formatted_days[day_name] = formatted_ranges
    return formatted_days


# ---------------------------------------------------------------------------
# CREATE INFRASTRUCTURE
# ---------------------------------------------------------------------------

_SCHEDULE_DAYS: tuple[str, ...] = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


def _create_fields_input_select(
    options: list[str] | None, initial: Any, **_: Any
) -> dict[str, Any]:
    if not options:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "options list is required for input_select",
                context=_simple_helper_error_context("input_select"),
            )
        )
    if not isinstance(options, list) or len(options) == 0:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "options must be a non-empty list for input_select",
                context=_simple_helper_error_context("input_select"),
            )
        )
    fields: dict[str, Any] = {"options": options}
    _validate_initial_in_options(options, initial)
    if initial is not None:
        fields["initial"] = initial
    return fields


def _create_fields_input_number(
    min_value: float | None,
    max_value: float | None,
    step: float | None,
    unit_of_measurement: str | None,
    mode: str | None,
    initial: Any,
    **_: Any,
) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if min_value is not None:
        fields["min"] = min_value
    if max_value is not None:
        fields["max"] = max_value
    if step is not None:
        fields["step"] = step
    if unit_of_measurement:
        fields["unit_of_measurement"] = unit_of_measurement
    _validate_mode("input_number", mode)
    if mode is not None:
        fields["mode"] = mode
    if initial is not None:
        fields["initial"] = initial
    return fields


def _create_fields_input_text(
    min_value: float | None,
    max_value: float | None,
    mode: str | None,
    initial: Any,
    **_: Any,
) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if min_value is not None:
        fields["min"] = int(min_value)
    if max_value is not None:
        fields["max"] = int(max_value)
    _validate_mode("input_text", mode)
    if mode is not None:
        fields["mode"] = mode
    if initial is not None:
        fields["initial"] = initial
    return fields


def _create_fields_input_boolean(initial: Any, **_: Any) -> dict[str, Any]:
    if initial is None:
        return {}
    return {"initial": str(initial).lower() in ["true", "on", "yes", "1"]}


def _create_fields_input_datetime(
    has_date: bool | None,
    has_time: bool | None,
    initial: Any,
    **_: Any,
) -> dict[str, Any]:
    if has_date is None and has_time is None:
        fields: dict[str, Any] = {"has_date": True, "has_time": True}
    elif has_date is None:
        fields = {"has_date": False, "has_time": has_time}
    elif has_time is None:
        fields = {"has_date": has_date, "has_time": False}
    else:
        fields = {"has_date": has_date, "has_time": has_time}
    _validate_datetime_has_date_or_time(fields["has_date"], fields["has_time"])
    if initial is not None:
        fields["initial"] = initial
    return fields


def _create_fields_counter(
    initial: Any,
    min_value: float | None,
    max_value: float | None,
    step: float | None,
    restore: bool | None,
    **_: Any,
) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if initial is not None:
        fields["initial"] = int(initial) if isinstance(initial, str) else initial
    if min_value is not None:
        fields["minimum"] = int(min_value)
    if max_value is not None:
        fields["maximum"] = int(max_value)
    if step is not None:
        fields["step"] = int(step)
    if restore is not None:
        fields["restore"] = restore
    return fields


def _create_fields_timer(
    duration: str | None, restore: bool | None, **_: Any
) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if duration is not None:
        fields["duration"] = duration
    if restore is not None:
        fields["restore"] = restore
    return fields


def _create_fields_schedule(
    monday: list | None,
    tuesday: list | None,
    wednesday: list | None,
    thursday: list | None,
    friday: list | None,
    saturday: list | None,
    sunday: list | None,
    **_: Any,
) -> dict[str, Any]:
    formatted = _format_schedule_days(
        monday, tuesday, wednesday, thursday, friday, saturday, sunday
    )
    if not any(formatted.get(d) for d in _SCHEDULE_DAYS):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "schedule helper requires at least one day-of-week with at least one time range.",
                context=_simple_helper_error_context("schedule"),
                suggestions=[
                    'Pass e.g. monday=[{"from": "08:00", "to": "17:00"}]',
                    'Each day\'s value is a list of {"from": "HH:MM", "to": "HH:MM"} dicts',
                ],
            )
        )
    return formatted


def _create_fields_zone(
    latitude: float | None,
    longitude: float | None,
    radius: float | None,
    passive: bool | None,
    **_: Any,
) -> dict[str, Any]:
    missing = []
    if latitude is None:
        missing.append("latitude")
    if longitude is None:
        missing.append("longitude")
    if missing:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"zone helper requires {' and '.join(missing)}.",
                context=_simple_helper_error_context("zone", missing_fields=missing),
                suggestions=[
                    "Pass latitude (float) and longitude (float)",
                    "Optionally pass radius (meters, default 100) and passive (bool)",
                ],
            )
        )
    fields: dict[str, Any] = {"latitude": latitude, "longitude": longitude}
    if radius is not None:
        fields["radius"] = radius
    if passive is not None:
        fields["passive"] = passive
    return fields


def _create_fields_person(
    user_id: str | None,
    device_trackers: list[str] | None,
    picture: str | None,
    **_: Any,
) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if user_id:
        fields["user_id"] = user_id
    if device_trackers:
        fields["device_trackers"] = device_trackers
    if picture:
        fields["picture"] = picture
    return fields


def _create_fields_tag(
    tag_id: str | None, description: str | None, **_: Any
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "tag_id": tag_id if tag_id is not None else uuid.uuid4().hex
    }
    if description:
        fields["description"] = description
    return fields


_SIMPLE_CREATE_FIELD_BUILDERS: dict[str, Callable[..., dict[str, Any]]] = {
    "input_select": _create_fields_input_select,
    "input_number": _create_fields_input_number,
    "input_text": _create_fields_input_text,
    "input_boolean": _create_fields_input_boolean,
    "input_datetime": _create_fields_input_datetime,
    "counter": _create_fields_counter,
    "timer": _create_fields_timer,
    "schedule": _create_fields_schedule,
    "zone": _create_fields_zone,
    "person": _create_fields_person,
    "tag": _create_fields_tag,
}


def _build_create_message(
    helper_type: str, name: str, icon: str | None, **kw: Any
) -> dict[str, Any]:
    """Build the WebSocket {type}/create message for a simple helper."""
    message: dict[str, Any] = {"type": f"{helper_type}/create", "name": name}
    if icon and helper_type not in ("person", "tag"):
        message["icon"] = icon
    builder = _SIMPLE_CREATE_FIELD_BUILDERS.get(helper_type)
    if builder is not None:
        message.update(builder(**kw))
    return message


async def _apply_create_entity_registry(
    client: Any,
    entity_id: str,
    icon: str | None,
    area_id: str | None,
    labels: list[str] | None,
    helper_data: dict[str, Any],
    warnings: list[str],
) -> None:
    """Apply area/labels registry update after a simple-helper create; echo into helper_data."""
    if area_id is None and labels is None:
        return
    update_message: dict[str, Any] = {
        "type": "config/entity_registry/update",
        "entity_id": entity_id,
    }
    if area_id is not None:
        update_message["area_id"] = area_id if area_id else None
    if labels is not None:
        update_message["labels"] = labels
    update_result = await client.send_websocket_message(update_message)
    if update_result.get("success"):
        if icon is not None:
            helper_data["icon"] = icon if icon else None
        if area_id is not None:
            helper_data["area_id"] = area_id if area_id else None
        if labels is not None:
            helper_data["labels"] = labels
    else:
        warnings.append(
            f"Helper created but entity registry update failed: {_ws_error_msg(update_result)}"
        )


async def _apply_create_category(
    client: Any,
    entity_id: str,
    category: str | None,
    helper_data: dict[str, Any],
    warnings: list[str],
) -> None:
    """Apply category to a newly created helper entity."""
    if not (category and entity_id):
        return
    cat_result: dict[str, Any] = {}
    await apply_entity_category(
        client, entity_id, category, "helpers", cat_result, "helper"
    )
    if "category" in cat_result:
        helper_data["category"] = cat_result["category"]
    elif cat_result.get("warnings"):
        warnings.extend(cat_result["warnings"])


async def _execute_create_simple_helper(
    client: Any,
    helper_type: str,
    name: str | None,
    icon: str | None,
    area_id: str | None,
    labels: list[str] | None,
    category: str | None,
    wait: bool,
    MandatoryBPS: bool,
    **kw: Any,
) -> dict[str, Any]:
    """Execute the create path for a simple (non-flow) helper type."""
    if not name or not name.strip():
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"name is required for create action. Include "
                f'"name" as a top-level argument, e.g. '
                f'{{"helper_type": "{helper_type}", "name": "My Helper"}}.',
                suggestions=[
                    'Add "name": "My Helper" at the top level of the JSON arguments',
                    'Or pass "helper_id": "my_helper" if you intended to update an existing helper',
                ],
                context=_simple_helper_error_context(helper_type),
            )
        )

    message = _build_create_message(helper_type, name, icon, **kw)
    result = await client.send_websocket_message(message)
    if not result.get("success"):
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Failed to create helper: {result.get('error', 'Unknown error')}",
                context=_simple_helper_error_context(helper_type, name=name),
            )
        )

    helper_data = result.get("result", {})
    entity_id = helper_data.get("entity_id")
    if not entity_id and helper_data.get("id"):
        entity_id = f"{helper_type}.{helper_data['id']}"

    warnings: list[str] = []
    # Tags live in their own tag registry and never appear in /api/states/<entity_id> —
    # polling there always 404s for the full timeout (~10s per tag), burning CI time.
    if wait and entity_id and helper_type != "tag":
        try:
            registered = await wait_for_entity_registered(client, entity_id)
            if not registered:
                warnings.append(
                    f"Helper created but {entity_id} not yet queryable. It may take a moment to become available."
                )
        except Exception as e:
            warnings.append(f"Helper created but verification failed: {e}")

    if entity_id:
        await _apply_create_entity_registry(
            client, entity_id, icon, area_id, labels, helper_data, warnings
        )
        await _apply_create_category(client, entity_id, category, helper_data, warnings)

    create_response = _helper_response(
        "create",
        helper_type,
        data=helper_data,
        entity_id=entity_id,
        message=f"Successfully created {helper_type}: {name}",
        warnings=warnings,
    )
    _attach_helper_skill(create_response, MandatoryBPS)
    return create_response


# ---------------------------------------------------------------------------
# UPDATE INFRASTRUCTURE
# ---------------------------------------------------------------------------


def _update_fields_input_select(
    existing: dict[str, Any],
    options: list[str] | None,
    initial: Any,
    **_: Any,
) -> dict[str, Any]:
    merged_options = options if options is not None else existing.get("options", [])
    initial_val = initial if initial is not None else existing.get("initial")
    _validate_initial_in_options(merged_options, initial_val)
    fields: dict[str, Any] = {"options": merged_options}
    if initial_val is not None:
        fields["initial"] = initial_val
    return fields


def _update_fields_input_number(
    existing: dict[str, Any],
    min_value: float | None,
    max_value: float | None,
    step: float | None,
    unit_of_measurement: str | None,
    mode: str | None,
    initial: Any,
    **_: Any,
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "min": min_value if min_value is not None else existing.get("min", 0),
        "max": max_value if max_value is not None else existing.get("max", 100),
    }
    step_val = step if step is not None else existing.get("step")
    if step_val is not None:
        fields["step"] = step_val
    unit_val = (
        unit_of_measurement
        if unit_of_measurement is not None
        else existing.get("unit_of_measurement")
    )
    if unit_val is not None:
        fields["unit_of_measurement"] = unit_val
    _validate_mode("input_number", mode)
    mode_val = mode if mode is not None else existing.get("mode")
    if mode_val is not None:
        fields["mode"] = mode_val
    initial_val = initial if initial is not None else existing.get("initial")
    if initial_val is not None:
        fields["initial"] = initial_val
    return fields


def _update_fields_input_text(
    existing: dict[str, Any],
    min_value: float | None,
    max_value: float | None,
    mode: str | None,
    initial: Any,
    **_: Any,
) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    min_val = int(min_value) if min_value is not None else existing.get("min")
    if min_val is not None:
        fields["min"] = min_val
    max_val = int(max_value) if max_value is not None else existing.get("max")
    if max_val is not None:
        fields["max"] = max_val
    _validate_mode("input_text", mode)
    mode_val = mode if mode is not None else existing.get("mode")
    if mode_val is not None:
        fields["mode"] = mode_val
    initial_val = initial if initial is not None else existing.get("initial")
    if initial_val is not None:
        fields["initial"] = initial_val
    return fields


def _update_fields_input_boolean(
    existing: dict[str, Any], initial: Any, **_: Any
) -> dict[str, Any]:
    if initial is not None:
        return {"initial": str(initial).lower() in ["true", "on", "yes", "1"]}
    if "initial" in existing:
        return {"initial": existing["initial"]}
    return {}


def _update_fields_input_datetime(
    existing: dict[str, Any],
    has_date: bool | None,
    has_time: bool | None,
    initial: Any,
    **_: Any,
) -> dict[str, Any]:
    merged_has_date = (
        has_date if has_date is not None else existing.get("has_date", False)
    )
    merged_has_time = (
        has_time if has_time is not None else existing.get("has_time", False)
    )
    _validate_datetime_has_date_or_time(merged_has_date, merged_has_time)
    fields: dict[str, Any] = {"has_date": merged_has_date, "has_time": merged_has_time}
    initial_val = initial if initial is not None else existing.get("initial")
    if initial_val is not None:
        fields["initial"] = initial_val
    return fields


def _update_fields_counter(
    existing: dict[str, Any],
    initial: Any,
    min_value: float | None,
    max_value: float | None,
    step: float | None,
    restore: bool | None,
    **_: Any,
) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    initial_val = int(initial) if initial is not None else existing.get("initial")
    if initial_val is not None:
        fields["initial"] = initial_val
    minimum_val = int(min_value) if min_value is not None else existing.get("minimum")
    if minimum_val is not None:
        fields["minimum"] = minimum_val
    maximum_val = int(max_value) if max_value is not None else existing.get("maximum")
    if maximum_val is not None:
        fields["maximum"] = maximum_val
    step_val = int(step) if step is not None else existing.get("step")
    if step_val is not None:
        fields["step"] = step_val
    restore_val = restore if restore is not None else existing.get("restore")
    if restore_val is not None:
        fields["restore"] = restore_val
    return fields


def _update_fields_timer(
    existing: dict[str, Any],
    duration: str | None,
    restore: bool | None,
    **_: Any,
) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    duration_val = duration if duration is not None else existing.get("duration")
    if duration_val is not None:
        fields["duration"] = duration_val
    restore_val = restore if restore is not None else existing.get("restore")
    if restore_val is not None:
        fields["restore"] = restore_val
    return fields


_SIMPLE_UPDATE_FIELD_BUILDERS: dict[str, Callable[..., dict[str, Any]]] = {
    "input_select": _update_fields_input_select,
    "input_number": _update_fields_input_number,
    "input_text": _update_fields_input_text,
    "input_boolean": _update_fields_input_boolean,
    "input_datetime": _update_fields_input_datetime,
    "counter": _update_fields_counter,
    "timer": _update_fields_timer,
}


def _build_standard_update_message(
    helper_type: str,
    unique_id: str,
    existing: dict[str, Any],
    name: str | None,
    icon: str | None,
    **kw: Any,
) -> dict[str, Any]:
    """Build the {type}/update WS message for standard input_* types.

    HA's storage-collection update is full-replace (not patch): all vol.Required
    fields must be present even for partial updates, so callers fetch the existing
    config first and merge — passing the new value if provided, else preserving
    the existing value.
    """
    message: dict[str, Any] = {
        "type": f"{helper_type}/update",
        f"{helper_type}_id": unique_id,
        "name": name if name is not None else existing.get("name"),
    }
    if helper_type not in ("person", "tag"):
        icon_val = icon if icon is not None else existing.get("icon")
        if icon_val is not None:
            message["icon"] = icon_val
    builder = _SIMPLE_UPDATE_FIELD_BUILDERS.get(helper_type)
    if builder is not None:
        message.update(builder(existing=existing, icon=icon, **kw))
    return message


async def _execute_person_config_update(
    client: Any,
    entity_id: str,
    unique_id: str,
    name: str | None,
    user_id: str | None,
    device_trackers: list[str] | None,
    picture: str | None,
) -> dict[str, Any]:
    """Update a person entity via person/update (full-replace — merges with existing)."""
    list_result = await client.send_websocket_message({"type": "person/list"})
    if not list_result.get("success"):
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Failed to fetch person config list: {list_result.get('error', 'Unknown')}",
                context=_simple_helper_error_context("person", entity_id=entity_id),
            )
        )
    person_result = list_result.get("result", {})
    person_list = (
        person_result.get("storage", [])
        if isinstance(person_result, dict)
        else person_result
    )
    current_config = next(
        (p for p in person_list if isinstance(p, dict) and p.get("id") == unique_id),
        None,
    )
    if not current_config:
        raise_tool_error(
            create_error_response(
                ErrorCode.CONFIG_NOT_FOUND,
                f"Person config not found for id: {unique_id}",
                context=_simple_helper_error_context("person", entity_id=entity_id),
            )
        )
    update_msg: dict[str, Any] = {
        "type": "person/update",
        "person_id": unique_id,
        "name": name if name is not None else current_config.get("name"),
        "user_id": user_id if user_id is not None else current_config.get("user_id"),
        "device_trackers": device_trackers
        if device_trackers is not None
        else current_config.get("device_trackers", []),
    }
    if picture is not None:
        update_msg["picture"] = picture
    elif current_config.get("picture"):
        update_msg["picture"] = current_config["picture"]
    result = await client.send_websocket_message(update_msg)
    if not result.get("success"):
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Failed to update person config: {result.get('error', 'Unknown error')}",
                context=_simple_helper_error_context("person", entity_id=entity_id),
            )
        )
    return result.get("result", {})  # type: ignore[no-any-return]


async def _execute_zone_config_update(
    client: Any,
    entity_id: str,
    unique_id: str,
    name: str | None,
    latitude: float | None,
    longitude: float | None,
    radius: float | None,
    passive: bool | None,
) -> dict[str, Any]:
    """Update a zone entity via zone/update."""
    update_msg: dict[str, Any] = {"type": "zone/update", "zone_id": unique_id}
    if name is not None:
        update_msg["name"] = name
    if latitude is not None:
        update_msg["latitude"] = latitude
    if longitude is not None:
        update_msg["longitude"] = longitude
    if radius is not None:
        update_msg["radius"] = radius
    if passive is not None:
        update_msg["passive"] = passive
    result = await client.send_websocket_message(update_msg)
    if not result.get("success"):
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Failed to update zone config: {result.get('error', 'Unknown error')}",
                context=_simple_helper_error_context("zone", entity_id=entity_id),
            )
        )
    return result.get("result", {})  # type: ignore[no-any-return]


async def _execute_schedule_config_update(
    client: Any,
    entity_id: str,
    unique_id: str,
    name: str | None,
    icon: str | None,
    monday: list | None,
    tuesday: list | None,
    wednesday: list | None,
    thursday: list | None,
    friday: list | None,
    saturday: list | None,
    sunday: list | None,
) -> dict[str, Any]:
    """Update a schedule entity via schedule/update."""
    update_msg: dict[str, Any] = {"type": "schedule/update", "schedule_id": unique_id}
    if name is not None:
        update_msg["name"] = name
    if icon is not None:
        update_msg["icon"] = icon
    update_msg.update(
        _format_schedule_days(
            monday, tuesday, wednesday, thursday, friday, saturday, sunday
        )
    )
    result = await client.send_websocket_message(update_msg)
    if not result.get("success"):
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Failed to update schedule config: {result.get('error', 'Unknown error')}",
                context=_simple_helper_error_context("schedule", entity_id=entity_id),
            )
        )
    return result.get("result", {})  # type: ignore[no-any-return]


async def _execute_standard_helper_update(
    client: Any,
    helper_type: str,
    entity_id: str,
    unique_id: str,
    name: str | None,
    icon: str | None,
    **kw: Any,
) -> dict[str, Any]:
    """Fetch existing config, merge caller values, and POST update for input_* types."""
    list_result = await client.send_websocket_message({"type": f"{helper_type}/list"})
    if not list_result.get("success"):
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Failed to fetch {helper_type} config list: {list_result.get('error', 'Unknown')}",
                context=_simple_helper_error_context(helper_type, entity_id=entity_id),
            )
        )
    existing = next(
        (
            item
            for item in list_result.get("result", [])
            if isinstance(item, dict) and item.get("id") == unique_id
        ),
        None,
    )
    if not existing:
        raise_tool_error(
            create_error_response(
                ErrorCode.CONFIG_NOT_FOUND,
                f"{helper_type} config not found for id: {unique_id}",
                context=_simple_helper_error_context(helper_type, entity_id=entity_id),
            )
        )
    update_msg = _build_standard_update_message(
        helper_type, unique_id, existing, name, icon, **kw
    )
    result = await client.send_websocket_message(update_msg)
    if not result.get("success"):
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Failed to update {helper_type} config: {result.get('error', 'Unknown error')}",
                context=_simple_helper_error_context(helper_type, entity_id=entity_id),
            )
        )
    return result.get("result", {})  # type: ignore[no-any-return]


_CONFIG_STORE_TYPES: frozenset[str] = frozenset(
    {
        "person",
        "zone",
        "schedule",
        "input_select",
        "input_number",
        "input_text",
        "input_boolean",
        "input_datetime",
        "counter",
        "timer",
        "input_button",
    }
)


async def _execute_config_store_update(
    client: Any,
    helper_type: str,
    entity_id: str,
    unique_id: str,
    name: str | None,
    icon: str | None,
    **kw: Any,
) -> dict[str, Any]:
    """Dispatch a config-store helper update to the appropriate per-type executor."""
    if helper_type == "person":
        return await _execute_person_config_update(
            client,
            entity_id,
            unique_id,
            name,
            kw.get("user_id"),
            kw.get("device_trackers"),
            kw.get("picture"),
        )
    if helper_type == "zone":
        return await _execute_zone_config_update(
            client,
            entity_id,
            unique_id,
            name,
            kw.get("latitude"),
            kw.get("longitude"),
            kw.get("radius"),
            kw.get("passive"),
        )
    if helper_type == "schedule":
        return await _execute_schedule_config_update(
            client,
            entity_id,
            unique_id,
            name,
            icon,
            kw.get("monday"),
            kw.get("tuesday"),
            kw.get("wednesday"),
            kw.get("thursday"),
            kw.get("friday"),
            kw.get("saturday"),
            kw.get("sunday"),
        )
    return await _execute_standard_helper_update(
        client, helper_type, entity_id, unique_id, name, icon, **kw
    )


async def _resolve_update_unique_id(
    client: Any,
    helper_type: str,
    entity_id: str,
    helper_id: str | None,
    name: str | None,
) -> str:
    """Look up the unique_id for a helper entity via the entity registry."""
    registry_result = await client.send_websocket_message(
        {
            "type": "config/entity_registry/get",
            "entity_id": entity_id,
        }
    )
    if not registry_result.get("success"):
        suggestions = [
            f"Verify the helper_id={helper_id!r} exists "
            "(use ha_config_list_helpers to list current helpers)",
        ]
        if name:
            suggestions.append(
                f"If you meant to create a new helper named {name!r}, "
                "omit helper_id (or pass action='create')"
            )
        raise_tool_error(
            create_error_response(
                ErrorCode.ENTITY_NOT_FOUND,
                f"Could not find {helper_type} entity: {entity_id}",
                context=_simple_helper_error_context(
                    helper_type, entity_id=entity_id, helper_id=helper_id, name=name
                ),
                suggestions=suggestions,
            )
        )
    registry_entry = registry_result.get("result", {})
    if not isinstance(registry_entry, dict):
        raise_tool_error(
            create_error_response(
                ErrorCode.INTERNAL_ERROR,
                f"Unexpected registry response for {entity_id}",
                context=_simple_helper_error_context(helper_type, entity_id=entity_id),
            )
        )
    unique_id = registry_entry.get("unique_id")
    if not unique_id:
        raise_tool_error(
            create_error_response(
                ErrorCode.CONFIG_NOT_FOUND,
                f"No unique_id found in entity registry for {entity_id}",
                context=_simple_helper_error_context(helper_type, entity_id=entity_id),
            )
        )
    return unique_id  # type: ignore[no-any-return]


async def _apply_update_icon_area_labels(
    client: Any,
    entity_id: str,
    icon: str | None,
    area_id: str | None,
    labels: list[str] | None,
    updated_data: dict[str, Any],
    warnings: list[str],
) -> None:
    """Apply icon/area/labels to the entity registry after a helper update."""
    if icon is None and area_id is None and labels is None:
        return
    registry_update: dict[str, Any] = {
        "type": "config/entity_registry/update",
        "entity_id": entity_id,
    }
    if icon is not None:
        registry_update["icon"] = icon if icon else None
    if area_id is not None:
        registry_update["area_id"] = area_id if area_id else None
    if labels is not None:
        registry_update["labels"] = labels
    reg_result = await client.send_websocket_message(registry_update)
    if reg_result.get("success"):
        if icon is not None:
            updated_data["icon"] = icon if icon else None
        if area_id is not None:
            updated_data["area_id"] = area_id if area_id else None
        if labels is not None:
            updated_data["labels"] = labels
    else:
        warnings.append(
            f"Config updated but entity registry update failed: {_ws_error_msg(reg_result)}"
        )


async def _apply_update_registry_and_category(
    client: Any,
    entity_id: str,
    icon: str | None,
    area_id: str | None,
    labels: list[str] | None,
    category: str | None,
    updated_data: dict[str, Any],
    warnings: list[str],
) -> None:
    """Apply icon/area/labels/category to the entity registry after a helper update."""
    await _apply_update_icon_area_labels(
        client, entity_id, icon, area_id, labels, updated_data, warnings
    )

    if category:
        cat_result: dict[str, Any] = {}
        await apply_entity_category(
            client, entity_id, category, "helpers", cat_result, "helper"
        )
        if "category" in cat_result:
            updated_data["category"] = cat_result["category"]
        elif cat_result.get("warnings"):
            warnings.extend(cat_result["warnings"])


async def _execute_fallback_registry_update(
    client: Any,
    helper_type: str,
    entity_id: str,
    name: str | None,
    icon: str | None,
    area_id: str | None,
    labels: list[str] | None,
    category: str | None,
    warnings: list[str],
) -> dict[str, Any]:
    """Update an unknown/future helper type via entity registry update only."""
    fallback_msg: dict[str, Any] = {
        "type": "config/entity_registry/update",
        "entity_id": entity_id,
    }
    if name is not None:
        fallback_msg["name"] = name if name else None
    if icon is not None:
        fallback_msg["icon"] = icon if icon else None
    if area_id is not None:
        fallback_msg["area_id"] = area_id if area_id else None
    if labels is not None:
        fallback_msg["labels"] = labels
    result = await client.send_websocket_message(fallback_msg)
    updated_data: dict[str, Any] = {}
    if result.get("success"):
        updated_data = result.get("result", {}).get("entity_entry", {})
    else:
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Failed to update helper: {result.get('error', 'Unknown error')}",
                context=_simple_helper_error_context(helper_type, entity_id=entity_id),
            )
        )
    if category:
        cat_result: dict[str, Any] = {}
        await apply_entity_category(
            client, entity_id, category, "helpers", cat_result, "helper"
        )
        if "category" in cat_result:
            updated_data["category"] = cat_result["category"]
        elif cat_result.get("warnings"):
            warnings.extend(cat_result["warnings"])
    return updated_data


async def _execute_update_simple_helper(
    client: Any,
    helper_type: str,
    entity_id: str,
    helper_id: str | None,
    name: str | None,
    icon: str | None,
    area_id: str | None,
    labels: list[str] | None,
    category: str | None,
    wait: bool,
    MandatoryBPS: bool,
    **kw: Any,
) -> dict[str, Any]:
    """Execute the update path for a simple (non-flow) helper type."""
    if not helper_id or not helper_id.strip():
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "helper_id is required for update action",
                context=_simple_helper_error_context(helper_type),
            )
        )

    warnings: list[str] = []
    updated_data: dict[str, Any] = {}

    if helper_type == "tag":
        tag_update_id = (
            helper_id.removeprefix("tag.")
            if helper_id.startswith("tag.")
            else helper_id
        )
        update_msg: dict[str, Any] = {"type": "tag/update", "tag_id": tag_update_id}
        if name is not None:
            update_msg["name"] = name
        if kw.get("description") is not None:
            update_msg["description"] = kw["description"]
        result = await client.send_websocket_message(update_msg)
        if not result.get("success"):
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Failed to update tag config: {result.get('error', 'Unknown error')}",
                    context=_simple_helper_error_context(
                        helper_type, entity_id=entity_id
                    ),
                )
            )
        tag_response = _helper_response(
            "update",
            helper_type,
            data=result.get("result", {}),
            entity_id=entity_id,
            message=f"Successfully updated {helper_type}: {entity_id}",
            warnings=warnings,
        )
        _attach_helper_skill(tag_response, MandatoryBPS)
        return tag_response

    if helper_type in _CONFIG_STORE_TYPES:
        unique_id = await _resolve_update_unique_id(
            client, helper_type, entity_id, helper_id, name
        )
        updated_data = await _execute_config_store_update(
            client, helper_type, entity_id, unique_id, name, icon, **kw
        )
        await _apply_update_registry_and_category(
            client, entity_id, icon, area_id, labels, category, updated_data, warnings
        )
    else:
        updated_data = await _execute_fallback_registry_update(
            client,
            helper_type,
            entity_id,
            name,
            icon,
            area_id,
            labels,
            category,
            warnings,
        )

    if wait:
        try:
            registered = await wait_for_entity_registered(client, entity_id)
            if not registered:
                warnings.append(f"Update applied but {entity_id} not yet queryable.")
        except Exception as e:
            warnings.append(f"Update applied but verification failed: {e}")

    update_response = _helper_response(
        "update",
        helper_type,
        data=updated_data,
        entity_id=entity_id,
        message=f"Successfully updated {helper_type}: {entity_id}",
        warnings=warnings,
    )
    _attach_helper_skill(update_response, MandatoryBPS)
    return update_response


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


async def _validate_set_helper_action(
    client: Any,
    action: str | None,
    helper_id: str | None,
    helper_type: str,
) -> str:
    """Validate and resolve the action for ha_config_set_helper."""
    if action is not None:
        if action == "create" and helper_id is not None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"action='create' was passed together with helper_id={helper_id!r}. "
                    "These are contradictory: create makes a new helper, while helper_id "
                    "targets an existing one.",
                    context=(
                        _simple_helper_error_context(
                            helper_type, action=action, helper_id=helper_id
                        )
                        if helper_type in SIMPLE_HELPER_TYPES
                        else await _flow_helper_error_context(
                            client, helper_type, action=action, helper_id=helper_id
                        )
                    ),
                    suggestions=[
                        "Omit helper_id to create a new helper",
                        "Or pass action='update' to modify the existing helper at helper_id",
                    ],
                )
            )
        if action == "update" and helper_id is None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "action='update' requires helper_id to identify which helper to modify.",
                    context=(
                        _simple_helper_error_context(helper_type, action=action)
                        if helper_type in SIMPLE_HELPER_TYPES
                        else await _flow_helper_error_context(
                            client, helper_type, action=action
                        )
                    ),
                    suggestions=[
                        'Pass "helper_id": "my_helper" to identify the helper',
                        "Or pass action='create' (or omit action) to create a new helper",
                    ],
                )
            )
        if action == "update" and helper_id is not None:
            validate_identifier_not_empty(
                helper_id,
                "helper_id",
                suggestions=[
                    "Pass a valid helper_id to identify the helper to update",
                    "Or omit helper_id and pass action='create' to create a new helper",
                ],
                context={"helper_type": helper_type, "action": action},
            )
        return action
    # Implicit discriminator (back-compat).
    if helper_id is not None:
        validate_identifier_not_empty(
            helper_id,
            "helper_id",
            suggestions=[
                "Omit helper_id entirely to create a new helper",
                "Pass a valid helper_id to update an existing helper",
                "Or pass action='create' / action='update' explicitly to declare intent",
            ],
            context={"helper_type": helper_type},
        )
    return "update" if helper_id else "create"


def _validate_pre_dispatch_params(
    helper_type: str,
    min_value: float | None,
    max_value: float | None,
    step: float | None,
    options: list[str] | None,
    monday: list | None,
    tuesday: list | None,
    wednesday: list | None,
    thursday: list | None,
    friday: list | None,
    saturday: list | None,
    sunday: list | None,
) -> None:
    """Run per-type schema validation before dispatching create or update."""
    if helper_type in ("input_number", "counter", "input_text"):
        _validate_numeric_range(helper_type, min_value, max_value, step)
    if helper_type == "input_select":
        _validate_input_select_options(options)
    if helper_type == "schedule":
        _validate_schedule_days(
            monday, tuesday, wednesday, thursday, friday, saturday, sunday
        )


# ---------------------------------------------------------------------------
# REGISTRATION
# ---------------------------------------------------------------------------


class HelperConfigTools:
    """Encapsulates helper configuration tools for ha_config_list_helpers and ha_config_set_helper."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @tool(
        name="ha_config_list_helpers",
        tags={"Helper Entities"},
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "title": "List Helpers",
        },
    )
    @log_tool_usage
    async def ha_config_list_helpers(
        self,
        helper_type: Annotated[
            Literal[
                "input_button",
                "input_boolean",
                "input_select",
                "input_number",
                "input_text",
                "input_datetime",
                "counter",
                "timer",
                "schedule",
                "zone",
                "person",
                "tag",
            ],
            Field(description="Type of helper entity to list"),
        ],
    ) -> dict[str, Any]:
        """
        List all Home Assistant helpers of a specific type with their configurations.

        Returns complete configuration for all helpers of the specified type including:
        - ID, name, icon
        - Type-specific settings (min/max for input_number, options for input_select, etc.)
        - Area and label assignments

        SUPPORTED HELPER TYPES:
        - input_button: Virtual buttons for triggering automations
        - input_boolean: Toggle switches/checkboxes
        - input_select: Dropdown selection lists
        - input_number: Numeric sliders/input boxes
        - input_text: Text input fields
        - input_datetime: Date/time pickers
        - counter: Counters with increment/decrement/reset
        - timer: Countdown timers with start/pause/cancel
        - schedule: Weekly schedules with time ranges (on/off per day)
        - zone: Geographical zones for presence detection
        - person: Person entities linked to device trackers
        - tag: NFC/QR tags for automation triggers

        EXAMPLES:
        - List all number helpers: ha_config_list_helpers("input_number")
        - List all counters: ha_config_list_helpers("counter")
        - List all zones: ha_config_list_helpers("zone")
        - List all persons: ha_config_list_helpers("person")
        - List all tags: ha_config_list_helpers("tag")

        **NOTE:** This only returns storage-based helpers (created via UI/API), not YAML-defined helpers.

        Flow-based types (template / group / utility_meter / derivative / etc.)
        cannot be listed via this tool — use ``ha_search_entities`` or
        ``ha_deep_search`` for those.

        For detailed helper documentation, use ha_get_skill_guide.
        """
        try:
            result = await self._client.send_websocket_message(
                {"type": f"{helper_type}/list"}
            )
            if result.get("success"):
                items = result.get("result", [])
                return {
                    "success": True,
                    "helper_type": helper_type,
                    "count": len(items),
                    "helpers": items,
                    "message": f"Found {len(items)} {helper_type} helper(s)",
                }
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Failed to list helpers: {result.get('error', 'Unknown error')}",
                    context={"helper_type": helper_type},
                )
            )
        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error listing helpers: {e}")
            exception_to_structured_error(
                e,
                context={"helper_type": helper_type},
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify WebSocket connection is active",
                    "Use ha_search_entities(domain_filter='input_*') as alternative",
                ],
            )

    @tool(
        name="ha_config_set_helper",
        tags={"Helper Entities"},
        annotations={"destructiveHint": True, "title": "Create or Update Helper"},
    )
    @with_auto_backup(
        domain_fn=lambda kw: f"helper_{kw.get('helper_type', 'unknown')}",
        id_fn=lambda kw: str(
            kw.get("helper_id") or kw.get("entry_id") or kw.get("subentry_id") or ""
        ),
    )
    @log_tool_usage
    async def ha_config_set_helper(
        self,
        helper_type: Annotated[
            Literal[
                "counter",
                "config_subentry",
                "derivative",
                "filter",
                "generic_hygrostat",
                "generic_thermostat",
                "group",
                "input_boolean",
                "input_button",
                "input_datetime",
                "input_number",
                "input_select",
                "input_text",
                "integration",
                "min_max",
                "person",
                "random",
                "schedule",
                "statistics",
                "switch_as_x",
                "tag",
                "template",
                "threshold",
                "timer",
                "tod",
                "trend",
                "utility_meter",
                "zone",
            ],
            Field(description="Type of helper entity to create or update"),
        ],
        name: Annotated[
            str | None,
            Field(
                description=(
                    "Display name for simple/flow helper creation. Required when "
                    "creating a helper without helper_id. Optional on helper update. "
                    "Ignored for helper_type='config_subentry', which uses "
                    "entry_id/subentry_type/subentry_id instead. For flow-based "
                    "helper updates (template, group, utility_meter, ...), this is "
                    "typically ignored because options flows don't expose renaming. "
                    "Rename a flow helper by deleting and recreating instead."
                ),
                default=None,
            ),
        ] = None,
        helper_id: Annotated[
            str | None,
            Field(
                description="REQUIRED when updating an existing helper. Bare ID ('my_button') or full entity ID ('input_button.my_button'). Omit to create a new helper.",
                default=None,
            ),
        ] = None,
        entry_id: Annotated[
            str | None,
            Field(
                description=(
                    "Parent config entry ID when helper_type='config_subentry'. "
                    "Use ha_get_integration() to find entry IDs."
                ),
                default=None,
            ),
        ] = None,
        subentry_type: Annotated[
            str | None,
            Field(
                description=(
                    "Integration-defined subentry type when "
                    "helper_type='config_subentry'."
                ),
                default=None,
            ),
        ] = None,
        subentry_id: Annotated[
            str | None,
            Field(
                description=(
                    "Existing config subentry ID to reconfigure when "
                    "helper_type='config_subentry'. Omit to create."
                ),
                default=None,
            ),
        ] = None,
        show_advanced_options: Annotated[
            bool,
            Field(
                description=(
                    "When helper_type='config_subentry', ask Home Assistant "
                    "to expose advanced flow options."
                ),
                default=False,
            ),
        ] = False,
        icon: Annotated[
            str | None,
            Field(
                description="Material Design Icon (e.g., 'mdi:bell', 'mdi:toggle-switch')",
                default=None,
            ),
        ] = None,
        area_id: Annotated[
            str | None,
            Field(description="Area/room ID to assign the helper to", default=None),
        ] = None,
        labels: Annotated[
            str | list[str] | None,
            Field(description="Labels to categorize the helper", default=None),
        ] = None,
        min_value: Annotated[
            float | None,
            Field(
                description="Minimum value (input_number/counter) or minimum length (input_text). Also accepts shorthand 'min'.",
                default=None,
                validation_alias=AliasChoices("min_value", "min"),
            ),
        ] = None,
        max_value: Annotated[
            float | None,
            Field(
                description="Maximum value (input_number/counter) or maximum length (input_text). Also accepts shorthand 'max'.",
                default=None,
                validation_alias=AliasChoices("max_value", "max"),
            ),
        ] = None,
        step: Annotated[
            float | None,
            Field(
                description="Step/increment value for input_number or counter",
                default=None,
            ),
        ] = None,
        unit_of_measurement: Annotated[
            str | None,
            Field(
                description="Unit of measurement for input_number (e.g., '°C', '%', 'W'). Also accepts shorthand 'unit'.",
                default=None,
                validation_alias=AliasChoices("unit_of_measurement", "unit"),
            ),
        ] = None,
        options: Annotated[
            str | list[str] | None,
            Field(
                description="List of options for input_select (required for input_select)",
                default=None,
            ),
        ] = None,
        initial: Annotated[
            str | int | None,
            Field(
                description="Initial value for the helper (input_select, input_text, input_boolean, input_datetime, counter)",
                default=None,
            ),
        ] = None,
        mode: Annotated[
            str | None,
            Field(
                description="Display mode: 'box'/'slider' for input_number, 'text'/'password' for input_text",
                default=None,
            ),
        ] = None,
        has_date: Annotated[
            bool | None,
            Field(
                description="Include date component for input_datetime", default=None
            ),
        ] = None,
        has_time: Annotated[
            bool | None,
            Field(
                description="Include time component for input_datetime", default=None
            ),
        ] = None,
        restore: Annotated[
            bool | None,
            Field(
                description="Restore state after restart (counter, timer). Defaults to True for counter, False for timer",
                default=None,
            ),
        ] = None,
        duration: Annotated[
            str | None,
            Field(
                description="Default duration for timer in format 'HH:MM:SS' or seconds (e.g., '0:05:00' for 5 minutes)",
                default=None,
            ),
        ] = None,
        monday: Annotated[
            list[dict[str, Any]] | None,
            Field(
                description="Schedule time ranges for Monday. List of {'from': 'HH:MM', 'to': 'HH:MM'} dicts. Optional 'data' dict for additional attributes (e.g. {'from': '07:00', 'to': '22:00', 'data': {'mode': 'comfort'}})",
                default=None,
            ),
        ] = None,
        tuesday: Annotated[
            list[dict[str, Any]] | None,
            Field(
                description="Schedule time ranges for Tuesday. List of {'from': 'HH:MM', 'to': 'HH:MM'} dicts. Optional 'data' dict for additional attributes.",
                default=None,
            ),
        ] = None,
        wednesday: Annotated[
            list[dict[str, Any]] | None,
            Field(
                description="Schedule time ranges for Wednesday. List of {'from': 'HH:MM', 'to': 'HH:MM'} dicts. Optional 'data' dict for additional attributes.",
                default=None,
            ),
        ] = None,
        thursday: Annotated[
            list[dict[str, Any]] | None,
            Field(
                description="Schedule time ranges for Thursday. List of {'from': 'HH:MM', 'to': 'HH:MM'} dicts. Optional 'data' dict for additional attributes.",
                default=None,
            ),
        ] = None,
        friday: Annotated[
            list[dict[str, Any]] | None,
            Field(
                description="Schedule time ranges for Friday. List of {'from': 'HH:MM', 'to': 'HH:MM'} dicts. Optional 'data' dict for additional attributes.",
                default=None,
            ),
        ] = None,
        saturday: Annotated[
            list[dict[str, Any]] | None,
            Field(
                description="Schedule time ranges for Saturday. List of {'from': 'HH:MM', 'to': 'HH:MM'} dicts. Optional 'data' dict for additional attributes.",
                default=None,
            ),
        ] = None,
        sunday: Annotated[
            list[dict[str, Any]] | None,
            Field(
                description="Schedule time ranges for Sunday. List of {'from': 'HH:MM', 'to': 'HH:MM'} dicts. Optional 'data' dict for additional attributes.",
                default=None,
            ),
        ] = None,
        latitude: Annotated[
            float | None,
            Field(description="Latitude for zone (required for zone)", default=None),
        ] = None,
        longitude: Annotated[
            float | None,
            Field(description="Longitude for zone (required for zone)", default=None),
        ] = None,
        radius: Annotated[
            float | None,
            Field(description="Radius in meters for zone (default: 100)", default=None),
        ] = None,
        passive: Annotated[
            bool | None,
            Field(
                description="Passive zone (won't trigger state changes for person entities)",
                default=None,
            ),
        ] = None,
        user_id: Annotated[
            str | None,
            Field(description="User ID to link to person entity", default=None),
        ] = None,
        device_trackers: Annotated[
            list[str] | None,
            Field(
                description="List of device_tracker entity IDs for person", default=None
            ),
        ] = None,
        picture: Annotated[
            str | None,
            Field(description="Picture URL for person entity", default=None),
        ] = None,
        tag_id: Annotated[
            str | None,
            Field(
                description=(
                    "Tag ID for tag. On create, omit to auto-generate a unique "
                    "uuid4 hex (HA's tag/create requires this field; the tool "
                    "fills it in for you). On update, the tag's existing tag_id "
                    "is required (passed via helper_id)."
                ),
                default=None,
            ),
        ] = None,
        description: Annotated[
            str | None,
            Field(description="Description for tag", default=None),
        ] = None,
        category: Annotated[
            str | None,
            Field(
                description="Category ID to assign to this helper. Use ha_config_get_category(scope='helpers') to list available categories, or ha_config_set_category() to create one.",
                default=None,
            ),
        ] = None,
        config: Annotated[
            dict[str, Any] | None,
            Field(
                description=(
                    "Config dict for flow-based helper types and "
                    "helper_type='config_subentry' "
                    "(template, group, utility_meter, derivative, min_max, threshold, "
                    "integration, statistics, trend, random, filter, tod, "
                    "generic_thermostat, switch_as_x, generic_hygrostat). "
                    "Ignored for simple helper types. "
                    "Field set is delivered as data_schema on the first validation error."
                ),
                default=None,
            ),
        ] = None,
        wait: Annotated[
            bool,
            Field(
                description="Wait for helper entity to be queryable before returning. Default: True. Set to False for bulk operations.",
                default=True,
            ),
        ] = True,
        action: Annotated[
            Literal["create", "update"] | None,
            Field(
                description=(
                    "Explicit intent: 'create' a new helper or 'update' an existing one. "
                    "When omitted, falls back to the implicit discriminator: presence of "
                    "helper_id => update, absence => create. Pass 'create' or 'update' "
                    "to disambiguate (e.g. so a typo in helper_id surfaces as a clear "
                    "'helper not found' error instead of being mistaken for a create call)."
                ),
                default=None,
            ),
        ] = None,
        MandatoryBPS: Annotated[
            bool,
            Field(default=True),
        ] = True,
    ) -> dict[str, Any]:
        """
        Create or update Home Assistant helper entities and config subentries
        (28 types, unified interface). MUST call ha_get_skill_guide first.

        SIMPLE/FLOW helper create requires `name`; SIMPLE/FLOW helper update
        requires `helper_id`. Config subentry create requires `entry_id` and
        `subentry_type`; config subentry update also requires `subentry_id`.

        SIMPLE types (structured params, WebSocket API): input_boolean, input_button,
        input_select, input_number, input_text, input_datetime, counter, timer, schedule,
        zone, person, tag.

        FLOW types (pass `config` dict, Config Entry Flow API): template, group,
        utility_meter, derivative, min_max, threshold, integration, statistics, trend,
        random, filter, tod, generic_thermostat, switch_as_x, generic_hygrostat.
        Note: `tod` is the purpose-built "is-current-time-in-range" indicator
        (supports cross-midnight ranges, unlike `schedule`).

        CONFIG_SUBENTRY type (Config Subentry Flow API): config_subentry.
        Pass `entry_id`, `subentry_type`, and `config`. Pass `subentry_id` to
        reconfigure an existing subentry; omit it to create a new subentry.

        For flow-type updates, pass the existing entry_id as `helper_id`. Options flows
        reject the `name` key on update — to rename a flow helper, delete and recreate.

        Behavior notes:
        - UPDATE preserves type-specific fields not re-passed (rename never wipes
          initial/icon/etc. for any simple helper).
        - Pass `action="create"` or `action="update"` to disambiguate intent.
          For SIMPLE/FLOW helpers, omitted action falls back to the implicit
          `helper_id`-presence discriminator. For config subentries, omitted
          action falls back to the `subentry_id`-presence discriminator.
        - For flow-based helpers, config keys not declared by any step's
          data_schema are silently ignored by HA; submit once and the
          validation error returns the `data_schema` for that helper so
          subsequent calls use the correct field names.
        - Validation errors raised by this tool carry the helper's
          `data_schema` in the response context (and `menu_options` for
          menu-rooted helpers like `template`/`group` when no sub-type is
          chosen yet) so a follow-up call can self-correct without a
          separate schema-discovery round-trip.

        EXAMPLES (menu-based types + tod, where first-call payload is non-obvious):
        - template sensor:
            ha_config_set_helper(helper_type="template", name="Room Temp",
                config={"next_step_id": "sensor",
                        "state": "{{ states('sensor.x')|float }}",
                        "unit_of_measurement": "°C"})
        - group (light):
            ha_config_set_helper(helper_type="group", name="Kitchen Lights",
                config={"group_type": "light",
                        "entities": ["light.a", "light.b"]})
        - tod (time-of-day indicator, cross-midnight OK):
            ha_config_set_helper(helper_type="tod", name="Quiet Hours",
                config={"after_time": "22:00:00", "before_time": "07:00:00"})
        - config subentry (create under an existing integration):
            ha_config_set_helper(helper_type="config_subentry",
                entry_id="01HXYZ...", subentry_type="conversation",
                config={"name": "Local agent", "model": "gemma3:27b"})

        ``helper-selection.md`` ships in this response under
        ``skill_content`` by default — decision
        matrix for picking the right helper type plus worked examples
        and per-type field tables. For deeper helper-design guidance
        beyond what ships here, call ha_get_skill_guide.
        """
        try:
            if helper_type == "config_subentry":
                return await _handle_set_config_subentry(
                    self._client,
                    action,
                    entry_id,
                    subentry_type,
                    subentry_id,
                    show_advanced_options,
                    config,
                    MandatoryBPS,
                )

            action = await _validate_set_helper_action(
                self._client, action, helper_id, helper_type
            )  # type: ignore[assignment]

            # Bug 4b/7c/10/14 (issue #1150): reject typed params that don't apply
            # to the chosen helper_type instead of silently dropping them.
            _validate_applicable_params(
                helper_type,
                {
                    "icon": icon,
                    "min_value": min_value,
                    "max_value": max_value,
                    "step": step,
                    "unit_of_measurement": unit_of_measurement,
                    "options": options,
                    "initial": initial,
                    "mode": mode,
                    "has_date": has_date,
                    "has_time": has_time,
                    "restore": restore,
                    "duration": duration,
                    "monday": monday,
                    "tuesday": tuesday,
                    "wednesday": wednesday,
                    "thursday": thursday,
                    "friday": friday,
                    "saturday": saturday,
                    "sunday": sunday,
                    "latitude": latitude,
                    "longitude": longitude,
                    "radius": radius,
                    "passive": passive,
                    "user_id": user_id,
                    "device_trackers": device_trackers,
                    "picture": picture,
                    "tag_id": tag_id,
                    "description": description,
                },
            )

            # The `config` parameter only applies to flow-based types; reject early
            # so the caller realizes simple types use explicit params, not `config`.
            if helper_type not in FLOW_HELPER_TYPES and config not in (None, {}, ""):
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"The 'config' parameter is only valid for flow-based helper types. "
                        f"For '{helper_type}', use the explicit parameters (name, options, min_value, etc.).",
                        context=_simple_helper_error_context(helper_type),
                        suggestions=[
                            f"Pass values for '{helper_type}' via explicit parameters (e.g. options=..., min_value=...)",
                            "For flow-based types (template, group, utility_meter, ...), use 'config' as a dict or JSON string",
                        ],
                    )
                )

            # Bug 12: detect name collision before sending so the caller isn't silently given a duplicate.
            if action == "create":
                await _check_name_collision(self._client, helper_type, name)

            if helper_type in FLOW_HELPER_TYPES:
                flow_response = await _handle_flow_helper(
                    self._client,
                    helper_type,
                    name,
                    helper_id,
                    config,
                    area_id,
                    labels,
                    category,
                    wait,
                    action,
                )
                _attach_helper_skill(flow_response, MandatoryBPS)
                return flow_response

            try:
                labels = parse_string_list_param(labels, "labels")
                options = parse_string_list_param(options, "options")
            except ValueError as e:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"Invalid list parameter: {e}",
                    )
                )

            # Bug 16 (issue #1150): validate area_id / labels / category exist.
            await _validate_registry_ids(self._client, area_id, labels, category)

            # Bug 13/17 (issue #1150): pre-validate per-type schema constraints.
            _validate_pre_dispatch_params(
                helper_type,
                min_value,
                max_value,
                step,
                options,
                monday,
                tuesday,
                wednesday,
                thursday,
                friday,
                saturday,
                sunday,
            )

            type_kw: dict[str, Any] = {
                "options": options,
                "initial": initial,
                "min_value": min_value,
                "max_value": max_value,
                "step": step,
                "unit_of_measurement": unit_of_measurement,
                "mode": mode,
                "has_date": has_date,
                "has_time": has_time,
                "restore": restore,
                "duration": duration,
                "monday": monday,
                "tuesday": tuesday,
                "wednesday": wednesday,
                "thursday": thursday,
                "friday": friday,
                "saturday": saturday,
                "sunday": sunday,
                "latitude": latitude,
                "longitude": longitude,
                "radius": radius,
                "passive": passive,
                "user_id": user_id,
                "device_trackers": device_trackers,
                "picture": picture,
                "tag_id": tag_id,
                "description": description,
            }

            if action == "create":
                return await _execute_create_simple_helper(
                    self._client,
                    helper_type,
                    name,
                    icon,
                    area_id,
                    labels,
                    category,
                    wait,
                    MandatoryBPS,
                    **type_kw,
                )

            if action != "update":
                raise_tool_error(
                    create_error_response(
                        ErrorCode.INTERNAL_ERROR,
                        f"Unexpected action: {action}",
                    )
                )

            # helper_id is guaranteed non-None by _validate_set_helper_action for update
            hid: str = helper_id  # type: ignore[assignment]
            entity_id = hid if hid.startswith(helper_type) else f"{helper_type}.{hid}"
            return await _execute_update_simple_helper(
                self._client,
                helper_type,
                entity_id,
                hid,
                name,
                icon,
                area_id,
                labels,
                category,
                wait,
                MandatoryBPS,
                **type_kw,
            )

        except ToolError as te:
            raise augment_tool_error_with_skill_content(te, bp_warnings=None) from None
        except Exception as e:
            error = exception_to_structured_error(
                e,
                context={"action": action, "helper_type": helper_type},
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify helper_id exists for update operations",
                    "Ensure required parameters are provided for the helper type",
                ],
                raise_error=False,
            )
            augment_error_dict_with_skill_content(error, bp_warnings=None)
            raise_tool_error(error)


def register_config_helper_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant helper configuration tools."""
    register_tool_methods(mcp, HelperConfigTools(client))
