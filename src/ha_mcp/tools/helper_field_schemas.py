"""Static per-type field data for ha_config_set_helper's SIMPLE helpers.

The typed-parameter allowlists, the `config` key description, and the field
schemas served as ``data_schema`` when the component can't supply Core's own.
"""

from typing import Any, TypedDict

# Stateful HA input helpers skip last-state restore when `initial` is stored in config
# (including false/0). See input_boolean/input_number async_added_to_hass in HA core.
_INITIAL_DISABLES_RESTORE_DESCRIPTION = (
    "When set — even to false/0 — disables last-state restore and forces this "
    "value on every HA restart. Omit unless you want the helper to reset to this "
    "value on every restart instead of restoring its last state."
)
# Per-type `config` keys for SIMPLE helpers, published in the `config` description.
_SIMPLE_CONFIG_KEYS_DESCRIPTION = (
    "input_select: options (list, required), initial. "
    "input_number: min_value, max_value, step, unit_of_measurement, "
    "mode ('box'/'slider'), initial. "
    "input_text: min_value, max_value (length), mode ('text'/'password'), initial. "
    "input_datetime: has_date, has_time, initial. "
    "input_boolean: initial. "
    "counter: initial (starting value), min_value, max_value, step, "
    "restore (default true). "
    "timer: duration ('HH:MM:SS' or seconds), restore (default false). "
    "schedule: monday..sunday, each a list of {'from': 'HH:MM', 'to': 'HH:MM'} "
    "with optional 'data' dict of extra attributes. "
    "zone: latitude, longitude (both required), radius (meters, default 100), "
    "passive (won't trigger person state changes). "
    "person: user_id, device_trackers (device_tracker entity IDs), picture (URL). "
    "tag: tag_id (omit on create to auto-generate), description. "
    "On input_* types, `initial` — even false/0 — disables last-state restore "
    "and forces that value on every HA restart."
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
