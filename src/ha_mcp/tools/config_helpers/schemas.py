"""Helper type constants, per-type field schemas and the helper response shape."""

from contextvars import ContextVar
from typing import Any, Literal, TypedDict, get_args

from ..config_write_helpers import attach_skill_content

__all__ = [
    "SIMPLE_HELPER_SCHEMAS",
    "SIMPLE_HELPER_TYPES",
    "StorageHelperType",
    "_HELPER_SKILL_FILES",
    "_SIMPLE_CONFIG_KEYS_DESCRIPTION",
    "HelperResponse",
    "_attach_helper_skill",
    "_helper_response",
    "_simple_helper_error_context",
    "get_simple_helper_schema",
]

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
# (not Config Entry Flow). The helper tools' schemas spell them out as an enum.
# Any other value is a helper flow type, checked against Core at call time
# (``helper_flows``), except config_subentry and ha_config_list_helpers' "all".
StorageHelperType = Literal[
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
]
SIMPLE_HELPER_TYPES: frozenset[str] = frozenset(get_args(StorageHelperType))

# Stateful HA input helpers skip last-state restore when `initial` is stored in config
# (including false/0). See input_boolean/input_number async_added_to_hass in HA core.
_INITIAL_DISABLES_RESTORE_DESCRIPTION = (
    "When set — even to false/0 — disables last-state restore and forces this "
    "value on every HA restart. Omit unless you want the helper to reset to this "
    "value on every restart instead of restoring its last state."
)


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
                "Initial state. "
                f"{_INITIAL_DISABLES_RESTORE_DESCRIPTION} "
                "Accepts 'true'/'false'/'on'/'off'/'yes'/'no'/'1'/'0'."
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
            "description": (
                "Initial value — must be one of `options`. "
                f"{_INITIAL_DISABLES_RESTORE_DESCRIPTION}"
            ),
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
            "name": "min",
            "required": True,
            "selector": {"number": {}},
            "description": "Minimum value.",
        },
        {
            "name": "max",
            "required": True,
            "selector": {"number": {}},
            "description": "Maximum value.",
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
        {
            "name": "initial",
            "required": False,
            "selector": {"number": {}},
            "description": _INITIAL_DISABLES_RESTORE_DESCRIPTION,
        },
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
            "name": "min",
            "required": False,
            "selector": {"number": {}},
            "description": "Minimum length (0–255).",
        },
        {
            "name": "max",
            "required": False,
            "selector": {"number": {}},
            "description": "Maximum length (1–255).",
        },
        {
            "name": "mode",
            "required": False,
            "selector": {"select": {"options": ["text", "password"]}},
            "description": "Default 'text'.",
        },
        {"name": "unit_of_measurement", "required": False, "selector": {"text": {}}},
        {
            "name": "pattern",
            "required": False,
            "selector": {"text": {}},
            "description": "Regex the value must match.",
        },
        {
            "name": "initial",
            "required": False,
            "selector": {"text": {}},
            "description": _INITIAL_DISABLES_RESTORE_DESCRIPTION,
        },
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
            "description": (
                "Initial value (datetime string). "
                f"{_INITIAL_DISABLES_RESTORE_DESCRIPTION}"
            ),
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
        {
            "name": "initial",
            "required": False,
            "selector": {"number": {}},
            "description": (
                "Initial value. Counter restores its last value on restart by "
                "default; control via `restore`."
            ),
        },
        {
            "name": "minimum",
            "required": False,
            "selector": {"number": {}},
            "description": "Minimum value.",
        },
        {
            "name": "maximum",
            "required": False,
            "selector": {"number": {}},
            "description": "Maximum value.",
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
    ``fetch_helper_flow_info`` in ``config_entry_flow_introspect``).
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
    schema = _core_helper_fields(helper_type) or get_simple_helper_schema(helper_type)
    if schema is not None:
        context["data_schema"] = schema
    context.update(extra)
    return context


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


# Per-type `config` keys for SIMPLE helpers, published in the `config` description
# (replaced by Core's own field list when the component serves helper writes).
# Home Assistant validates these fields itself and names any mistake.
_SIMPLE_CONFIG_KEYS_DESCRIPTION = (
    "input_select: options (list, required), initial. "
    "input_number: min, max (required), step, unit_of_measurement, "
    "mode ('box'/'slider'), initial. "
    "input_text: min, max (length), mode ('text'/'password'), initial, "
    "unit_of_measurement, pattern (regex the value must match). "
    "input_datetime: has_date, has_time, initial. "
    "input_boolean: initial. "
    "counter: initial (starting value), minimum, maximum, step, "
    "restore (default true). "
    "timer: duration ('HH:MM:SS' or seconds), restore (default false). "
    "schedule: monday..sunday, each a list of {'from': 'HH:MM', 'to': 'HH:MM'} "
    "with optional 'data' dict of extra attributes. "
    "zone: latitude, longitude (both required), radius (meters, default 100), "
    "passive (won't trigger person state changes). "
    "person: user_id, device_trackers (device_tracker entity IDs), picture (URL). "
    "tag: tag_id (omit on create to auto-generate), description. "
    "On input_* types, `initial` — even false/0 — disables last-state restore "
    "and forces that value on every HA restart. "
    "ha_config_list_helpers(helper_type, describe=True) lists a type's fields."
)


# Core's simple-helper field lists and the call's action, fetched from the
# component once per ha_config_set_helper call; None falls back to
# SIMPLE_HELPER_SCHEMAS.
_CORE_HELPER_SCHEMAS: ContextVar[tuple[dict[str, Any], str] | None] = ContextVar(
    "_CORE_HELPER_SCHEMAS", default=None
)


def _core_helper_fields(helper_type: str) -> list[dict[str, Any]] | None:
    current = _CORE_HELPER_SCHEMAS.get()
    if current is None:
        return None
    schemas, action = current
    fields = (schemas.get(helper_type) or {}).get(action)
    return fields or None
