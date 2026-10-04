"""Registries of runtime-editable settings and their validation bounds."""

from typing import Literal, NamedTuple

__all__ = [
    "ADDON_SYNCED_ADVANCED_FIELDS",
    "ADVANCED_SETTINGS_FIELDS",
    "BACKUP_OVERRIDE_FIELDS",
    "BETA_FEATURE_FIELDS",
    "FEATURE_FLAG_FIELDS",
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

# Runtime-editable feature flags surfaced in the /settings web UI
# (issue #863). Each entry is (field_name, env_var_name, python_type).
# The web UI's /api/settings/features GET/POST endpoints iterate this
# tuple to advertise per-field origin (env / addon / file / default)
# and to validate incoming writes. Precedence: explicit env var beats
# the override file, addon mode (SUPERVISOR_TOKEN set) ignores the
# file entirely (start.py owns env vars from config.yaml in that
# mode), and the pydantic field default is the fallback.
# ===== Typed registry shapes =====
#
# NamedTuples preserve positional-unpack compatibility (existing
# ``for fname, env, ftype in FEATURE_FLAG_FIELDS:`` iteration sites keep
# working) AND add attribute access (``f.field`` / ``f.env`` / ``f.ftype``)
# for new call sites. Literal annotations on closed-set fields (section,
# python type) give mypy a chance to catch typos at definition time
# instead of letting them surface as silent runtime no-ops. The
# ``_validate_registries()`` call at module bottom enforces cross-table
# invariants at import time (field name exists on Settings, registries
# are name-disjoint, bounds-on-numeric / choices-on-str).

# Allowed python types for the override-apply machinery in
# ``_apply_*_overrides``. Anything outside this set silently falls into
# the ``else: continue`` arm of the type switch and the override is
# dropped — making the constraint explicit at the type level catches
# typos like ``ftype=Path`` at definition site.
RegistryFieldType = type[bool] | type[int] | type[float] | type[str]

# Closed set of UI section names. The advanced renderer in
# settings_ui/__init__.py picks a DOM container per section; a typo would render
# the row into nothing.
AdvancedSection = Literal[
    "connection",
    "search",
    "operations",
    "diagnostics",
    "tools_surface",
    "sidecar",
    "beta_codemode",
    "beta_yamlkeys",
    "developer",
]


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


FEATURE_FLAG_FIELDS: tuple[FeatureFlagField, ...] = (
    FeatureFlagField("enable_beta_features", "ENABLE_BETA_FEATURES", bool),
    FeatureFlagField("enable_tool_search", "ENABLE_TOOL_SEARCH", bool),
    FeatureFlagField("tool_search_max_results", "TOOL_SEARCH_MAX_RESULTS", int),
    FeatureFlagField(
        "enable_tool_security_policies", "ENABLE_TOOL_SECURITY_POLICIES", bool
    ),
    # Registers ha_manage_security_policy (#2148). Non-beta, like the master
    # policy flag above it, so it is deliberately NOT in BETA_FEATURE_FIELDS.
    # It carries no ``features.*`` locale keys either: that is what keeps it
    # out of the generated FEATURE_META and therefore out of the generic
    # Server Settings feature list, while /api/settings/features still serves
    # and persists it for the Policies-tab toggle.
    FeatureFlagField(
        "enable_security_policy_tool", "ENABLE_SECURITY_POLICY_TOOL", bool
    ),
    # Non-beta global safety toggle (discussion #1569). Lives here so the
    # Tools-tab toggle and the Server Settings row share the same
    # /api/settings/features plumbing, override-file persistence, and
    # addon Supervisor routing as every other feature flag.
    FeatureFlagField("read_only_mode", "READ_ONLY_MODE", bool),
    # Non-beta secret-redaction toggle (issue #2157). Same shared plumbing
    # as read_only_mode: web-UI row, override-file persistence, addon
    # Supervisor routing, stdio sidecar — all registry-driven.
    FeatureFlagField("redact_secrets", "REDACT_SECRETS", bool),
    # Non-beta, default-ON master switch for write-tool skill_content
    # delivery (#1182). Grouped with the non-beta flags above the beta
    # run below; intentionally NOT in BETA_FEATURE_FIELDS (it must not be
    # gated by the beta master) nor in ADVANCED_SETTINGS_FIELDS (registries
    # are name-disjoint per _validate_registries()).
    FeatureFlagField("enable_mandatory_bps", "ENABLE_MANDATORY_BPS", bool),
    # Child flag of enable_mandatory_bps (#1779). Non-beta like its
    # parent, so it belongs here and NOT in BETA_FEATURE_FIELDS; kept out
    # of ADVANCED_SETTINGS_FIELDS too (registries are name-disjoint).
    FeatureFlagField(
        "enable_strict_mandatory_bps", "ENABLE_STRICT_MANDATORY_BPS", bool
    ),
    FeatureFlagField("enable_yaml_config_editing", "ENABLE_YAML_CONFIG_EDITING", bool),
    FeatureFlagField("enable_yaml_edit_confirm", "ENABLE_YAML_EDIT_CONFIRM", bool),
    # Per-key sub-gates beneath enable_yaml_config_editing. Nested in
    # the UI, dimmed when the parent is off. Also listed in
    # BETA_FEATURE_FIELDS so they follow the same master-gate +
    # addon-mode override path as the other beta flags — that is what
    # makes the web-UI toggle take effect on the stable add-on (where
    # they are not in config.yaml). See that tuple for the rationale.
    FeatureFlagField(
        "enable_yaml_packages_automation",
        "ENABLE_YAML_PACKAGES_AUTOMATION",
        bool,
    ),
    FeatureFlagField(
        "enable_yaml_packages_script", "ENABLE_YAML_PACKAGES_SCRIPT", bool
    ),
    FeatureFlagField("enable_yaml_packages_scene", "ENABLE_YAML_PACKAGES_SCENE", bool),
    FeatureFlagField("enable_lite_docstrings", "ENABLE_LITE_DOCSTRINGS", bool),
    FeatureFlagField("enable_filesystem_tools", "HAMCP_ENABLE_FILESYSTEM_TOOLS", bool),
    # ``enable_code_mode`` lives in this tuple so the override file (and
    # the web UI Server Settings tab) can write the flag. Without this
    # entry, the UI save logic would have nowhere to land the value.
    FeatureFlagField("enable_code_mode", "ENABLE_CODE_MODE", bool),
    FeatureFlagField(
        "enable_dashboard_screenshot",
        "HAMCP_ENABLE_DASHBOARD_SCREENSHOT",
        bool,
    ),
)

# Override-file location is the same data dir that holds tool_config.json
# (resolved via ``utils.data_paths.get_data_dir`` — ``HA_MCP_CONFIG_DIR``,
# addon ``/data``, ``~/.ha-mcp``, or a tmpdir fallback).
# Imported lazily inside helpers to avoid a circular import at module
# load.
_FEATURE_FLAG_OVERRIDE_FILENAME = "feature_flags.json"

# Per-field validation bounds for non-bool fields. Only fields with
# range constraints need entries here; bools are handled by the
# coercion in ``_apply_feature_flag_overrides``. Mirrors the pydantic
# Field bounds on the same fields so a corrupt override file can't
# push values out of range.
_FEATURE_FLAG_INT_BOUNDS: dict[str, tuple[int, int]] = {
    "tool_search_max_results": (2, 10),
}

# Beta sub-flags gated by ``enable_beta_features``. Consumed
# by the master gate inside ``_apply_feature_flag_overrides``. Each name
# is also in ``FEATURE_FLAG_FIELDS`` so the UI's per-field origin / save
# logic stays unchanged — this tuple is consulted ONLY by the master
# gate, never by the per-field iteration.
BETA_FEATURE_FIELDS: tuple[str, ...] = (
    "enable_yaml_config_editing",
    "enable_yaml_edit_confirm",  # Default-ON safety sub-toggle; see field comment.
    # Per-key sub-gates of enable_yaml_config_editing. Included here so
    # they ride the same master gate + addon-mode override path as the
    # other beta flags. Without this, the addon-mode short-circuit in
    # ``_apply_feature_flag_overrides`` (and the ``get_feature_flag_origin``
    # logic) would leave them dead on the stable add-on — reachable only
    # via the dev add-on's config.yaml options. They still render NESTED
    # under their parent in the web UI (not as separate beta-sub rows).
    "enable_yaml_packages_automation",
    "enable_yaml_packages_script",
    "enable_yaml_packages_scene",
    "enable_filesystem_tools",
    "enable_code_mode",
    "enable_lite_docstrings",
    "enable_dashboard_screenshot",
)

# ===== Advanced settings panel registry =====
#
# Each entry: (field_name, env_var_name, python_type, section, editable).
#
# - ``section`` groups fields in the Advanced section of the Server Settings
#   tab: "connection", "search", "operations", "diagnostics", "tools_surface".
#   The beta sub-flags + the master live in a separate "beta" section that
#   the UI renders below the Advanced section (the per-key yaml-packages
#   sub-flags render nested under enable_yaml_config_editing within it).
# - ``editable=False`` marks display-only rows. Connection fields are
#   non-editable from the running server (chicken-and-egg footgun);
#   ``MCP_SERVER_VERSION`` is editable (it has an env alias) but the UI
#   warns that overriding it can confuse clients.
# - Fields that already appear in ``FEATURE_FLAG_FIELDS`` (e.g. tool search
#   toggles, beta flags) are intentionally NOT duplicated here — the UI
#   continues to source them via ``FEATURE_FLAG_FIELDS`` so the per-field
#   env-pin / addon-Supervisor routing logic stays unchanged for those rows.
ADVANCED_SETTINGS_FIELDS: tuple[AdvancedField, ...] = (
    # Connection — URL/token are display-only (chicken-and-egg: if you
    # could break the connection from the UI you couldn't use the same
    # UI to fix it). timeout / max_retries / verify_ssl are editable.
    AdvancedField("homeassistant_url", "HOMEASSISTANT_URL", str, "connection", False),
    AdvancedField(
        "homeassistant_token", "HOMEASSISTANT_TOKEN", str, "connection", False
    ),
    AdvancedField("timeout", "HA_TIMEOUT", int, "connection", True),
    AdvancedField("max_retries", "HA_MAX_RETRIES", int, "connection", True),
    # verify_ssl was in the (now-removed) connection section and was
    # always env-locked in addon mode. Moved to operations so it
    # renders in the panel, and added to ADDON_SYNCED_ADVANCED_FIELDS
    # below so saves in addon mode route through Supervisor — the same
    # sync behaviour the feature flags already get.
    AdvancedField("verify_ssl", "HA_VERIFY_SSL", bool, "operations", True),
    # Search & matching.
    AdvancedField("fuzzy_threshold", "FUZZY_THRESHOLD", int, "search", True),
    # Smart-search config-fetch time budgets (#1538). Restart-required
    # (consumed as import-time constants in smart_search/_config.py).
    AdvancedField(
        "automation_config_time_budget",
        "HAMCP_AUTOMATION_CONFIG_TIME_BUDGET",
        float,
        "search",
        True,
    ),
    AdvancedField(
        "script_config_time_budget",
        "HAMCP_SCRIPT_CONFIG_TIME_BUDGET",
        float,
        "search",
        True,
    ),
    AdvancedField(
        "scene_config_time_budget",
        "HAMCP_SCENE_CONFIG_TIME_BUDGET",
        float,
        "search",
        True,
    ),
    # Attempt-C per-request timeout + batch size (#1784). Restart-required
    # (same import-time consumption as the budgets above).
    AdvancedField(
        "individual_config_timeout",
        "HAMCP_INDIVIDUAL_CONFIG_TIMEOUT",
        float,
        "search",
        True,
    ),
    AdvancedField(
        "individual_fetch_batch_size",
        "HAMCP_INDIVIDUAL_FETCH_BATCH_SIZE",
        int,
        "search",
        True,
    ),
    # Operations.
    AdvancedField(
        "enable_history_query_guardrails",
        "HAMCP_ENABLE_HISTORY_QUERY_GUARDRAILS",
        bool,
        "operations",
        True,
    ),
    AdvancedField(
        "ha_tool_concurrency", "HA_TOOL_CONCURRENCY", int, "operations", True
    ),
    AdvancedField("backup_hint", "BACKUP_HINT", str, "operations", True),
    AdvancedField("enable_websocket", "ENABLE_WEBSOCKET", bool, "operations", True),
    # Dashboard-screenshot engine URL (#1538): docker/.env users could set
    # HAMCP_DASHBOARD_SCREENSHOT_ENGINE_URL, but add-on users had no path to
    # it. It is resolved live per capture (resolve_engine), so unlike the
    # time budgets it takes effect without a restart. Blank = auto-discover
    # the Puppet add-on via the Supervisor.
    AdvancedField(
        "dashboard_screenshot_engine_url",
        "HAMCP_DASHBOARD_SCREENSHOT_ENGINE_URL",
        str,
        "operations",
        True,
    ),
    AdvancedField(
        "enabled_tool_modules", "ENABLED_TOOL_MODULES", str, "tools_surface", True
    ),
    AdvancedField(
        "enable_dashboard_partial_tools",
        "ENABLE_DASHBOARD_PARTIAL_TOOLS",
        bool,
        "tools_surface",
        True,
    ),
    # Diagnostics.
    AdvancedField("mcp_server_name", "MCP_SERVER_NAME", str, "diagnostics", True),
    AdvancedField("mcp_server_version", "MCP_SERVER_VERSION", str, "diagnostics", True),
    AdvancedField("environment", "ENVIRONMENT", str, "diagnostics", True),
    AdvancedField("log_level", "LOG_LEVEL", str, "diagnostics", True),
    AdvancedField("debug", "DEBUG", bool, "diagnostics", True),
    AdvancedField(
        "http_transport_diagnostics",
        "HAMCP_HTTP_TRANSPORT_DIAGNOSTICS",
        bool,
        "diagnostics",
        True,
    ),
    AdvancedField(
        "http_json_response", "HAMCP_HTTP_JSON_RESPONSE", bool, "diagnostics", True
    ),
    # Settings UI sidecar (stdio-only). 0 (default) = first spawn picks a
    # free port and later spawns reuse it via ui.state (#2131); a value
    # pins a preferred fixed port instead (best-effort, #1587).
    AdvancedField("sidecar_pin_port", "HA_MCP_SIDECAR_PORT", int, "sidecar", True),
    # NOTE: ``auto_backup_dir`` and ``auto_backup_calendar_lookahead_days``
    # are NOT in this tuple. They are in ``BACKUP_OVERRIDE_FIELDS`` (defined
    # below) so they persist to ``backup_settings.json`` alongside the
    # other auto-backup settings.
    # Code-mode sub-numerics (only meaningful when enable_code_mode is on).
    # editable=True but the UI nests them under the beta section's
    # enable_code_mode row, dimmed and disabled when code mode is off.
    AdvancedField(
        "code_mode_max_duration", "CODE_MODE_MAX_DURATION", float, "beta_codemode", True
    ),
    AdvancedField(
        "code_mode_max_memory", "CODE_MODE_MAX_MEMORY", int, "beta_codemode", True
    ),
    AdvancedField(
        "code_mode_max_recursion", "CODE_MODE_MAX_RECURSION", int, "beta_codemode", True
    ),
    AdvancedField(
        "code_mode_max_invocations",
        "CODE_MODE_MAX_INVOCATIONS",
        int,
        "beta_codemode",
        True,
    ),
    AdvancedField(
        "code_mode_saved_tools_path",
        "CODE_MODE_SAVED_TOOLS_PATH",
        str,
        "beta_codemode",
        True,
    ),
    # Extra YAML write keys (issue #1887). Same shape as the code-mode
    # sub-settings above: a non-bool value that belongs visually under a
    # feature toggle rather than in the advanced panel, so it lives here
    # with its own section and the features renderer nests it beneath
    # "Enable YAML config editing".
    AdvancedField(
        "extra_yaml_write_keys",
        "HA_MCP_EXTRA_YAML_KEYS",
        str,
        "beta_yamlkeys",
        True,
    ),
    # Developer mode (issue #1775). Lives in ADVANCED_SETTINGS_FIELDS —
    # not FEATURE_FLAG_FIELDS — so it renders in its own "Developer"
    # section at the very bottom of the Server Settings tab instead of
    # among the feature toggles, and stays independent of the beta
    # master gate.
    AdvancedField("enable_dev_mode", "HAMCP_ENABLE_DEV_MODE", bool, "developer", True),
    # Dev-tools security-policy access (issue #2141). Renders directly
    # below the dev-mode toggle in the same Developer section. Editable
    # here and via the env var; the dev tools themselves refuse to write
    # it, so the AI cannot grant itself policy access.
    AdvancedField(
        "dev_tools_security_policy_access",
        "HAMCP_DEV_SECURITY_POLICY_ACCESS",
        bool,
        "developer",
        True,
    ),
)


# Per-field validation bounds for non-bool advanced fields.
# Bounds present on the Settings field today (mirrored):
#   fuzzy_threshold (validator 0-100), code_mode_* (Field ge/le).
# Bounds added purely as UI/POST guardrails (no Field constraint):
#   timeout, max_retries.
_ADVANCED_SETTINGS_BOUNDS: dict[str, tuple[float, float]] = {
    "timeout": (1, 600),
    "max_retries": (0, 20),
    "fuzzy_threshold": (0, 100),
    "automation_config_time_budget": (1.0, 600.0),
    "script_config_time_budget": (1.0, 600.0),
    "scene_config_time_budget": (1.0, 600.0),
    "individual_config_timeout": (1.0, 600.0),
    "individual_fetch_batch_size": (1, 100),
    "ha_tool_concurrency": (1, 32),
    "code_mode_max_duration": (1.0, 300.0),
    "code_mode_max_memory": (1_048_576, 268_435_456),
    "code_mode_max_recursion": (1, 10_000),
    "code_mode_max_invocations": (1, 10_000),
    # 0 is the "off" sentinel (ephemeral); the range below is the valid
    # PINNED range. See _ADVANCED_SETTINGS_SENTINELS.
    "sidecar_pin_port": (1024, 65535),
}


# Fields where a specific value is a valid "off" sentinel that bypasses the
# _ADVANCED_SETTINGS_BOUNDS range (sidecar_pin_port: 0 = ephemeral). The UI
# emits min=sentinel so the number input can still express "off"; the
# override-apply and UI-POST paths accept the sentinel OR the bounded range.
_ADVANCED_SETTINGS_SENTINELS: dict[str, int] = {
    "ha_tool_concurrency": 0,
    "sidecar_pin_port": 0,
}


# Allowed-values for enum-like string fields (renders as <select> in UI).
_ADVANCED_SETTINGS_CHOICES: dict[str, tuple[str, ...]] = {
    "backup_hint": ("strong", "normal", "weak", "auto"),
    "log_level": ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
    "environment": ("development", "production"),
}


# Advanced fields that also live in the HA add-on's `config.yaml`
# schema. In addon mode, their env vars are written by start.py from
# /data/options.json, so the override file would be
# ignored at next boot anyway — writes must route through Supervisor
# /addons/self/options instead. ``_origin_for_advanced_field`` returns
# ``'addon'`` for these in addon mode; ``_save_advanced_settings``
# batches addon-origin writes and POSTs them via Supervisor.
ADDON_SYNCED_ADVANCED_FIELDS: tuple[str, ...] = (
    "backup_hint",
    "ha_tool_concurrency",
    "verify_ssl",
)


# Backup runtime-editable fields (#1288 web UI editor). Each entry
# is (field_name, env_var_name, python_type). The web UI's
# /api/settings/backups/config GET/POST endpoints iterate this tuple to
# advertise per-field origin (env / addon / file / default) and to
# validate incoming writes. Keep aligned with the matching ``Settings``
# fields above — adding a runtime-editable setting means a new
# tuple entry plus matching addon ``config.yaml`` schema mirror.
BACKUP_OVERRIDE_FIELDS: tuple[BackupOverrideField, ...] = (
    BackupOverrideField("enable_auto_backup", "ENABLE_AUTO_BACKUP", bool),
    BackupOverrideField(
        "auto_backup_throttle_minutes", "AUTO_BACKUP_THROTTLE_MINUTES", int
    ),
    BackupOverrideField(
        "auto_backup_retain_per_entity", "AUTO_BACKUP_RETAIN_PER_ENTITY", int
    ),
    BackupOverrideField("auto_backup_dir", "HAMCP_BACKUP_DIR", str),
    BackupOverrideField(
        "auto_backup_calendar_lookahead_days",
        "HAMCP_AUTO_BACKUP_CALENDAR_LOOKAHEAD_DAYS",
        int,
    ),
    BackupOverrideField("enable_snapshot_actions", "ENABLE_SNAPSHOT_ACTIONS", bool),
    BackupOverrideField("backup_read_only", "BACKUP_READ_ONLY", bool),
    BackupOverrideField("enable_snapshot_delete", "ENABLE_SNAPSHOT_DELETE", bool),
    BackupOverrideField(
        "snapshot_delete_min_age_days", "SNAPSHOT_DELETE_MIN_AGE_DAYS", int
    ),
)

# Override-file location is the same data dir that holds tool_config.json
# (resolved via ``utils.data_paths.get_data_dir`` — ``HA_MCP_CONFIG_DIR``,
# addon ``/data``, ``~/.ha-mcp``, or a tmpdir fallback).
# Imported lazily inside helpers to avoid a circular import at module
# load (``utils.data_paths`` imports from ``_version`` which imports
# from ``config`` transitively in some test layouts).
_BACKUP_OVERRIDE_FILENAME = "backup_settings.json"
