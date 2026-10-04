"""Registries of runtime-editable settings and their validation bounds.

Every table is derived from the ``Setting`` metadata on the ``Settings``
fields; add or change a setting there, not here.
"""

from typing import NamedTuple

from pydantic.fields import FieldInfo

from ha_mcp.config_meta import (
    BETA_MASTER,
    AdvancedSection,
    Setting,
    SettingSurface,
    setting_of,
)
from ha_mcp.config_settings import Settings

__all__ = [
    "ADDON_SYNCED_ADVANCED_FIELDS",
    "ADVANCED_RESTART_REQUIRED",
    "ADVANCED_SETTINGS_FIELDS",
    "BACKUP_OVERRIDE_FIELDS",
    "BETA_FEATURE_FIELDS",
    "FEATURE_FLAG_FIELDS",
    "SETTING_BOUNDS",
    "_ADVANCED_SETTINGS_BOUNDS",
    "_ADVANCED_SETTINGS_CHOICES",
    "_ADVANCED_SETTINGS_SENTINELS",
    "_BACKUP_OVERRIDE_FILENAME",
    "_FEATURE_FLAG_INT_BOUNDS",
    "_FEATURE_FLAG_OVERRIDE_FILENAME",
    "AdvancedField",
    "AdvancedSection",
    "BackupOverrideField",
    "FeatureFlagField",
    "OverrideField",
    "RegistryFieldType",
]

# ===== Typed registry shapes =====
#
# NamedTuples preserve positional-unpack compatibility (existing
# ``for fname, env, ftype in FEATURE_FLAG_FIELDS:`` iteration sites keep
# working) AND add attribute access (``f.field`` / ``f.env`` / ``f.ftype``)
# for new call sites.

# Allowed python types for the override-apply machinery in
# ``_apply_*_overrides``. Anything outside this set silently falls into
# the ``else: continue`` arm of the type switch and the override is
# dropped, so ``_ftype`` below rejects any other field type at import.
RegistryFieldType = type[bool] | type[int] | type[float] | type[str]


class OverrideField(NamedTuple):
    """One row of an override-style registry (feature flags + backup
    settings + any future ``(field, env, ftype)`` registry).

    NOTE: adding a field here is a BREAKING change for every positional
    unpack site (e.g. ``for f, e, t in FEATURE_FLAG_FIELDS:``). Prefer
    a new NamedTuple over extending this one if a registry needs
    additional metadata.
    """

    field: str
    env: str
    ftype: RegistryFieldType


# Aliases preserve the readable names callers use at construction sites
# (``FeatureFlagField(...)`` reads more clearly than ``OverrideField(...)``
# inside FEATURE_FLAG_FIELDS) while ensuring the two registries can never
# drift apart at the type level.
FeatureFlagField = OverrideField
BackupOverrideField = OverrideField


class AdvancedField(NamedTuple):
    """One row of ADVANCED_SETTINGS_FIELDS.

    NOTE: adding a field here is a BREAKING change for every positional
    unpack site (e.g. ``for f, e, t, s, ed in ADVANCED_SETTINGS_FIELDS:``).
    """

    field: str
    env: str
    ftype: RegistryFieldType
    section: AdvancedSection
    editable: bool


def _check_setting(name: str, field: FieldInfo, setting: Setting) -> None:
    """Raise when a field's ``Setting`` metadata cannot mean what it says.

    Each of these would otherwise be a silent no-op at runtime: a range
    that the type switch in the apply loops never reads, a UI row rendered
    into no section, or a master gate writing to a field it cannot gate.
    """
    numeric = field.annotation in (int, float)
    problems = [
        (setting.range is not None and not numeric, "a range on a non-numeric field"),
        (
            setting.choices is not None and field.annotation is not str,
            "choices on a non-str field",
        ),
        (
            setting.off_value is not None and setting.range is None,
            "an off value without a range",
        ),
        (
            setting.lenient and setting.range is None and setting.choices is None,
            "lenient parsing with nothing to check",
        ),
        (
            (setting.surface == "advanced") != (setting.section is not None),
            "a section on a non-advanced setting, or an advanced setting without one",
        ),
        (
            setting.beta
            and (setting.surface != "feature" or field.annotation is not bool),
            "a beta flag that is not a bool feature flag",
        ),
        (field.alias is None, "no env var alias"),
        (
            setting.surface is not None
            and field.annotation not in (bool, int, float, str),
            "a web UI surface on a type the override files cannot store",
        ),
        (
            setting.app is not None and field.annotation not in (bool, int, str),
            "an app option of a type start.py cannot check",
        ),
        (
            setting.app is not None
            and setting.app.invalid_value is not None
            and field.annotation is not bool,
            "an invalid-value fallback on a non-bool app option",
        ),
        # start.py treats the dev-only app options, apart from the master,
        # as the beta sub-flags the master gates.
        (
            setting.beta and (setting.app is None or setting.app.flavors != ("dev",)),
            "a beta flag that is not a dev-only app option",
        ),
        (
            setting.app is not None
            and setting.app.flavors == ("dev",)
            and not setting.beta
            and name != BETA_MASTER,
            "a dev-only app option that is not a beta flag",
        ),
    ]
    for failed, problem in problems:
        if failed:
            raise RuntimeError(f"Settings.{name} has {problem}")


