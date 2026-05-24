"""
Configuration management for Home Assistant MCP Server.
"""

import logging
import os

# Load environment variables from .env file with HAMCP_ENV_FILE support
# Use absolute path to ensure .env is found regardless of cwd
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ha_mcp._version import get_version

logger = logging.getLogger(__name__)

_PACKAGE_VERSION = get_version()

project_root = Path(__file__).parent.parent.parent

# Demo environment token - use HOMEASSISTANT_TOKEN="demo" to connect to the public demo
# Demo server: https://ha-mcp-demo-server.qc-h.net (login: mcp/mcp, resets weekly)
DEMO_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiIxOTE5ZTZlMTVkYjI0Mzk2YTQ4YjFiZTI1MDM1YmU2YSIsImlhdCI6MTc1NzI4OTc5NiwiZXhwIjoyMDcyNjQ5Nzk2fQ.Yp9SSAjm2gvl9Xcu96FFxS8SapHxWAVzaI0E3cD9xac"

# OAuth mode sentinel values — when these are present, HA credentials come from OAuth tokens
OAUTH_MODE_URL = "http://oauth-mode"
OAUTH_MODE_TOKEN = "oauth-mode-token"

# Support for different environment files via HAMCP_ENV_FILE
env_file = os.getenv("HAMCP_ENV_FILE", ".env")
env_path = project_root / env_file

# Load the specified environment file (silently, since env vars may come from other sources)
if env_path.exists():
    load_dotenv(env_path)
