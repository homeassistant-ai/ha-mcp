"""
Configuration management tools for Home Assistant helpers.

This module provides tools for listing, creating, updating, and removing
Home Assistant helper entities (input_button, input_boolean, input_select,
input_number, input_text, input_datetime, counter, timer, schedule).
"""

import asyncio
import logging
import uuid
from typing import Annotated, Any, Literal, TypedDict

from fastmcp.exceptions import ToolError
from pydantic import AliasChoices, Field

from ..client.rest_client import HomeAssistantAPIError
from ..errors import ErrorCode, create_error_response
from .helpers import exception_to_structured_error, log_tool_usage, raise_tool_error
from .tools_config_entry_flow import (
    FLOW_HELPER_TYPES,
    create_flow_helper,
    fetch_helper_data_schema,
    get_user_step_field_names,
    update_flow_helper,
)
from .util_helpers import (
    apply_entity_category,
    coerce_bool_param,
    parse_json_param,
    parse_string_list_param,
    wait_for_entity_registered,
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
# Consumed by:
#   - ``ha_get_helper_schema`` (returned verbatim for simple types).
#   - ``ha_config_set_helper`` validation errors (relevant entry attached to
#     ``context["data_schema"]`` so the LLM sees field shape inline with the
#     4xx that just blocked it).
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
    ``_fetch_data_schema_for_error_context`` in ``tools_config_entry_flow``).
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
# FORM — for these, ``fetch_helper_data_schema`` cannot return a ``data_schema``
# without a menu choice (``next_step_id`` / ``group_type`` / ``menu_option``).
# The pre-flow gates in ``_handle_flow_helper`` use this set to surface a
# ``data_schema_unavailable_reason: "menu_helper_requires_branch"`` marker so
# the LLM gets a non-silent signal to call
# ``ha_get_helper_schema(<type>, menu_option=...)``. Hint set — extending it
# only sharpens the signal, missing entries fall back to silent ``None``.
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
    marker is added instead so the LLM gets a non-silent signal to call
    ``ha_get_helper_schema(<type>, menu_option=...)`` rather than reading
    the absence of ``data_schema`` as "no schema exists".
    """
    context: dict[str, Any] = {"helper_type": helper_type}
    try:
        schema = await fetch_helper_data_schema(
            client, helper_type, menu_choice=menu_choice
        )
    except Exception as e:
        # Mirror the breadcrumb in ``abort_config_flow``'s own swallow
        # (tools_config_entry_flow), so a fetch failure here doesn't
        # disappear silently — this PR raises the call rate by 5 sites
        # and the swallow needs an audit-trail entry.
        logger.debug(
            "_flow_helper_error_context: schema fetch failed for "
            "helper_type=%r menu_choice=%r: %s",
            helper_type,
            menu_choice,
            e,
        )
        schema = None
    if schema is not None:
        context["data_schema"] = schema
    elif helper_type in _MENU_ROOTED_FLOW_HELPER_TYPES and not menu_choice:
        context["data_schema_unavailable_reason"] = "menu_helper_requires_branch"
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
            "config (use ha_get_helper_schema to see fields), "
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
            "goes inside the `config` dict; the per-type fields are discoverable "
            f"via ha_get_helper_schema(helper_type='{helper_type}')."
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
    # Early-out: nothing to validate.
    needs_area = area_id is not None and area_id != ""
    needs_labels = bool(labels)
    needs_category = category is not None and category != ""
    if not (needs_area or needs_labels or needs_category):
        return

    async def _ws_list(
        message: dict[str, Any],
    ) -> tuple[bool, list[dict[str, Any]]]:
        """Return (ok, items). ``ok=False`` means the lookup itself failed
        (HA unreachable, auth lost, registry not implemented). ``ok=True``
        with empty list means the registry exists and is genuinely empty —
        distinct from failure so we can still reject phantom IDs against an
        empty registry. The fail-open ``ok=False`` path keeps transient HA
        outages from blocking legitimate calls.
        """
        try:
            result = await client.send_websocket_message(message)
        except Exception:
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

    def _id_set(items: list[dict[str, Any]], field: str) -> list[str]:
        """Pull non-empty string values of `field` from a list of dicts."""
        return [
            v
            for it in items
            if isinstance(it, dict) and isinstance((v := it.get(field)), str)
        ]

    # Run the three registry lookups concurrently — they're independent and
    # each is a separate WS round-trip.
    lookups: list[tuple[str, Any]] = []
    if needs_area:
        lookups.append(("area", _ws_list({"type": "config/area_registry/list"})))
    if needs_labels:
        lookups.append(("labels", _ws_list({"type": "config/label_registry/list"})))
    if needs_category:
        lookups.append(
            (
                "category",
                _ws_list({"type": "config/category_registry/list", "scope": "helpers"}),
            )
        )
    raw = await asyncio.gather(*(coro for _, coro in lookups))
    by_param = {key: result for (key, _), result in zip(lookups, raw, strict=True)}

    # Validate area_id (single value).
    if needs_area:
        ok, areas = by_param["area"]
        valid_area_ids = _id_set(areas, "area_id")
        if ok and area_id not in valid_area_ids:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"area_id={area_id!r} does not exist in the area registry.",
                    context={"area_id": area_id},
                    suggestions=[
                        "Use ha_config_list_areas() to list valid area IDs.",
                        'Pass area_id="" to clear the area assignment.',
                        f"Available area_ids: {sorted(valid_area_ids)}",
                    ],
                )
            )

    # Validate labels (list of values).
    if needs_labels:
        ok, ws_labels = by_param["labels"]
        valid_label_ids = _id_set(ws_labels, "label_id")
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

    # Validate category (single value).
    if needs_category:
        ok, categories = by_param["category"]
        valid_category_ids = _id_set(categories, "category_id")
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

    Empty / missing `name` is left to the existing name-required check downstream
    so the user sees the standard "name is required" error rather than a
    spurious collision miss.
    """
    if not name:
        return

    target_slug = _slugify_helper_name(name)
    if not target_slug:
        # Name normalises to empty (e.g. all punctuation). HA's create call
        # will reject; let it surface that error rather than guessing.
        return

    existing_id: str | None = None

    if helper_type in FLOW_HELPER_TYPES:
        # Flow helpers live in the config-entry registry. Filter by domain so
        # we only see entries created via this helper_type's flow.
        try:
            result = await client.send_websocket_message(
                {
                    "type": "config_entries/get",
                    "domain": helper_type,
                }
            )
        except (HomeAssistantAPIError, ConnectionError, TimeoutError):
            # Connectivity issue — skip the check; HA will still suffix on its
            # own and we'll fail open rather than block legit creates.
            return
        entries = result.get("result", []) if isinstance(result, dict) else result
        if not isinstance(entries, list):
            return
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            title = entry.get("title")
            if isinstance(title, str) and _slugify_helper_name(title) == target_slug:
                existing_id = entry.get("entry_id") or entry.get("id")
                break
    else:
        # Simple helpers expose a {type}/list WS command. Most types return
        # entries with an `id` field (the slug HA derived from the name) plus
        # `name`. Tags differ: their primary key is `tag_id` (UUID hex, not a
        # slug), so the slug match below never fires and tag duplicates are
        # caught by the ``_slugify_helper_name(existing_name)`` branch further
        # down in this function.
        try:
            result = await client.send_websocket_message(
                {"type": f"{helper_type}/list"}
            )
        except (HomeAssistantAPIError, ConnectionError, TimeoutError):
            # Connectivity issue — skip the check; HA will still suffix on its
            # own and we'll fail open rather than block legit creates.
            return
        items: list[Any] = []
        if isinstance(result, dict):
            inner = result.get("result", [])
            # person/list returns {"storage": [...], "config": [...]}; flatten.
            if isinstance(inner, dict):
                for key in ("storage", "config"):
                    sub = inner.get(key)
                    if isinstance(sub, list):
                        items.extend(sub)
            elif isinstance(inner, list):
                items = inner
        elif isinstance(result, list):
            items = result
        for item in items:
            if not isinstance(item, dict):
                continue
            existing_slug = item.get("id") or item.get("tag_id")
            if isinstance(existing_slug, str) and existing_slug == target_slug:
                existing_id = existing_slug
                break
            existing_name = item.get("name")
            if (
                isinstance(existing_name, str)
                and _slugify_helper_name(existing_name) == target_slug
            ):
                existing_id = item.get("id") or item.get("tag_id") or target_slug
                break

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
            error_detail = result.get("error", "Unknown error")
            error_msg = (
                error_detail.get("message", str(error_detail))
                if isinstance(error_detail, dict)
                else str(error_detail)
            )
            warnings.append(
                f"entity_registry/list failed for config_entry_id={entry_id}: "
                f"{error_msg}"
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

    # Run the two independent registry calls concurrently:
    # 1. config/entity_registry/update for area_id + labels (combined)
    # 2. apply_entity_category for category (separate WS shape).
    # `is not None` distinguishes "not provided" from "explicit clear" (empty
    # string / empty list). Mirrors ha_set_entity. A transient raise on either
    # call is captured via return_exceptions so a multi-entity flow helper
    # (e.g. utility_meter with N tariffs) can still report partial success.
    needs_registry = area_id is not None or labels is not None
    needs_category = bool(category)
    if not (needs_registry or needs_category):
        return applied

    async def _do_registry_update() -> Any:
        update_message: dict[str, Any] = {
            "type": "config/entity_registry/update",
            "entity_id": entity_id,
        }
        if area_id is not None:
            update_message["area_id"] = area_id if area_id else None
        if labels is not None:
            update_message["labels"] = labels
        return await client.send_websocket_message(update_message)

    async def _do_category_apply() -> dict[str, Any]:
        cat_ack: dict[str, Any] = {}
        # `category` is non-None whenever we entered this branch (needs_category).
        assert category is not None
        await apply_entity_category(
            client, entity_id, category, "helpers", cat_ack, "helper"
        )
        return cat_ack

    reg_task = _do_registry_update() if needs_registry else None
    cat_task = _do_category_apply() if needs_category else None
    coros = [c for c in (reg_task, cat_task) if c is not None]
    raw_results: list[Any] = list(await asyncio.gather(*coros, return_exceptions=True))
    reg_result = raw_results.pop(0) if needs_registry else None
    cat_result = raw_results.pop(0) if needs_category else None

    # Handle entity_registry/update outcome.
    if isinstance(reg_result, BaseException):
        warnings.append(f"{entity_id}: entity registry update raised: {reg_result}")
    elif reg_result is not None:
        if reg_result.get("success"):
            if area_id is not None:
                applied["area_id"] = area_id if area_id else None
            if labels is not None:
                applied["labels"] = labels
        else:
            error_detail = reg_result.get("error", {})
            error_msg = (
                error_detail.get("message", "Unknown error")
                if isinstance(error_detail, dict)
                else str(error_detail)
            )
            warnings.append(f"{entity_id}: entity registry update failed: {error_msg}")

    # Handle category outcome.
    if isinstance(cat_result, BaseException):
        warnings.append(f"{entity_id}: category apply raised: {cat_result}")
    elif cat_result is not None:
        if "category" in cat_result:
            applied["category"] = cat_result["category"]
        elif "category_warning" in cat_result:
            warnings.append(f"{entity_id}: {cat_result['category_warning']}")

    return applied


async def _handle_flow_helper(
    client: Any,
    helper_type: str,
    name: str | None,
    helper_id: str | None,
    config: str | dict | None,
    area_id: str | None,
    labels: str | list[str] | None,
    category: str | None,
    wait: bool | str,
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
    if action is None:
        action = "update" if helper_id else "create"

    # Normalize empty string to None, matching ha_config_set_helper's treatment
    # of config in (None, {}, "") as "nothing passed" (L785 simple-type branch).
    # Without this, parse_json_param("") raises a confusing 'Invalid JSON' error.
    if config == "":
        config = None

    # Normalize config into a dict (accepts JSON string or dict).
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
        config_dict: dict[str, Any] = parsed
    elif isinstance(config, dict):
        config_dict = dict(config)  # shallow copy — we may mutate
    elif config is None:
        config_dict = {}
    else:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"config must be a dict or JSON string, got {type(config).__name__}",
                context=await _flow_helper_error_context(client, helper_type),
            )
        )

    # Pre-flow warnings (e.g. stripped `name` on update) collected here and
    # surfaced alongside any later warnings on the result.
    pre_warnings: list[str] = []

    # Name handling differs between create and update flows:
    #
    # CREATE: most flow helpers accept `name` as a top-level form field, so the
    # tool folds the top-level `name` parameter into the form payload. But some
    # helpers — notably `switch_as_x` — derive the entity name from the source
    # switch and reject `name` as an extra key with HA-side 400 "extra keys not
    # allowed @ data['name']". Probe the user-step schema first; only inject if
    # the schema actually accepts a `name` field. If introspection fails or the
    # top step is a menu (template, group), fall back to the legacy behaviour
    # of injecting — those helpers are known to accept `name`.
    #
    # UPDATE: options flows are strict about extra keys; HA rejects any
    # caller-supplied `name` (you cannot rename a flow helper through its
    # options flow). Strip `name` from config_dict and emit a warning so the
    # caller learns their attempted rename was a no-op.
    if action == "create" and name and "name" not in config_dict:
        schema_fields = await get_user_step_field_names(client, helper_type)
        if schema_fields is None or "name" in schema_fields:
            config_dict["name"] = name
        # else: schema is a form that explicitly does not include `name`
        # (e.g. switch_as_x). Skip injection — HA would reject otherwise.
    elif action == "update" and "name" in config_dict:
        stripped_name = config_dict.pop("name")
        pre_warnings.append(
            f"Ignored 'name' in config: flow helper options flows do not "
            f"support renaming (attempted name={stripped_name!r}). Use "
            f"ha_set_entity to change the friendly name of the resulting "
            f"entity."
        )

    # Normalize labels to a list for registry updates below.
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

    # Bug 16 (issue #1150): validate registry IDs BEFORE creating the config
    # entry. If the IDs are invalid, fail fast — otherwise we'd succeed in
    # creating the helper but later silently persist phantom references on the
    # post-create entity-registry update.
    await _validate_registry_ids(client, area_id, labels_list, category)

    # Dispatch to the shared flow machinery.
    if action == "create":
        # Validate against EITHER the top-level `name` arg OR `config_dict["name"]`.
        # Some helpers (switch_as_x) deliberately don't have `name` injected into
        # config_dict because their schema rejects it — but the tool still
        # requires `name` to be supplied so callers fail fast and consistently.
        if not (name or config_dict.get("name")):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f'name is required for create action. Include "name" as a '
                    f'top-level argument, e.g. {{"helper_type": "{helper_type}", '
                    f'"name": "My Helper"}}.',
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
        # For updates, helper_id is the config entry_id (flow-based helpers)
        flow_result = await update_flow_helper(
            client,
            helper_type,
            config_dict,
            helper_id,  # type: ignore[arg-type]
        )

    entry_id = flow_result.get("entry_id")
    result: dict[str, Any] = {
        "success": True,
        "action": action,
        "helper_type": helper_type,
        "method": "config_flow",
        "entry_id": entry_id,
        "title": flow_result.get("title"),
        "message": flow_result.get("message"),
    }
    if action == "update":
        result["updated"] = True

    # Resolve all entities for this config entry (multi-entity helpers handled naturally).
    # For create with wait=True, poll briefly for at least one entity to appear —
    # otherwise a single fetch is enough (update keeps entities; create without wait
    # is caller-opted into not waiting).
    #
    # Graduated polling: short intervals for the first retries catch local/small
    # instances quickly; steady 500ms matches typical entity_registry/list latency
    # on larger remote setups without missing entities near the deadline.
    warnings: list[str] = list(pre_warnings)
    wait_bool = coerce_bool_param(wait, "wait", default=True)
    entities: list[dict[str, Any]] = []
    if entry_id:
        if action == "create" and wait_bool:
            deadline = 5.0
            intervals = [0.2, 0.3]  # first two retries faster
            steady_interval = 0.5
            elapsed = 0.0
            attempt = 0
            # Silent retries — a transient WS failure on attempt #1 often
            # recovers by the deadline, and 14 identical warnings would
            # just flood the response. Collect warnings only on the final
            # attempt, when we know the poll has truly given up.
            while elapsed < deadline:
                poll_warnings: list[str] = []
                entities = await _get_entities_for_config_entry(
                    client, entry_id, poll_warnings
                )
                if entities:
                    break
                step = (
                    intervals[attempt] if attempt < len(intervals) else steady_interval
                )
                await asyncio.sleep(step)
                elapsed += step
                attempt += 1
            # Polled out without finding entities — surface the last
            # attempt's warning so the caller sees why.
            if not entities and poll_warnings:
                warnings.extend(poll_warnings)
        else:
            entities = await _get_entities_for_config_entry(client, entry_id, warnings)
    entity_ids = [e["entity_id"] for e in entities if e.get("entity_id")]
    result["entity_ids"] = entity_ids

    # Apply registry updates (area_id / labels / category) to every entity.
    # Use `is not None` so an explicit empty value (area_id="" or labels=[])
    # reaches _apply_registry_updates_to_entity, which forwards the clear
    # semantics (area_id: None / labels: []) to Home Assistant.
    if entity_ids and (
        area_id is not None or labels_list is not None or category is not None
    ):
        # Apply per-entity updates concurrently — each entity's update is
        # independent, so a multi-entity helper (e.g. utility_meter with N
        # tariffs) finishes in one round-trip instead of N.
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
            result["area_id"] = area_id if area_id else None
        if labels_list is not None:
            result["labels"] = labels_list
        if category:
            result["category"] = category
        result["applied"] = applied_per_entity

    if warnings:
        result["warnings"] = warnings

    return result


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