def _settings_of(surface: SettingSurface) -> list[tuple[str, FieldInfo, Setting]]:
    """Return the ``Settings`` fields of one web UI surface, in field order."""
    fields = []
    for name, field in Settings.model_fields.items():
        setting = setting_of(field)
        if setting is not None and setting.surface == surface:
            fields.append((name, field, setting))
    return fields


for _name, _field in Settings.model_fields.items():
    if (_setting := setting_of(_field)) is not None:
        _check_setting(_name, _field, _setting)


def _env(field: FieldInfo) -> str:
    assert field.alias is not None
    return field.alias


def _ftype(field: FieldInfo) -> RegistryFieldType:
    assert field.annotation in (bool, int, float, str)
    return field.annotation


FEATURE_FLAG_FIELDS: tuple[FeatureFlagField, ...] = tuple(
    FeatureFlagField(name, _env(field), _ftype(field))
    for name, field, _setting in _settings_of("feature")
)

# Beta sub-flags gated by ``enable_beta_features``. Consumed by the master
# gate inside ``_apply_feature_flag_overrides``; the per-field UI and save
# logic iterates ``FEATURE_FLAG_FIELDS`` as for any other flag.
BETA_FEATURE_FIELDS: tuple[str, ...] = tuple(
    name for name, _field, setting in _settings_of("feature") if setting.beta
)

# Override-file location is the same data dir that holds tool_config.json
# (resolved via ``utils.data_paths.get_data_dir`` — ``HA_MCP_CONFIG_DIR``,
# addon ``/data``, ``~/.ha-mcp``, or a tmpdir fallback).
# Imported lazily inside helpers to avoid a circular import at module
# load.
_FEATURE_FLAG_OVERRIDE_FILENAME = "feature_flags.json"

# Fields already in ``FEATURE_FLAG_FIELDS`` are not advanced settings: the
# UI sources them from there, so the per-field env-pin / addon-Supervisor
# routing stays the same for those rows.
ADVANCED_SETTINGS_FIELDS: tuple[AdvancedField, ...] = tuple(
    AdvancedField(name, _env(field), _ftype(field), setting.section, setting.editable)
    for name, field, setting in _settings_of("advanced")
    if setting.section is not None  # always, by _check_setting; narrows the type
)

BACKUP_OVERRIDE_FIELDS: tuple[BackupOverrideField, ...] = tuple(
    BackupOverrideField(name, _env(field), _ftype(field))
    for name, field, _setting in _settings_of("backup")
)

# The inclusive range of every setting that has one, for the override-file,
# web UI and dev-tool write paths.
SETTING_BOUNDS: dict[str, tuple[float, float]] = {
    name: setting.range
    for name, field in Settings.model_fields.items()
    if (setting := setting_of(field)) is not None and setting.range is not None
}

_FEATURE_FLAG_INT_BOUNDS: dict[str, tuple[float, float]] = {
    f.field: SETTING_BOUNDS[f.field]
    for f in FEATURE_FLAG_FIELDS
    if f.field in SETTING_BOUNDS
}
_ADVANCED_SETTINGS_BOUNDS: dict[str, tuple[float, float]] = {
    f.field: SETTING_BOUNDS[f.field]
    for f in ADVANCED_SETTINGS_FIELDS
    if f.field in SETTING_BOUNDS
}

# Values outside the range that mean "off" (sidecar_pin_port: 0 = ephemeral).
# The UI emits min=off value so the number input can still express it; the
# override-apply and UI-POST paths accept the off value OR the range.
_ADVANCED_SETTINGS_SENTINELS: dict[str, int] = {
    name: setting.off_value
    for name, _field, setting in _settings_of("advanced")
    if setting.off_value is not None
}

# Allowed values for enum-like string fields (renders as <select> in UI).
_ADVANCED_SETTINGS_CHOICES: dict[str, tuple[str, ...]] = {
    name: setting.choices
    for name, _field, setting in _settings_of("advanced")
    if setting.choices is not None
}

# Advanced fields that take effect only after a restart of the MCP server.
ADVANCED_RESTART_REQUIRED: frozenset[str] = frozenset(
    name
    for name, _field, setting in _settings_of("advanced")
    if setting.restart_required
)

# Advanced fields that are also app options. In addon mode, start.py writes
# their env vars from /data/options.json, so the override file would be
# ignored at next boot anyway — writes must route through Supervisor
# /addons/self/options instead. ``_origin_for_advanced_field`` returns
# ``'addon'`` for these in addon mode; ``_save_advanced_settings``
# batches addon-origin writes and POSTs them via Supervisor.
ADDON_SYNCED_ADVANCED_FIELDS: tuple[str, ...] = tuple(
    name for name, _field, setting in _settings_of("advanced") if setting.app
)

# Override-file location is the same data dir that holds tool_config.json
# (resolved via ``utils.data_paths.get_data_dir`` — ``HA_MCP_CONFIG_DIR``,
# addon ``/data``, ``~/.ha-mcp``, or a tmpdir fallback).
# Imported lazily inside helpers to avoid a circular import at module
# load (``utils.data_paths`` imports from ``_version`` which imports
# from ``config`` transitively in some test layouts).
_BACKUP_OVERRIDE_FILENAME = "backup_settings.json"