else:
    # Fallback to default .env
    default_env_path = project_root / ".env"
    if default_env_path.exists():
        load_dotenv(default_env_path)


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Home Assistant connection
    # In OAuth mode, these are optional and provided per-request
    homeassistant_url: str = Field(default=OAUTH_MODE_URL, alias="HOMEASSISTANT_URL")
    homeassistant_token: str = Field(
        default=OAUTH_MODE_TOKEN, alias="HOMEASSISTANT_TOKEN"
    )

    # Server configuration
    timeout: int = Field(30, alias="HA_TIMEOUT")
    max_retries: int = Field(3, alias="HA_MAX_RETRIES")

    # False = skip TLS verification (self-signed / hostname mismatch). Trusted networks only.
    verify_ssl: bool = Field(True, alias="HA_VERIFY_SSL")

    # Tool configuration
    fuzzy_threshold: int = Field(60, alias="FUZZY_THRESHOLD")
    entity_search_limit: int = Field(20, alias="ENTITY_SEARCH_LIMIT")

    # Backup tool configuration
    backup_hint: str = Field("normal", alias="BACKUP_HINT")

    # WebSocket configuration (essential for async operations)
    enable_websocket: bool = Field(True, alias="ENABLE_WEBSOCKET")

    # Development/Debug configuration
    debug: bool = Field(False, alias="DEBUG")
    log_level: str = Field("INFO", alias="LOG_LEVEL")

    # MCP Server configuration
    mcp_server_name: str = Field("ha-mcp", alias="MCP_SERVER_NAME")
    mcp_server_version: str = Field(
        default=_PACKAGE_VERSION, alias="MCP_SERVER_VERSION"
    )

    # Environment configuration
    environment: str = Field("development", alias="ENVIRONMENT")

    # Tool filtering - comma-separated list of module names to enable
    # Special values: "all" (default), "automation" (automation-related tools only)
    # Examples: "tools_config_automations,tools_config_scripts,tools_traces"
    enabled_tool_modules: str = Field("all", alias="ENABLED_TOOL_MODULES")

    # Dashboard partial update tools (python_transform, find_card)
    # These are token-efficient alternatives to full config replacement.
    # Disable when using clients with programmatic tool use (future).
    enable_dashboard_partial_tools: bool = Field(
        True, alias="ENABLE_DASHBOARD_PARTIAL_TOOLS"
    )

    # Tool search transform — replaces the full tool catalog with a unified
    # BM25 search tool and categorized call proxies (read/write/delete).
    # Dramatically reduces idle context token usage for LLMs.
    enable_tool_search: bool = Field(False, alias="ENABLE_TOOL_SEARCH")

    # Tool security policies middleware — opt-in gate that routes high-stakes
    # tool calls through a per-tool policy with out-of-band web-UI approval
    # (issue #966). Disabled by default.
    enable_tool_security_policies: bool = Field(
        False, alias="ENABLE_TOOL_SECURITY_POLICIES"
    )

    # Master beta-features toggle (#1164). UI-only — intentionally not in
    # any addon config.yaml schema. Consumed by the master gate in
    # ``_apply_feature_flag_overrides``, which force-sets the five
    # ``BETA_FEATURE_FIELDS`` sub-flags to False whenever this master is
    # off. Dev addon ``start.py`` auto-writes ``ENABLE_BETA_FEATURES=true``
    # whenever any beta sub-flag key is present in ``/data/options.json``
    # so the dev-addon UX is unchanged.
    enable_beta_features: bool = Field(False, alias="ENABLE_BETA_FEATURES")

    # Managed YAML config editing — allows ha_config_set_yaml to add,
    # replace, or remove top-level keys in configuration.yaml and package
    # files. Disabled by default; only for YAML-only features with no UI/API path.
    enable_yaml_config_editing: bool = Field(False, alias="ENABLE_YAML_CONFIG_EDITING")

    # Seed values for tool visibility (comma-separated tool names).
    # Used as initial config when no tool_config.json exists.
    # The web settings UI (/settings) is the primary interface for managing these.
    disabled_tools: str = Field("", alias="DISABLED_TOOLS")
    pinned_tools: str = Field("", alias="PINNED_TOOLS")

    # Max results returned by ha_search_tools. Pydantic enforces the
    # 2-10 range; the addon-dev schema also uses ``int(2,10)?`` so the
    # supervisor UI rejects out-of-range values before they reach env vars.
    tool_search_max_results: int = Field(
        5, ge=2, le=10, alias="TOOL_SEARCH_MAX_RESULTS"
    )

    # Lite docstrings — replace selected heavy tool descriptions with
    # shorter variants that defer detailed guidance to the
    # ``ha_get_skill_guide`` skill tool/resource.
    # Reduces idle catalog token usage at the cost of relying on the LLM
    # to actually consult the skill when it needs detail. Beta feature
    # (issue #1062); a startup WARNING is emitted when enabled so
    # env-var users see the trade-off in their logs.
    enable_lite_docstrings: bool = Field(False, alias="ENABLE_LITE_DOCSTRINGS")

    # Filesystem tools — read/write/delete/list under the HA config dir.
    # Previously gated by a direct ``os.getenv`` call in
    # ``tools/tools_filesystem.py`` so callers (and the settings UI)
    # couldn't see it through ``Settings``. Promoted to a first-class
    # Settings field so the same precedence path applies as for every
    # other gated capability.
    enable_filesystem_tools: bool = Field(False, alias="HAMCP_ENABLE_FILESYSTEM_TOOLS")

    # Custom-component installer (``ha_install_mcp_tools``) — pulls the
    # ``ha_mcp_tools`` integration into HACS. Same env-var-direct
    # background as ``enable_filesystem_tools``; promoted for the same
    # reason.
    enable_custom_component_integration: bool = Field(
        False, alias="HAMCP_ENABLE_CUSTOM_COMPONENT_INTEGRATION"
    )

    # Code Mode — sandboxed Python execution via pydantic-monty.
    # Provides an "escape hatch" tool (ha_manage_custom_tool) that lets LLMs write
    # custom one-off Python code when no existing tool covers the request.
    # Disabled by default due to the inherent risk of LLM-generated code.
    # Range bounds reject zero/negative values that would silently break the
    # tool and clamp upper bounds at sane safety margins (5 min wall-clock,
    # 256 MB memory, 10k recursion, 10k API/tool calls per execution).
    enable_code_mode: bool = Field(False, alias="ENABLE_CODE_MODE")
    code_mode_max_duration: float = Field(
        30.0, ge=1.0, le=300.0, alias="CODE_MODE_MAX_DURATION"
    )
    code_mode_max_memory: int = Field(
        10_485_760, ge=1_048_576, le=268_435_456, alias="CODE_MODE_MAX_MEMORY"
    )  # 10 MB default; 1 MB floor, 256 MB ceiling
    code_mode_max_recursion: int = Field(
        100, ge=1, le=10_000, alias="CODE_MODE_MAX_RECURSION"
    )
    code_mode_max_invocations: int = Field(
        100, ge=1, le=10_000, alias="CODE_MODE_MAX_INVOCATIONS"
    )
    # Path to a JSON file for persisting saved custom tools across restarts.
    # Empty string disables persistence (saved tools live in process memory
    # and are lost on restart). The addon sets this to /data/saved_tools.json
    # by default so saved tools survive addon restarts (the /data directory
    # is mapped per-addon by Supervisor and is preserved across addon
    # updates).
    code_mode_saved_tools_path: str = Field("", alias="CODE_MODE_SAVED_TOOLS_PATH")

    # Auto-backup of edited entities (#1288).
    # Captures the pre-write state of every wrapped write/destructive tool
    # to a local directory. Enabled by default — captures are best-effort
    # (failures log a WARNING but never block the wrapped write) and the
    # disk footprint is small (typically <10 KB per snapshot; default
    # retention is 100/entity, see ``auto_backup_retain_per_entity``).
    # Set ``ENABLE_AUTO_BACKUP=false`` to opt out.
    enable_auto_backup: bool = Field(True, alias="ENABLE_AUTO_BACKUP")

    # Per-entity throttle window. 0 (default) = backup every write; N>0 =
    # at most one snapshot per N minutes per entity. Upper bound 1440
    # (one day) prevents accidental indefinite throttling via typo.
    auto_backup_throttle_minutes: int = Field(
        0, ge=0, le=1440, alias="AUTO_BACKUP_THROTTLE_MINUTES"
    )

    # Max snapshots kept per entity. Older snapshots beyond this cap
    # are rotated out on each successful capture.
    auto_backup_retain_per_entity: int = Field(
        100, ge=1, le=10_000, alias="AUTO_BACKUP_RETAIN_PER_ENTITY"
    )

    # Backup directory override. Empty ("") resolves at runtime to a
    # deployment-mode default: ``/data/ha_mcp_backups`` in the add-on,
    # otherwise ``${XDG_DATA_HOME:-~/.local/share}/ha_mcp/backups``.
    auto_backup_dir: str = Field("", alias="HAMCP_BACKUP_DIR")

    # Calendar event backups query an ahead-of-now window to locate the
    # event by uid. Default 7 days catches typical edits; widen for
    # far-future events. Range 1-365 days.
    auto_backup_calendar_lookahead_days: int = Field(
        7, ge=1, le=365, alias="HAMCP_AUTO_BACKUP_CALENDAR_LOOKAHEAD_DAYS"
    )

    # Mirror the legacy ``os.getenv("FLAG", "").lower() in ("true", ...)``
    # semantics for the two ex-direct-getenv flags: an empty env var
    # value MUST be treated as False rather than raising
    # ``ValidationError``. Pydantic v2's bool parser raises on ``""``
    # which broke ``test_tools_filesystem.py::TestFeatureFlag::
    # test_disabled_with_empty_string`` after the migration; this
    # validator restores the contract callers rely on.
    @field_validator(
        "enable_filesystem_tools",
        "enable_custom_component_integration",
        mode="before",
    )
    @classmethod
    def _empty_string_means_false(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return False
        return v

    @property
    def env_file_name(self) -> str:
        """Get the current environment file name."""
        return os.getenv("HAMCP_ENV_FILE", ".env")

    @field_validator("homeassistant_url")
    @classmethod
    def validate_homeassistant_url(cls, v: str) -> str:
        """Ensure URL is properly formatted."""
        # Allow OAuth mode placeholder
        if v == OAUTH_MODE_URL:
            return v
        if not v.startswith(("http://", "https://")):
            raise ValueError("Home Assistant URL must start with http:// or https://")
        return v.rstrip("/")  # Remove trailing slash

    @field_validator("homeassistant_token")
    @classmethod
    def validate_homeassistant_token(cls, v: str) -> str:
        """Ensure token is not empty. Use 'demo' for public demo environment."""
        # Allow OAuth mode placeholder
        if v == OAUTH_MODE_TOKEN:
            return v
        if not v or v == "your_long_lived_access_token_here":
            raise ValueError("Home Assistant token must be provided")
        # Replace "demo" with actual demo token for easy onboarding
        if v.lower() == "demo":
            return DEMO_TOKEN
        return v

    @field_validator("fuzzy_threshold")
    @classmethod
    def validate_fuzzy_threshold(cls, v: int) -> int:
        """Ensure fuzzy threshold is reasonable."""
        if not 0 <= v <= 100:
            raise ValueError("Fuzzy threshold must be between 0 and 100")
        return v

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Ensure log level is valid."""
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        if v.upper() not in valid_levels:
            raise ValueError(f"Log level must be one of {valid_levels}")
        return v.upper()

    @field_validator("backup_hint")
    @classmethod
    def validate_backup_hint(cls, v: str) -> str:
        """Ensure backup hint is valid."""
        valid_hints = ["strong", "normal", "weak", "auto"]
        if v.lower() not in valid_hints:
            raise ValueError(f"Backup hint must be one of {valid_hints}")
        return v.lower()

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="allow"
    )


def get_settings() -> Settings:
    """Get application settings."""
    return Settings()  # type: ignore[call-arg]


def validate_settings() -> tuple[bool, str | None]:
    """
    Validate settings and return (is_valid, error_message).

    Returns:
        tuple: (True, None) if valid, (False, error_message) if invalid
    """
    try:
        settings = get_settings()

        # Additional validation
        if not settings.homeassistant_url:
            return False, "Home Assistant URL is required"

        if not settings.homeassistant_token:
            return False, "Home Assistant token is required"

        return True, None
    except Exception as e:
        return False, str(e)


# Runtime-editable feature flags surfaced in the /settings web UI
# (issue #863). Each entry is (field_name, env_var_name, python_type).
# The web UI's /api/settings/features GET/POST endpoints iterate this
# tuple to advertise per-field origin (env / addon / file / default)
# and to validate incoming writes. Precedence: explicit env var beats
# the override file, addon mode (SUPERVISOR_TOKEN set) ignores the
# file entirely (start.py owns env vars from config.yaml in that
# mode), and the pydantic field default is the fallback.
FEATURE_FLAG_FIELDS: tuple[tuple[str, str, type], ...] = (
    ("enable_beta_features", "ENABLE_BETA_FEATURES", bool),
    ("enable_tool_search", "ENABLE_TOOL_SEARCH", bool),
    ("tool_search_max_results", "TOOL_SEARCH_MAX_RESULTS", int),
    ("enable_tool_security_policies", "ENABLE_TOOL_SECURITY_POLICIES", bool),
    ("enable_yaml_config_editing", "ENABLE_YAML_CONFIG_EDITING", bool),
    ("enable_lite_docstrings", "ENABLE_LITE_DOCSTRINGS", bool),
    ("enable_filesystem_tools", "HAMCP_ENABLE_FILESYSTEM_TOOLS", bool),
    (
        "enable_custom_component_integration",
        "HAMCP_ENABLE_CUSTOM_COMPONENT_INTEGRATION",
        bool,
    ),
    # ``enable_code_mode`` was not in this tuple prior to #1164 — adding
    # it here is what makes the override file (and the new web UI Server
    # Settings tab) able to write the flag. Without this entry, the UI
    # save logic would have nowhere to land the value.
    ("enable_code_mode", "ENABLE_CODE_MODE", bool),
)

# Override-file location is the same data dir that holds tool_config.json
# (resolved via ``utils.data_paths.get_data_dir`` — addon ``/data``,
# ``HA_MCP_CONFIG_DIR``, ``XDG_DATA_HOME``, or a tmpdir fallback).
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

# Beta sub-flags gated by ``enable_beta_features`` (#1164). Consumed
# by the master gate inside ``_apply_feature_flag_overrides``. Each name
# is also in ``FEATURE_FLAG_FIELDS`` so the UI's per-field origin / save
# logic stays unchanged — this tuple is consulted ONLY by the master
# gate, never by the per-field iteration.
BETA_FEATURE_FIELDS: tuple[str, ...] = (
    "enable_yaml_config_editing",
    "enable_filesystem_tools",
    "enable_custom_component_integration",
    "enable_code_mode",
    "enable_lite_docstrings",
)

# ===== Advanced settings panel registry (#1164) =====
#
# Each entry: (field_name, env_var_name, python_type, section, editable).
#
# - ``section`` groups fields in the Advanced section of the Server Settings
#   tab: "connection", "search", "operations", "diagnostics", "tools_surface".
#   The 5 beta sub-flags + the master live in a separate "beta" section that
#   the UI renders below the Advanced section.
# - ``editable=False`` marks display-only rows. Connection fields are
#   non-editable from the running server (#1164 chicken-and-egg footgun);
#   ``MCP_SERVER_VERSION`` is editable (it has an env alias) but the UI
#   warns that overriding it can confuse clients.
# - Fields that already appear in ``FEATURE_FLAG_FIELDS`` (e.g. tool search
#   toggles, beta flags) are intentionally NOT duplicated here — the UI
#   continues to source them via ``FEATURE_FLAG_FIELDS`` so the per-field
#   env-pin / addon-Supervisor routing logic stays unchanged for those rows.
ADVANCED_SETTINGS_FIELDS: tuple[tuple[str, str, type, str, bool], ...] = (
    # Connection — URL/token are display-only (chicken-and-egg: if you
    # could break the connection from the UI you couldn't use the same
    # UI to fix it). timeout / max_retries / verify_ssl are editable.
    ("homeassistant_url", "HOMEASSISTANT_URL", str, "connection", False),
    ("homeassistant_token", "HOMEASSISTANT_TOKEN", str, "connection", False),
    ("timeout", "HA_TIMEOUT", int, "connection", True),
    ("max_retries", "HA_MAX_RETRIES", int, "connection", True),
    ("verify_ssl", "HA_VERIFY_SSL", bool, "connection", True),
    # Search & matching.
    ("fuzzy_threshold", "FUZZY_THRESHOLD", int, "search", True),
    ("entity_search_limit", "ENTITY_SEARCH_LIMIT", int, "search", True),
    # Operations.
    ("backup_hint", "BACKUP_HINT", str, "operations", True),
    ("enable_websocket", "ENABLE_WEBSOCKET", bool, "operations", True),
    ("enabled_tool_modules", "ENABLED_TOOL_MODULES", str, "tools_surface", True),
    (
        "enable_dashboard_partial_tools",
        "ENABLE_DASHBOARD_PARTIAL_TOOLS",
        bool,
        "tools_surface",
        True,
    ),
    # Diagnostics.
    ("mcp_server_name", "MCP_SERVER_NAME", str, "diagnostics", True),
    ("mcp_server_version", "MCP_SERVER_VERSION", str, "diagnostics", True),
    ("environment", "ENVIRONMENT", str, "diagnostics", True),
    ("log_level", "LOG_LEVEL", str, "diagnostics", True),
    ("debug", "DEBUG", bool, "diagnostics", True),
    # NOTE: ``auto_backup_dir`` and ``auto_backup_calendar_lookahead_days``
    # are NOT in this tuple. They are in ``BACKUP_OVERRIDE_FIELDS`` (defined
    # below) so they persist to ``backup_settings.json`` alongside the
    # other auto-backup settings.
    # Code-mode sub-numerics (only meaningful when enable_code_mode is on).
    # editable=True but the UI nests them under the beta section's
    # enable_code_mode row, dimmed and disabled when code mode is off.
    ("code_mode_max_duration", "CODE_MODE_MAX_DURATION", float, "beta_codemode", True),
    ("code_mode_max_memory", "CODE_MODE_MAX_MEMORY", int, "beta_codemode", True),
    ("code_mode_max_recursion", "CODE_MODE_MAX_RECURSION", int, "beta_codemode", True),
    (
        "code_mode_max_invocations",
        "CODE_MODE_MAX_INVOCATIONS",
        int,
        "beta_codemode",
        True,
    ),
    (
        "code_mode_saved_tools_path",
        "CODE_MODE_SAVED_TOOLS_PATH",
        str,
        "beta_codemode",
        True,
    ),
)


# Per-field validation bounds for non-bool advanced fields.
# Bounds present on the Settings field today (mirrored):
#   fuzzy_threshold (validator 0-100), code_mode_* (Field ge/le).
# Bounds added purely as UI/POST guardrails (no Field constraint):
#   timeout, max_retries, entity_search_limit.
_ADVANCED_SETTINGS_BOUNDS: dict[str, tuple[float, float]] = {
    "timeout": (1, 600),
    "max_retries": (0, 20),
    "fuzzy_threshold": (0, 100),
    "entity_search_limit": (1, 1000),
    "code_mode_max_duration": (1.0, 300.0),
    "code_mode_max_memory": (1_048_576, 268_435_456),
    "code_mode_max_recursion": (1, 10_000),
    "code_mode_max_invocations": (1, 10_000),
}


# Allowed-values for enum-like string fields (renders as <select> in UI).
_ADVANCED_SETTINGS_CHOICES: dict[str, tuple[str, ...]] = {
    "backup_hint": ("strong", "normal", "weak", "auto"),
    "log_level": ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
    "environment": ("development", "production"),
}


def get_feature_flag_origin(env_name: str) -> str:
    """Return where the live value for ``env_name`` is sourced from.

    Used by the web UI to label each feature-flag field with its
    source and decide whether the field is editable from the web UI:

    - ``"addon"``: running inside the HA add-on. ``start.py`` always
      writes these env vars from ``config.yaml`` on every addon
      start; the override file is ignored. Web UI edits are routed
      through Supervisor ``/addons/self/options`` so ``config.yaml``
      stays authoritative. (The current PR exposes flags read-only
      in addon mode; routing addon edits through Supervisor is
      tracked separately.)
    - ``"env"``: env var explicitly set in the process environment
      (includes values loaded from ``.env`` via ``load_dotenv`` at
      module import — those land in ``os.environ`` and are
      indistinguishable from ``docker -e`` / shell-set values,
      which is intentional). Web UI shows the field read-only;
      user must unset the env var to edit.
    - ``"file"``: standalone deployment with a value persisted in
      ``<data_dir>/feature_flags.json``. Web UI edits update the
      file in place.
    - ``"default"``: no env var and no override file entry; the
      pydantic field default applies. Web UI edits create the file.

    Addon-mode handling for the master and beta sub-flags (#1164):

    - ``ENABLE_BETA_FEATURES`` (master) is not in any addon schema,
      so ``"addon"`` is never returned for it — it follows env / file
      / default precedence regardless of mode.
    - The five ``BETA_FEATURE_FIELDS`` (sub-flags) exist in the dev
      addon schema but NOT the stable addon schema. In dev addon
      mode, ``start.py`` writes the env var from ``/data/options.json``
      so the env var is set at runtime; that signals "Supervisor
      authoritative" and ``"addon"`` is returned. In stable addon
      mode the env var is never written, so the sub-flag falls
      through to file/default.
    """
    field_name = next(
        (fname for fname, ename, _ in FEATURE_FLAG_FIELDS if ename == env_name),
        None,
    )
    is_master = field_name == "enable_beta_features"
    is_beta_sub = field_name in BETA_FEATURE_FIELDS
    in_addon = bool(os.environ.get("SUPERVISOR_TOKEN"))

    if in_addon:
        if is_master:
            # Master never has an addon schema entry — fall through to
            # file/default chain.
            pass
        elif is_beta_sub:
            # Dev addon: start.py wrote the env var → Supervisor is
            # the source of truth, mark as addon-editable. Stable
            # addon: env var never written → fall through to file
            # so the UI master toggle path applies.
            if os.environ.get(env_name) is not None:
                return "addon"
            # else: stable, fall through.
        else:
            return "addon"
    if os.environ.get(env_name) is not None:
        return "env"
    if field_name is None:
        return "default"
    overrides = _read_feature_flag_override_file()
    if field_name in overrides:
        return "file"
    return "default"


def _read_feature_flag_override_file() -> dict[str, object]:
    """Return the contents of the feature-flag override file, or ``{}``.

    Best-effort: a corrupt file MUST NOT break Settings loading. But
    the failure modes split into two categories that need different
    treatment:

    * **Silent**: file does not exist. The override layer is opt-in;
      a missing file is the normal "user has never edited" state and
      should not log.
    * **Loud (WARNING)**: file exists but is unreadable
      (``PermissionError``, broken filesystem) or unparseable
      (``JSONDecodeError``). The user toggled something, the UI said
      "Saved", and the value is silently being ignored. Without a log
      line they have no diagnostic; with one, the sidecar/server log
      tells them exactly what to fix.

    Data-dir resolution itself can raise (``RuntimeError`` when
    ``Path.home()`` cannot determine a home directory — typical of
    pytest's ``patch.dict(os.environ, {}, clear=True)``), so the
    ``get_data_dir()`` call is inside the try/except too. That branch
    is treated as silent: the user could not have created an override
    file in a directory we cannot resolve.
    """
    import json
    from pathlib import Path

    try:
        from .utils.data_paths import get_data_dir

        path: Path = get_data_dir() / _FEATURE_FLAG_OVERRIDE_FILENAME
    except (RuntimeError, OSError):
        # Couldn't resolve the data dir at all — user has no override
        # file by definition. Silent.
        return {}
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return {}
    except OSError:
        logger.warning(
            "Feature-flag override file at %s exists but is unreadable; "
            "falling back to defaults. Check filesystem permissions.",
            path,
            exc_info=True,
        )
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning(
            "Feature-flag override file at %s is not valid JSON; "
            "falling back to defaults. Delete or fix the file to "
            "re-enable persisted toggles.",
            path,
        )
        return {}
    if not isinstance(data, dict):
        logger.warning(
            "Feature-flag override file at %s is not a JSON object "
            "(got %s); falling back to defaults.",
            path,
            type(data).__name__,
        )
        return {}
    return data


def _apply_feature_flag_overrides(settings: "Settings") -> None:
    """Patch ``settings`` with override-file values + apply the master beta gate.

    Two behaviors interleave:

    1. **Per-field override-file application** (existing PR #1381 behavior):
       reads ``feature_flags.json`` and applies values for each
       FEATURE_FLAG_FIELDS entry, subject to: explicit env var wins over
       file; addon mode (SUPERVISOR_TOKEN set) normally short-circuits
       this branch because start.py owns env vars from config.yaml.

       EXCEPTION: the beta-master + beta-sub-flag fields skip the
       addon-mode short-circuit. They are not in any addon config.yaml
       schema (#1164), so the override file is the only authoritative
       source for them in either mode.

    2. **Beta master gate** (#1164): after the per-field pass, if
       ``enable_beta_features`` is False on the resolved Settings,
       force-set the five BETA_FEATURE_FIELDS to False regardless of
       how they currently look. This is the "master toggle" semantics —
       even a power user who sets ENABLE_YAML_CONFIG_EDITING=true via
       env var still needs to flip the master before the flag takes
       effect.
    """
    in_addon = bool(os.environ.get("SUPERVISOR_TOKEN"))
    overrides = _read_feature_flag_override_file()

    known = {fname: (ename, ftype) for fname, ename, ftype in FEATURE_FLAG_FIELDS}
    beta_fields = {"enable_beta_features", *BETA_FEATURE_FIELDS}

    for field_name, (env_name, ftype) in known.items():
        is_beta = field_name in beta_fields
        if in_addon and not is_beta:
            # Non-beta addon mode: start.py owns it. Skip.
            continue
        if os.environ.get(env_name) is not None:
            # Explicit env var wins over file for that field.
            continue
        if field_name not in overrides:
            continue
        raw = overrides[field_name]
        coerced: bool | int
        if ftype is bool:
            if not isinstance(raw, bool | int):
                logger.warning(
                    "Override for %r is %s; expected bool — ignoring.",
                    field_name,
                    type(raw).__name__,
                )
                continue
            coerced = bool(raw)
        elif ftype is int:
            if isinstance(raw, bool) or not isinstance(raw, int):
                logger.warning(
                    "Override for %r is %s; expected int — ignoring.",
                    field_name,
                    type(raw).__name__,
                )
                continue
            coerced = int(raw)
            bounds = _FEATURE_FLAG_INT_BOUNDS.get(field_name)
            if bounds is not None and not (bounds[0] <= coerced <= bounds[1]):
                logger.warning(
                    "Override for %r is %d, outside %d-%d — ignoring.",
                    field_name,
                    coerced,
                    bounds[0],
                    bounds[1],
                )
                continue
        else:
            continue
        if not hasattr(settings, field_name):
            logger.warning(
                "Override for %r (value=%r) targets a field that does "
                "not exist on Settings; ignoring. Likely a stale entry "
                "after a field was renamed/removed.",
                field_name,
                coerced,
            )
            continue
        try:
            setattr(settings, field_name, coerced)
        except (ValueError, TypeError) as err:
            logger.warning(
                "Override for %r (value=%r) rejected by Settings (%s); ignoring.",
                field_name,
                coerced,
                err,
            )

    # === Master beta gate ===
    if not getattr(settings, "enable_beta_features", False):
        for sub in BETA_FEATURE_FIELDS:
            if not hasattr(settings, sub):
                logger.warning(
                    "Beta gate: %s is not a Settings attribute; "
                    "BETA_FEATURE_FIELDS may have drifted from the "
                    "model. Skipping.",
                    sub,
                )
                continue
            current = getattr(settings, sub, False)
            if current:
                logger.info(
                    "Beta master toggle is off; forcing %s=False "
                    "(was True via env/file).",
                    sub,
                )
            try:
                setattr(settings, sub, False)
            except (ValueError, TypeError) as err:
                logger.warning(
                    "Could not force %s=False via master gate (%s); ignoring.",
                    sub,
                    err,
                )


def _apply_advanced_overrides(settings: "Settings") -> None:
    """Patch ``settings`` with advanced-section override values from
    ``feature_flags.json`` (#1164).

    Mirrors ``_apply_feature_flag_overrides`` but iterates
    ``ADVANCED_SETTINGS_FIELDS`` and supports float / str in addition
    to bool / int. Display-only fields (``editable=False`` in the
    registry) are NEVER applied — chicken-and-egg safeguard for
    connection settings (#1164).

    Addon-mode behavior: two advanced fields are in addon ``config.yaml``
    schemas — ``backup_hint`` and ``verify_ssl`` (both stable and dev).
    For those, ``start.py`` exports the env var on every boot and the
    env-var-wins check below correctly skips them. All other advanced
    fields (code_mode_* sub-numerics, mcp_server_*, log_level, debug,
    enabled_tool_modules, fuzzy_threshold, etc.) are NOT in any addon
    schema; the override file is the authoritative source and applies
    in either deployment mode.
    """
    overrides = _read_feature_flag_override_file()
    if not overrides:
        return
    for fname, env_name, ftype, _section, editable in ADVANCED_SETTINGS_FIELDS:
        if not editable:
            # Display-only field somehow landed in the override file (UI
            # POST guard at /api/settings/advanced blocks this, so the
            # only way in is direct hand-edit or upgrade-time drift).
            # Log so the operator can see why the value is being ignored.
            if fname in overrides:
                logger.warning(
                    "Override for %r is ignored: field is marked "
                    "display-only in ADVANCED_SETTINGS_FIELDS (set via "
                    "env var or addon configuration instead).",
                    fname,
                )
            continue
        if os.environ.get(env_name) is not None:
            continue
        if fname not in overrides:
            continue
        raw = overrides[fname]
        coerced: Any
        if ftype is bool:
            if not isinstance(raw, bool | int):
                logger.warning(
                    "Advanced override for %r is %s; expected bool — ignoring.",
                    fname,
                    type(raw).__name__,
                )
                continue
            coerced = bool(raw)
        elif ftype is int:
            if isinstance(raw, bool) or not isinstance(raw, int):
                logger.warning(
                    "Advanced override for %r is %s; expected int — ignoring.",
                    fname,
                    type(raw).__name__,
                )
                continue
            coerced = int(raw)
        elif ftype is float:
            if isinstance(raw, bool) or not isinstance(raw, int | float):
                logger.warning(
                    "Advanced override for %r is %s; expected float — ignoring.",
                    fname,
                    type(raw).__name__,
                )
                continue
            coerced = float(raw)
        elif ftype is str:
            if not isinstance(raw, str):
                logger.warning(
                    "Advanced override for %r is %s; expected str — ignoring.",
                    fname,
                    type(raw).__name__,
                )
                continue
            if "\x00" in raw:
                logger.warning(
                    "Advanced override for %r contains null byte; ignoring.",
                    fname,
                )
                continue
            coerced = raw
        else:
            continue

        bounds = _ADVANCED_SETTINGS_BOUNDS.get(fname)
        if bounds is not None and not (bounds[0] <= coerced <= bounds[1]):
            logger.warning(
                "Advanced override for %r is %s, outside %s-%s — ignoring.",
                fname,
                coerced,
                bounds[0],
                bounds[1],
            )
            continue
        choices = _ADVANCED_SETTINGS_CHOICES.get(fname)
        if choices is not None and coerced not in choices:
            logger.warning(
                "Advanced override for %r is %r, not in %s — ignoring.",
                fname,
                coerced,
                choices,
            )
            continue
        try:
            setattr(settings, fname, coerced)
        except Exception:
            logger.warning(
                "Advanced override for %r could not be applied; ignoring.",
                fname,
                exc_info=True,
            )


# Global settings instance
_settings: Settings | None = None


# Auto-backup runtime-editable fields (#1288 web UI editor). Each entry
# is (field_name, env_var_name, python_type). The web UI's
# /api/settings/backups/config GET/POST endpoints iterate this tuple to
# advertise per-field origin (env / addon / file / default) and to
# validate incoming writes. Keep aligned with the matching ``Settings``
# fields above — adding a fourth runtime-editable setting means a new
# tuple entry plus matching addon ``config.yaml`` schema mirror.
BACKUP_OVERRIDE_FIELDS: tuple[tuple[str, str, type], ...] = (
    ("enable_auto_backup", "ENABLE_AUTO_BACKUP", bool),
    ("auto_backup_throttle_minutes", "AUTO_BACKUP_THROTTLE_MINUTES", int),
    ("auto_backup_retain_per_entity", "AUTO_BACKUP_RETAIN_PER_ENTITY", int),
    ("auto_backup_dir", "HAMCP_BACKUP_DIR", str),
    (
        "auto_backup_calendar_lookahead_days",
        "HAMCP_AUTO_BACKUP_CALENDAR_LOOKAHEAD_DAYS",
        int,
    ),
)

# Override-file location is the same data dir that holds tool_config.json
# (resolved via ``utils.data_paths.get_data_dir`` — addon ``/data``,
# ``HA_MCP_CONFIG_DIR``, ``XDG_DATA_HOME``, or a tmpdir fallback).
# Imported lazily inside helpers to avoid a circular import at module
# load (``utils.data_paths`` imports from ``_version`` which imports
# from ``config`` transitively in some test layouts).
_BACKUP_OVERRIDE_FILENAME = "backup_settings.json"


def get_backup_setting_origin(env_name: str) -> str:
    """Return where the live value for ``env_name`` is sourced from.

    Used by the web UI to label each auto-backup field with its source
    and decide whether the field is editable from the web UI:

    - ``"addon"``: running inside the HA add-on. ``start.py`` always
      writes these env vars from ``config.yaml`` on every addon start;
      the override file is ignored. Web UI edits are routed through
      Supervisor ``/addons/self/options`` so ``config.yaml`` stays
      authoritative.
    - ``"env"``: env var explicitly set in the process environment
      (includes values loaded from ``.env`` via ``load_dotenv`` at
      module import — those land in ``os.environ`` and are
      indistinguishable from ``docker -e`` / shell-set values, which is
      intentional per the deployment design). Web UI shows the field
      read-only; user must unset / remove the env var to edit.
    - ``"file"``: standalone deployment with a value persisted in
      ``<data_dir>/backup_settings.json``. Web UI edits update the
      file in place.
    - ``"default"``: no env var and no override file entry; the
      pydantic field default applies. Web UI edits create the file.
    """
    if os.environ.get("SUPERVISOR_TOKEN"):
        return "addon"
    if os.environ.get(env_name) is not None:
        return "env"
    field_name = next(
        (fname for fname, ename, _ in BACKUP_OVERRIDE_FIELDS if ename == env_name),
        None,
    )
    if field_name is None:
        return "default"
    overrides = _read_backup_override_file()
    if field_name in overrides:
        return "file"
    return "default"


def _read_backup_override_file() -> dict[str, object]:
    """Return the contents of the auto-backup override file, or ``{}``.

    Best-effort: a corrupt file MUST NOT break Settings loading. The
    failure modes split into two categories that need different
    treatment (mirrors ``_read_feature_flag_override_file``):

    * **Silent**: file does not exist. The override layer is opt-in;
      a missing file is the normal "user has never edited" state and
      should not log.
    * **Loud (WARNING)**: file exists but is unreadable
      (``PermissionError``, broken filesystem) or unparseable
      (``JSONDecodeError``). The user toggled something, the UI said
      "Saved", and the value is silently being ignored. Without a
      log line they have no diagnostic; with one, the sidecar/server
      log tells them exactly what to fix.

    Reads are not cached — callers (Settings construction, the GET
    endpoint) hit disk each time, which is fine for a small JSON file
    behind a singleton-cached Settings.
    """
    import json
    from pathlib import Path

    try:
        from .utils.data_paths import get_data_dir

        path: Path = get_data_dir() / _BACKUP_OVERRIDE_FILENAME
    except (RuntimeError, OSError):
        # Couldn't resolve the data dir at all — user has no override
        # file by definition. Silent.
        return {}
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return {}
    except OSError:
        logger.warning(
            "Auto-backup override file at %s exists but is unreadable; "
            "falling back to defaults. Check filesystem permissions.",
            path,
            exc_info=True,
        )
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning(
            "Auto-backup override file at %s is not valid JSON; "
            "falling back to defaults. Delete or fix the file to "
            "re-enable persisted toggles.",
            path,
        )
        return {}
    if not isinstance(data, dict):
        logger.warning(
            "Auto-backup override file at %s is not a JSON object "
            "(got %s); falling back to defaults.",
            path,
            type(data).__name__,
        )
        return {}
    return data


def _apply_backup_overrides(settings: "Settings") -> None:
    """Patch ``settings`` with values from the override file, in place.

    Honors the "env var wins" contract: a field whose env var is set in
    the process environment is never overwritten. Addon mode short-
    circuits — ``start.py`` already wrote these env vars from
    ``config.yaml`` and the override file is ignored in that mode.
    Range / type clamping mirrors the pydantic Field bounds so a
    corrupt override file can't push values out of range; out-of-range
    or untypable entries are silently skipped.
    """
    if os.environ.get("SUPERVISOR_TOKEN"):
        return
    overrides = _read_backup_override_file()
    if not overrides:
        return
    for field_name, env_name, ftype in BACKUP_OVERRIDE_FIELDS:
        if os.environ.get(env_name) is not None:
            continue
        if field_name not in overrides:
            continue
        raw = overrides[field_name]
        coerced: bool | int | str
        if ftype is bool:
            if not isinstance(raw, bool | int):
                logger.warning(
                    "backup_settings.json: %s expects bool, got %s; ignoring",
                    field_name,
                    type(raw).__name__,
                )
                continue
            coerced = bool(raw)
        elif ftype is int:
            if isinstance(raw, bool) or not isinstance(raw, int):
                logger.warning(
                    "backup_settings.json: %s expects int, got %s; ignoring",
                    field_name,
                    type(raw).__name__,
                )
                continue
            try:
                coerced = int(raw)
            except (ValueError, TypeError):
                logger.warning(
                    "backup_settings.json: %s value %r is not coercible to int; ignoring",
                    field_name,
                    raw,
                )
                continue
            if (
                field_name == "auto_backup_throttle_minutes"
                and not 0 <= coerced <= 1440
            ):
                logger.warning(
                    "backup_settings.json: auto_backup_throttle_minutes=%d out of "
                    "range 0..1440; ignoring",
                    coerced,
                )
                continue
            if (
                field_name == "auto_backup_retain_per_entity"
                and not 1 <= coerced <= 10_000
            ):
                logger.warning(
                    "backup_settings.json: auto_backup_retain_per_entity=%d out of "
                    "range 1..10000; ignoring",
                    coerced,
                )
                continue
            if (
                field_name == "auto_backup_calendar_lookahead_days"
                and not 1 <= coerced <= 365
            ):
                logger.warning(
                    "backup_settings.json: auto_backup_calendar_lookahead_days=%d out of "
                    "range 1..365; ignoring",
                    coerced,
                )
                continue
        elif ftype is str:
            if not isinstance(raw, str):
                logger.warning(
                    "backup_settings.json: %s expects str, got %s; ignoring",
                    field_name,
                    type(raw).__name__,
                )
                continue
            if "\x00" in raw:
                logger.warning(
                    "backup_settings.json: %s contains null byte; ignoring",
                    field_name,
                )
                continue
            coerced = raw
        else:
            continue
        try:
            setattr(settings, field_name, coerced)
        except (ValueError, TypeError) as err:
            logger.warning(
                "backup_settings.json: setattr(%s, %r) rejected by Settings (%s); "
                "ignoring",
                field_name,
                coerced,
                err,
            )
            continue


def get_global_settings() -> Settings:
    """Get global settings instance (singleton pattern).

    Applies override files at first read so web-UI edits take effect
    on the next ``get_global_settings()`` call after
    ``_reset_global_settings()`` is called by the POST handler:

    - Feature flags persisted to ``<data_dir>/feature_flags.json``
    - Auto-backup settings persisted to ``<data_dir>/backup_settings.json``
    """
    global _settings
    if _settings is None:
        _settings = get_settings()
        _apply_feature_flag_overrides(_settings)
        _apply_backup_overrides(_settings)
        _apply_advanced_overrides(_settings)
    return _settings


def _reset_global_settings() -> None:
    """Drop the cached settings singleton.

    Test seam so suites that mutate env vars can force a re-read
    without reaching into module-private state. Also used by the
    feature-flag and auto-backup settings POST handlers to publish a
    freshly edited override file value to runtime consumers
    (``get_global_settings`` is the only documented read path; the
    ``@with_auto_backup`` decorator reads it per tool call).
    """
    global _settings
    _settings = None