def register_config_helper_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant helper configuration tools."""

    @mcp.tool(
        tags={"Helper Entities"},
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "title": "List Helpers",
        },
    )
    @log_tool_usage
    async def ha_config_list_helpers(
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

        For detailed helper documentation, use ha_get_skill_home_assistant_best_practices.
        """
        try:
            # Use the websocket list endpoint for the helper type
            message: dict[str, Any] = {
                "type": f"{helper_type}/list",
            }

            result = await client.send_websocket_message(message)

            if result.get("success"):
                items = result.get("result", [])
                return {
                    "success": True,
                    "helper_type": helper_type,
                    "count": len(items),
                    "helpers": items,
                    "message": f"Found {len(items)} {helper_type} helper(s)",
                }
            else:
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

    @mcp.tool(
        tags={"Helper Entities"},
        annotations={"destructiveHint": True, "title": "Create or Update Helper"},
    )
    @log_tool_usage
    async def ha_config_set_helper(
        helper_type: Annotated[
            Literal[
                "counter",
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
                    "REQUIRED when creating (no helper_id provided). Display name "
                    "for the helper. Optional on update — pass helper_id instead. "
                    "For flow-based helper types on update (template, group, "
                    "utility_meter, ...), this is typically ignored — options flows "
                    "don't expose renaming. Rename a flow helper by deleting and "
                    "recreating instead."
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
            Field(
                description="Latitude for zone (required for zone)",
                default=None,
            ),
        ] = None,
        longitude: Annotated[
            float | None,
            Field(
                description="Longitude for zone (required for zone)",
                default=None,
            ),
        ] = None,
        radius: Annotated[
            float | None,
            Field(
                description="Radius in meters for zone (default: 100)",
                default=None,
            ),
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
            Field(
                description="User ID to link to person entity",
                default=None,
            ),
        ] = None,
        device_trackers: Annotated[
            list[str] | None,
            Field(
                description="List of device_tracker entity IDs for person",
                default=None,
            ),
        ] = None,
        picture: Annotated[
            str | None,
            Field(
                description="Picture URL for person entity",
                default=None,
            ),
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
            Field(
                description="Description for tag",
                default=None,
            ),
        ] = None,
        category: Annotated[
            str | None,
            Field(
                description="Category ID to assign to this helper. Use ha_config_get_category(scope='helpers') to list available categories, or ha_config_set_category() to create one.",
                default=None,
            ),
        ] = None,
        config: Annotated[
            str | dict | None,
            Field(
                description=(
                    "Config dict for flow-based helper types "
                    "(template, group, utility_meter, derivative, min_max, threshold, "
                    "integration, statistics, trend, random, filter, tod, "
                    "generic_thermostat, switch_as_x, generic_hygrostat). "
                    "Accepts JSON string or dict. Ignored for simple helper types. "
                    "Use ha_get_helper_schema(helper_type) to discover required fields."
                ),
                default=None,
            ),
        ] = None,
        wait: Annotated[
            bool | str,
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
    ) -> dict[str, Any]:
        """
        Create or update Home Assistant helper entities (27 types, unified interface).

        Create requires `name`; update requires `helper_id`.

        SIMPLE types (structured params, WebSocket API): input_boolean, input_button,
        input_select, input_number, input_text, input_datetime, counter, timer, schedule,
        zone, person, tag.

        FLOW types (pass `config` dict, Config Entry Flow API): template, group,
        utility_meter, derivative, min_max, threshold, integration, statistics, trend,
        random, filter, tod, generic_thermostat, switch_as_x, generic_hygrostat.
        Note: `tod` is the purpose-built "is-current-time-in-range" indicator
        (supports cross-midnight ranges, unlike `schedule`).

        For flow-type updates, pass the existing entry_id as `helper_id`. Options flows
        reject the `name` key on update — to rename a flow helper, delete and recreate.

        Behavior notes:
        - UPDATE preserves type-specific fields not re-passed (rename never wipes
          initial/icon/etc. for any simple helper).
        - Pass `action="create"` or `action="update"` to disambiguate intent —
          without it the tool falls back to the implicit `helper_id`-presence
          discriminator.
        - For flow-based helpers, config keys not declared by any step's
          data_schema are silently ignored by HA; verify field names with
          `ha_get_helper_schema` before relying on them.
        - Validation errors raised by this tool carry the helper's
          `data_schema` in the response context so a follow-up call can
          self-correct. Calling `ha_get_helper_schema(helper_type)` ahead of
          time is therefore optional — the schema is delivered alongside the
          first 4xx if you call without it.

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

        For complex schemas and per-type parameter details, use ha_get_helper_schema.
        For broader helper-design guidance (when to pick which helper type, YAML
        examples), use ha_get_skill_home_assistant_best_practices — the skill's
        `helper-selection.md` reference covers the `input_*` family, `counter`,
        `timer`, and `schedule` with worked examples and a decision matrix.
        """
        try:
            # Determine if this is a create or update — set early so the
            # outer exception handler's context dict can reference it even
            # if an exception bubbles out of the flow-helper branch below.
            #
            # Bug 11 (issue #1150): the explicit `action` parameter lets the
            # caller declare intent unambiguously. Without it, we fall back to
            # the legacy implicit discriminator (presence of helper_id =>
            # update). The implicit fallback is back-compat for existing
            # callers; the explicit form is preferred because it lets us
            # validate intent contradictions (e.g. action="create" with a
            # helper_id passed by mistake) up front, before any WS round-trip
            # produces a confusing ENTITY_NOT_FOUND.
            if action is not None:
                # Explicit-intent path: validate the combination matches.
                if action == "create" and helper_id is not None:
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.VALIDATION_INVALID_PARAMETER,
                            "action='create' was passed together with "
                            f"helper_id={helper_id!r}. These are contradictory: "
                            "create makes a new helper, while helper_id targets "
                            "an existing one.",
                            context=(
                                _simple_helper_error_context(
                                    helper_type,
                                    action=action,
                                    helper_id=helper_id,
                                )
                                if helper_type in SIMPLE_HELPER_TYPES
                                else await _flow_helper_error_context(
                                    client,
                                    helper_type,
                                    action=action,
                                    helper_id=helper_id,
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
                            "action='update' requires helper_id to identify "
                            "which helper to modify.",
                            context=(
                                _simple_helper_error_context(
                                    helper_type,
                                    action=action,
                                )
                                if helper_type in SIMPLE_HELPER_TYPES
                                else await _flow_helper_error_context(
                                    client,
                                    helper_type,
                                    action=action,
                                )
                            ),
                            suggestions=[
                                'Pass "helper_id": "my_helper" to identify the helper',
                                "Or pass action='create' (or omit action) to create a new helper",
                            ],
                        )
                    )
            else:
                # Implicit discriminator (back-compat). Pass action='create'
                # or action='update' explicitly to avoid the inference.
                action = "update" if helper_id else "create"

            # Bug 4b/7c/10/14 (issue #1150): reject typed params that don't apply
            # to the chosen helper_type, instead of silently dropping them. Without
            # this, callers got `success: true` but their (mistakenly-passed) param
            # never made it into HA's config.
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

            # Simple helper types use explicit parameters (name, options, min_value, ...).
            # The `config` parameter only applies to flow-based types; silently ignoring
            # it here would let the caller believe the payload took effect. Done before
            # the collision check so we fail fast on bad inputs without a wasted WS call.
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

            # Bug 12: HA auto-suffixes duplicate names with `_2`/`_3`/...
            # Detect the slug collision before sending so a caller intending
            # to update an existing helper isn't silently given a duplicate.
            if action == "create":
                await _check_name_collision(client, helper_type, name)

            # Route flow-based helpers to Config Entry Flow API.
            # Simple helpers continue through the WebSocket {type}/create+update path below.
            if helper_type in FLOW_HELPER_TYPES:
                return await _handle_flow_helper(
                    client=client,
                    helper_type=helper_type,
                    name=name,
                    helper_id=helper_id,
                    config=config,
                    area_id=area_id,
                    labels=labels,
                    category=category,
                    wait=wait,
                    action=action,
                )

            # Parse JSON list parameters if provided as strings
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

            # Bug 16 (issue #1150): validate area_id / labels / category exist
            # in their respective registries before any registry-update WS call.
            # Without this, phantom IDs are silently persisted as dangling
            # references that confuse downstream UI and tools.
            await _validate_registry_ids(client, area_id, labels, category)

            # Bug 13/17 (issue #1150): pre-validate per-type schema constraints.
            # Done once for both create and update so the message is identical
            # regardless of action. HA's own errors here are cryptic
            # ("Unknown error", "Duplicate options are not allowed", per-day
            # range messages, broken sliders), so surface a clear error before
            # the WS round-trip.
            if helper_type in ("input_number", "counter", "input_text"):
                _validate_numeric_range(helper_type, min_value, max_value, step)
            if helper_type == "input_select":
                _validate_input_select_options(options)
            if helper_type == "schedule":
                _validate_schedule_days(
                    monday,
                    tuesday,
                    wednesday,
                    thursday,
                    friday,
                    saturday,
                    sunday,
                )

            if action == "create":
                if not name:
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.VALIDATION_INVALID_PARAMETER,
                            f"name is required for create action. Include "
                            f'"name" as a top-level argument, e.g. '
                            f'{{"helper_type": "{helper_type}", "name": '
                            f'"My Helper"}}.',
                            suggestions=[
                                'Add "name": "My Helper" at the top level of the JSON arguments',
                                'Or pass "helper_id": "my_helper" if you intended to update an existing helper',
                            ],
                            context=_simple_helper_error_context(helper_type),
                        )
                    )

                # Build create message based on helper type
                message: dict[str, Any] = {
                    "type": f"{helper_type}/create",
                    "name": name,
                }

                # Icon supported by most helpers except person and tag
                if icon and helper_type not in ("person", "tag"):
                    message["icon"] = icon

                # Type-specific parameters
                if helper_type == "input_select":
                    if not options:
                        raise_tool_error(
                            create_error_response(
                                ErrorCode.VALIDATION_INVALID_PARAMETER,
                                "options list is required for input_select",
                                context=_simple_helper_error_context(helper_type),
                            )
                        )
                    if not isinstance(options, list) or len(options) == 0:
                        raise_tool_error(
                            create_error_response(
                                ErrorCode.VALIDATION_INVALID_PARAMETER,
                                "options must be a non-empty list for input_select",
                                context=_simple_helper_error_context(helper_type),
                            )
                        )
                    message["options"] = options
                    # Bug 4a (issue #1150): if `initial` was passed but isn't
                    # one of the options, reject explicitly instead of silently
                    # dropping. The previous `if initial and initial in options`
                    # check stripped invalid initials with `success: true`.
                    if initial is not None:
                        if initial not in options:
                            raise_tool_error(
                                create_error_response(
                                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                                    f"initial={initial!r} must be one of options "
                                    f"{options!r} for input_select.",
                                    context=_simple_helper_error_context(
                                        helper_type,
                                        initial=initial,
                                        options=options,
                                    ),
                                    suggestions=[
                                        "Pick an `initial` value that's in `options`.",
                                        "Or omit `initial` so the entity starts unset.",
                                    ],
                                )
                            )
                        message["initial"] = initial

                elif helper_type == "input_number":
                    # Range/step validation handled centrally by
                    # _validate_numeric_range above (Bug 13).
                    if min_value is not None:
                        message["min"] = min_value
                    if max_value is not None:
                        message["max"] = max_value
                    if step is not None:
                        message["step"] = step
                    if unit_of_measurement:
                        message["unit_of_measurement"] = unit_of_measurement
                    _validate_mode(helper_type, mode)
                    if mode is not None:
                        message["mode"] = mode
                    if initial is not None:
                        message["initial"] = initial

                elif helper_type == "input_text":
                    if min_value is not None:
                        message["min"] = int(min_value)
                    if max_value is not None:
                        message["max"] = int(max_value)
                    _validate_mode(helper_type, mode)
                    if mode is not None:
                        message["mode"] = mode
                    # `is not None` so initial="" is honored; HA accepts empty.
                    if initial is not None:
                        message["initial"] = initial

                elif helper_type == "input_boolean":
                    if initial is not None:
                        initial_str = str(initial).lower()
                        message["initial"] = initial_str in [
                            "true",
                            "on",
                            "yes",
                            "1",
                        ]

                elif helper_type == "input_datetime":
                    # At least one of has_date or has_time must be True
                    if has_date is None and has_time is None:
                        # Default to both if not specified
                        message["has_date"] = True
                        message["has_time"] = True
                    elif has_date is None:
                        message["has_date"] = False
                        message["has_time"] = has_time
                    elif has_time is None:
                        message["has_date"] = has_date
                        message["has_time"] = False
                    else:
                        message["has_date"] = has_date
                        message["has_time"] = has_time

                    # Validate that at least one is True
                    if not message["has_date"] and not message["has_time"]:
                        raise_tool_error(
                            create_error_response(
                                ErrorCode.VALIDATION_INVALID_PARAMETER,
                                "At least one of has_date or has_time must be True for input_datetime",
                                context=_simple_helper_error_context(helper_type),
                            )
                        )

                    if initial is not None:
                        message["initial"] = initial

                elif helper_type == "counter":
                    if initial is not None:
                        message["initial"] = (
                            int(initial) if isinstance(initial, str) else initial
                        )
                    if min_value is not None:
                        message["minimum"] = int(min_value)
                    if max_value is not None:
                        message["maximum"] = int(max_value)
                    if step is not None:
                        message["step"] = int(step)
                    if restore is not None:
                        message["restore"] = restore

                elif helper_type == "timer":
                    # `is not None` so explicit "0:00:00" or 0 isn't dropped.
                    if duration is not None:
                        message["duration"] = duration
                    if restore is not None:
                        message["restore"] = restore

                elif helper_type == "schedule":
                    # Schedule parameters: monday-sunday with time ranges
                    # Each day is a list of {"from": "HH:MM:SS", "to": "HH:MM:SS"}
                    # with optional "data" dict for additional attributes
                    formatted = _format_schedule_days(
                        monday,
                        tuesday,
                        wednesday,
                        thursday,
                        friday,
                        saturday,
                        sunday,
                    )
                    # Bug 7a (issue #1150): a schedule with no time ranges on any
                    # day is an always-off entity — almost certainly not what the
                    # caller wanted. Reject so the caller realizes they must pass
                    # at least one day-of-week range.
                    if all(
                        not formatted.get(day)
                        for day in (
                            "monday",
                            "tuesday",
                            "wednesday",
                            "thursday",
                            "friday",
                            "saturday",
                            "sunday",
                        )
                    ):
                        raise_tool_error(
                            create_error_response(
                                ErrorCode.VALIDATION_INVALID_PARAMETER,
                                "schedule helper requires at least one day-of-week "
                                "with at least one time range.",
                                context=_simple_helper_error_context(helper_type),
                                suggestions=[
                                    'Pass e.g. monday=[{"from": "08:00", "to": "17:00"}]',
                                    'Each day\'s value is a list of {"from": "HH:MM", "to": "HH:MM"} dicts',
                                ],
                            )
                        )
                    message.update(formatted)

                elif helper_type == "zone":
                    # Bug 7b (issue #1150): pre-validate required fields with a
                    # clear tool-side error, instead of letting HA bubble its
                    # voluptuous "required key not provided" message.
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
                                context=_simple_helper_error_context(
                                    helper_type,
                                    missing_fields=missing,
                                ),
                                suggestions=[
                                    "Pass latitude (float) and longitude (float)",
                                    "Optionally pass radius (meters, default 100) and passive (bool)",
                                ],
                            )
                        )
                    message["latitude"] = latitude
                    message["longitude"] = longitude
                    if radius is not None:
                        message["radius"] = radius
                    if passive is not None:
                        message["passive"] = passive

                elif helper_type == "person":
                    # Person parameters: user_id, device_trackers, picture
                    if user_id:
                        message["user_id"] = user_id
                    if device_trackers:
                        message["device_trackers"] = device_trackers
                    if picture:
                        message["picture"] = picture

                elif helper_type == "tag":
                    # Tag parameters: tag_id, description
                    # Note: name goes into entity registry, not tag storage
                    # Bug 9 (issue #1150): HA's tag/create requires `tag_id`,
                    # rejecting omissions with a cryptic "Unknown error" 400.
                    # The tool's docstring (and tag_id Field description) say
                    # tag_id is auto-generated when missing — make that true.
                    if tag_id is None:
                        tag_id = uuid.uuid4().hex
                    message["tag_id"] = tag_id
                    if description:
                        message["description"] = description

                result = await client.send_websocket_message(message)

                if result.get("success"):
                    helper_data = result.get("result", {})
                    entity_id = helper_data.get("entity_id")
                    # Some helper types don't return entity_id — derive from result id
                    if not entity_id and helper_data.get("id"):
                        entity_id = f"{helper_type}.{helper_data['id']}"

                    # Wait for entity to be properly registered before proceeding
                    wait_bool = coerce_bool_param(wait, "wait", default=True)
                    if wait_bool and entity_id:
                        try:
                            registered = await wait_for_entity_registered(
                                client, entity_id
                            )
                            if not registered:
                                helper_data["warning"] = (
                                    f"Helper created but {entity_id} not yet queryable. It may take a moment to become available."
                                )
                        except Exception as e:
                            helper_data["warning"] = (
                                f"Helper created but verification failed: {e}"
                            )

                    # Update entity registry if area_id or labels specified
                    if (area_id is not None or labels is not None) and entity_id:
                        update_message: dict[str, Any] = {
                            "type": "config/entity_registry/update",
                            "entity_id": entity_id,
                        }
                        if area_id is not None:
                            update_message["area_id"] = area_id if area_id else None
                        if labels is not None:
                            update_message["labels"] = labels

                        update_result = await client.send_websocket_message(
                            update_message
                        )
                        if update_result.get("success"):
                            if area_id is not None:
                                helper_data["area_id"] = area_id if area_id else None
                            if labels is not None:
                                helper_data["labels"] = labels
                        else:
                            error_detail = update_result.get("error", {})
                            error_msg = (
                                error_detail.get("message", "Unknown error")
                                if isinstance(error_detail, dict)
                                else str(error_detail)
                            )
                            helper_data["warning"] = (
                                f"Helper created but entity registry update failed: {error_msg}"
                            )

                    # Apply category via shared helper (consistent with automations/scripts)
                    if category and entity_id:
                        await apply_entity_category(
                            client,
                            entity_id,
                            category,
                            "helpers",
                            helper_data,
                            "helper",
                        )

                    return {
                        "success": True,
                        "action": "create",
                        "helper_type": helper_type,
                        "helper_data": helper_data,
                        "entity_id": entity_id,
                        "message": f"Successfully created {helper_type}: {name}",
                    }
                else:
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.SERVICE_CALL_FAILED,
                            f"Failed to create helper: {result.get('error', 'Unknown error')}",
                            context=_simple_helper_error_context(
                                helper_type,
                                name=name,
                            ),
                        )
                    )

            elif action == "update":
                if not helper_id:
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.VALIDATION_INVALID_PARAMETER,
                            "helper_id is required for update action",
                            context=_simple_helper_error_context(helper_type),
                        )
                    )

                entity_id = (
                    helper_id
                    if helper_id.startswith(helper_type)
                    else f"{helper_type}.{helper_id}"
                )

                # Helper types that persist config in dedicated storage APIs
                # (not just the entity registry). Each type uses its own
                # {type}/update WebSocket command. Tags use their own
                # registry and don't have entity registry entries.
                config_store_types = {
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

                updated_data: dict[str, Any] = {}

                if helper_type == "tag":
                    # Tags use their own registry — no entity registry entries.
                    # The helper_id IS the tag_id (strip "tag." prefix if present).
                    tag_update_id = (
                        helper_id.removeprefix("tag.")
                        if helper_id.startswith("tag.")
                        else helper_id
                    )
                    update_msg: dict[str, Any] = {
                        "type": "tag/update",
                        "tag_id": tag_update_id,
                    }
                    if name is not None:
                        update_msg["name"] = name
                    if description is not None:
                        update_msg["description"] = description

                    result = await client.send_websocket_message(update_msg)
                    if not result.get("success"):
                        raise_tool_error(
                            create_error_response(
                                ErrorCode.SERVICE_CALL_FAILED,
                                f"Failed to update tag config: {result.get('error', 'Unknown error')}",
                                context=_simple_helper_error_context(
                                    helper_type,
                                    entity_id=entity_id,
                                ),
                            )
                        )
                    updated_data = result.get("result", {})

                    # Tags don't have entity registry entries, so return directly
                    # without wait_for_entity_registered (they're not entities).
                    return {
                        "success": True,
                        "action": "update",
                        "helper_type": helper_type,
                        "entity_id": entity_id,
                        "updated_data": updated_data,
                        "message": f"Successfully updated {helper_type}: {entity_id}",
                    }

                elif helper_type in config_store_types:
                    # Person and zone: look up unique_id from entity registry
                    registry_msg: dict[str, Any] = {
                        "type": "config/entity_registry/get",
                        "entity_id": entity_id,
                    }
                    registry_result = await client.send_websocket_message(registry_msg)
                    if not registry_result.get("success"):
                        # Bug 11 (issue #1150): if `name` was also passed, the
                        # caller may have intended a create but typoed
                        # helper_id. Surface that hypothesis explicitly so the
                        # error guides them to the right next call rather than
                        # leaving them confused by a bare ENTITY_NOT_FOUND.
                        suggestions = [
                            f"Verify the helper_id={helper_id!r} exists "
                            "(use ha_config_list_helpers to list current helpers)",
                        ]
                        if name:
                            suggestions.append(
                                f"If you meant to create a new helper named "
                                f"{name!r}, omit helper_id (or pass action='create')"
                            )
                        raise_tool_error(
                            create_error_response(
                                ErrorCode.ENTITY_NOT_FOUND,
                                f"Could not find {helper_type} entity: {entity_id}",
                                context=_simple_helper_error_context(
                                    helper_type,
                                    entity_id=entity_id,
                                    helper_id=helper_id,
                                    name=name,
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
                                context=_simple_helper_error_context(
                                    helper_type,
                                    entity_id=entity_id,
                                ),
                            )
                        )
                    unique_id = registry_entry.get("unique_id")
                    if not unique_id:
                        raise_tool_error(
                            create_error_response(
                                ErrorCode.CONFIG_NOT_FOUND,
                                f"No unique_id found in entity registry for {entity_id}",
                                context=_simple_helper_error_context(
                                    helper_type,
                                    entity_id=entity_id,
                                ),
                            )
                        )

                    if helper_type == "person":
                        # Person config API is full-replace (not patch):
                        # fetch current config, merge with new values, then send.
                        list_result = await client.send_websocket_message(
                            {"type": "person/list"}
                        )
                        if not list_result.get("success"):
                            raise_tool_error(
                                create_error_response(
                                    ErrorCode.SERVICE_CALL_FAILED,
                                    f"Failed to fetch person config list: {list_result.get('error', 'Unknown')}",
                                    context=_simple_helper_error_context(
                                        helper_type,
                                        entity_id=entity_id,
                                    ),
                                )
                            )

                        # person/list returns {"storage": [...], "config": [...]}
                        # "storage" contains UI-managed (editable) persons
                        person_result = list_result.get("result", {})
                        person_list = (
                            person_result.get("storage", [])
                            if isinstance(person_result, dict)
                            else person_result
                        )

                        current_config = next(
                            (
                                p
                                for p in person_list
                                if isinstance(p, dict) and p.get("id") == unique_id
                            ),
                            None,
                        )

                        if not current_config:
                            raise_tool_error(
                                create_error_response(
                                    ErrorCode.CONFIG_NOT_FOUND,
                                    f"Person config not found for id: {unique_id}",
                                    context=_simple_helper_error_context(
                                        helper_type,
                                        entity_id=entity_id,
                                    ),
                                )
                            )

                        # Merge: use new values if provided, else keep current
                        update_msg = {
                            "type": "person/update",
                            "person_id": unique_id,
                            "name": name
                            if name is not None
                            else current_config.get("name"),
                            "user_id": user_id
                            if user_id is not None
                            else current_config.get("user_id"),
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
                                    context=_simple_helper_error_context(
                                        helper_type,
                                        entity_id=entity_id,
                                    ),
                                )
                            )
                        updated_data = result.get("result", {})

                    elif helper_type == "zone":
                        update_msg = {
                            "type": "zone/update",
                            "zone_id": unique_id,
                        }
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
                                    context=_simple_helper_error_context(
                                        helper_type,
                                        entity_id=entity_id,
                                    ),
                                )
                            )
                        updated_data = result.get("result", {})

                    elif helper_type == "schedule":
                        update_msg = {
                            "type": "schedule/update",
                            "schedule_id": unique_id,
                        }
                        if name is not None:
                            update_msg["name"] = name
                        if icon is not None:
                            update_msg["icon"] = icon

                        update_msg.update(
                            _format_schedule_days(
                                monday,
                                tuesday,
                                wednesday,
                                thursday,
                                friday,
                                saturday,
                                sunday,
                            )
                        )

                        result = await client.send_websocket_message(update_msg)
                        if not result.get("success"):
                            raise_tool_error(
                                create_error_response(
                                    ErrorCode.SERVICE_CALL_FAILED,
                                    f"Failed to update schedule config: {result.get('error', 'Unknown error')}",
                                    context=_simple_helper_error_context(
                                        helper_type,
                                        entity_id=entity_id,
                                    ),
                                )
                            )
                        updated_data = result.get("result", {})

                    else:
                        # Standard input helpers: use {type}/update API
                        # to persist config changes (not just entity registry).
                        # HA's update schemas require all vol.Required fields
                        # even for partial updates, so fetch current config
                        # and backfill any fields the caller didn't provide.
                        list_result = await client.send_websocket_message(
                            {"type": f"{helper_type}/list"}
                        )
                        if not list_result.get("success"):
                            raise_tool_error(
                                create_error_response(
                                    ErrorCode.SERVICE_CALL_FAILED,
                                    f"Failed to fetch {helper_type} config list: {list_result.get('error', 'Unknown')}",
                                    context=_simple_helper_error_context(
                                        helper_type,
                                        entity_id=entity_id,
                                    ),
                                )
                            )
                        existing = next(
                            (
                                item
                                for item in list_result.get("result", [])
                                if isinstance(item, dict)
                                and item.get("id") == unique_id
                            ),
                            None,
                        )
                        if not existing:
                            raise_tool_error(
                                create_error_response(
                                    ErrorCode.CONFIG_NOT_FOUND,
                                    f"{helper_type} config not found for id: {unique_id}",
                                    context=_simple_helper_error_context(
                                        helper_type,
                                        entity_id=entity_id,
                                    ),
                                )
                            )

                        # HA's storage-collection update is full-replace, so per-type
                        # config fields below all merge: take the new value if
                        # the caller passed one, else preserve the existing value.
                        update_msg = {
                            "type": f"{helper_type}/update",
                            f"{helper_type}_id": unique_id,
                            "name": name if name is not None else existing.get("name"),
                        }
                        # Icon lives in the helper's storage entry for all simple
                        # types except person and tag; merge from existing so
                        # rename-style updates don't wipe the previously set icon.
                        if helper_type not in ("person", "tag"):
                            icon_val = (
                                icon if icon is not None else existing.get("icon")
                            )
                            if icon_val is not None:
                                update_msg["icon"] = icon_val

                        if helper_type == "input_select":
                            update_msg["options"] = (
                                options
                                if options is not None
                                else existing.get("options", [])
                            )
                            initial_val = (
                                initial
                                if initial is not None
                                else existing.get("initial")
                            )
                            if initial_val is not None:
                                update_msg["initial"] = initial_val

                        elif helper_type == "input_number":
                            update_msg["min"] = (
                                min_value
                                if min_value is not None
                                else existing.get("min", 0)
                            )
                            update_msg["max"] = (
                                max_value
                                if max_value is not None
                                else existing.get("max", 100)
                            )
                            step_val = (
                                step if step is not None else existing.get("step")
                            )
                            if step_val is not None:
                                update_msg["step"] = step_val
                            unit_val = (
                                unit_of_measurement
                                if unit_of_measurement is not None
                                else existing.get("unit_of_measurement")
                            )
                            if unit_val is not None:
                                update_msg["unit_of_measurement"] = unit_val
                            _validate_mode(helper_type, mode)
                            mode_val = (
                                mode if mode is not None else existing.get("mode")
                            )
                            if mode_val is not None:
                                update_msg["mode"] = mode_val
                            initial_val = (
                                initial
                                if initial is not None
                                else existing.get("initial")
                            )
                            if initial_val is not None:
                                update_msg["initial"] = initial_val

                        elif helper_type == "input_text":
                            min_val = (
                                int(min_value)
                                if min_value is not None
                                else existing.get("min")
                            )
                            if min_val is not None:
                                update_msg["min"] = min_val
                            max_val = (
                                int(max_value)
                                if max_value is not None
                                else existing.get("max")
                            )
                            if max_val is not None:
                                update_msg["max"] = max_val
                            _validate_mode(helper_type, mode)
                            mode_val = (
                                mode if mode is not None else existing.get("mode")
                            )
                            if mode_val is not None:
                                update_msg["mode"] = mode_val
                            initial_val = (
                                initial
                                if initial is not None
                                else existing.get("initial")
                            )
                            if initial_val is not None:
                                update_msg["initial"] = initial_val

                        elif helper_type == "input_boolean":
                            if initial is not None:
                                initial_str = str(initial).lower()
                                update_msg["initial"] = initial_str in [
                                    "true",
                                    "on",
                                    "yes",
                                    "1",
                                ]
                            elif "initial" in existing:
                                update_msg["initial"] = existing["initial"]

                        elif helper_type == "input_datetime":
                            update_msg["has_date"] = (
                                has_date
                                if has_date is not None
                                else existing.get("has_date", False)
                            )
                            update_msg["has_time"] = (
                                has_time
                                if has_time is not None
                                else existing.get("has_time", False)
                            )
                            initial_val = (
                                initial
                                if initial is not None
                                else existing.get("initial")
                            )
                            if initial_val is not None:
                                update_msg["initial"] = initial_val

                        elif helper_type == "counter":
                            initial_val = (
                                int(initial)
                                if initial is not None
                                else existing.get("initial")
                            )
                            if initial_val is not None:
                                update_msg["initial"] = initial_val
                            minimum_val = (
                                int(min_value)
                                if min_value is not None
                                else existing.get("minimum")
                            )
                            if minimum_val is not None:
                                update_msg["minimum"] = minimum_val
                            maximum_val = (
                                int(max_value)
                                if max_value is not None
                                else existing.get("maximum")
                            )
                            if maximum_val is not None:
                                update_msg["maximum"] = maximum_val
                            step_val = (
                                int(step) if step is not None else existing.get("step")
                            )
                            if step_val is not None:
                                update_msg["step"] = step_val
                            restore_val = (
                                restore
                                if restore is not None
                                else existing.get("restore")
                            )
                            if restore_val is not None:
                                update_msg["restore"] = restore_val

                        elif helper_type == "timer":
                            duration_val = (
                                duration
                                if duration is not None
                                else existing.get("duration")
                            )
                            if duration_val is not None:
                                update_msg["duration"] = duration_val
                            restore_val = (
                                restore
                                if restore is not None
                                else existing.get("restore")
                            )
                            if restore_val is not None:
                                update_msg["restore"] = restore_val

                        # input_button has no type-specific params beyond name/icon

                        result = await client.send_websocket_message(update_msg)
                        if not result.get("success"):
                            raise_tool_error(
                                create_error_response(
                                    ErrorCode.SERVICE_CALL_FAILED,
                                    f"Failed to update {helper_type} config: {result.get('error', 'Unknown error')}",
                                    context=_simple_helper_error_context(
                                        helper_type,
                                        entity_id=entity_id,
                                    ),
                                )
                            )
                        updated_data = result.get("result", {})

                    # Also update entity registry for icon, area, and labels
                    if icon is not None or area_id is not None or labels is not None:
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
                        reg_result = await client.send_websocket_message(
                            registry_update
                        )
                        if not reg_result.get("success"):
                            error_detail = reg_result.get("error", {})
                            error_msg = (
                                error_detail.get("message", "Unknown error")
                                if isinstance(error_detail, dict)
                                else str(error_detail)
                            )
                            logger.warning(
                                f"Entity registry update failed for {entity_id}: {error_msg}"
                            )
                            updated_data["warning"] = (
                                f"Config updated but entity registry update failed: {error_msg}"
                            )

                    # Apply category via shared helper
                    if category:
                        await apply_entity_category(
                            client,
                            entity_id,
                            category,
                            "helpers",
                            updated_data,
                            "helper",
                        )

                else:
                    # Fallback for unknown/future helper types: entity registry update only
                    update_msg = {
                        "type": "config/entity_registry/update",
                        "entity_id": entity_id,
                    }

                    if name is not None:
                        update_msg["name"] = name if name else None
                    if icon is not None:
                        update_msg["icon"] = icon if icon else None
                    if area_id is not None:
                        update_msg["area_id"] = area_id if area_id else None
                    if labels is not None:
                        update_msg["labels"] = labels

                    result = await client.send_websocket_message(update_msg)

                    if result.get("success"):
                        updated_data = result.get("result", {}).get("entity_entry", {})
                    else:
                        raise_tool_error(
                            create_error_response(
                                ErrorCode.SERVICE_CALL_FAILED,
                                f"Failed to update helper: {result.get('error', 'Unknown error')}",
                                context=_simple_helper_error_context(
                                    helper_type,
                                    entity_id=entity_id,
                                ),
                            )
                        )

                    # Apply category via shared helper
                    if category:
                        await apply_entity_category(
                            client,
                            entity_id,
                            category,
                            "helpers",
                            updated_data,
                            "helper",
                        )

                # Wait for entity to reflect the update
                wait_bool = coerce_bool_param(wait, "wait", default=True)
                response: dict[str, Any] = {
                    "success": True,
                    "action": "update",
                    "helper_type": helper_type,
                    "entity_id": entity_id,
                    "updated_data": updated_data,
                    "message": f"Successfully updated {helper_type}: {entity_id}",
                }
                if wait_bool:
                    try:
                        registered = await wait_for_entity_registered(client, entity_id)
                        if not registered:
                            response["warning"] = (
                                f"Update applied but {entity_id} not yet queryable."
                            )
                    except Exception as e:
                        response["warning"] = (
                            f"Update applied but verification failed: {e}"
                        )
                return response

            # This should never be reached since action is either "create" or "update"
            raise_tool_error(
                create_error_response(
                    ErrorCode.INTERNAL_ERROR,
                    f"Unexpected action: {action}",
                )
            )

        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={"action": action, "helper_type": helper_type},
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify helper_id exists for update operations",
                    "Ensure required parameters are provided for the helper type",
                ],
            )
