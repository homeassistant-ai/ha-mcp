"""
Configuration management for Home Assistant MCP Server.
"""

import logging
import os

# Load environment variables from .env file with HAMCP_ENV_FILE support
# Use absolute path to ensure .env is found regardless of cwd
from pathlib import Path
from typing import Any, Literal, NamedTuple

from dotenv import load_dotenv
from pydantic import Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ha_mcp._version import get_version, is_running_in_addon

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

    # Smart-search config-fetch time budgets (seconds). Bound how long
    # ha_search / ha_deep_search spends fetching automation/script/scene
    # definitions during the per-id fallback before reporting a partial
    # result. Surfaced in the Advanced settings panel (issue #1538) so
    # add-on users — who cannot set raw env vars — can tune them. Consumed
    # as import-time module constants in tools/smart_search/_config.py, so
    # a change requires an MCP-host restart to take effect (advanced
    # settings already carry a restart-required notice in the UI).
    automation_config_time_budget: float = Field(
        30.0, alias="HAMCP_AUTOMATION_CONFIG_TIME_BUDGET"
    )
    script_config_time_budget: float = Field(
        20.0, alias="HAMCP_SCRIPT_CONFIG_TIME_BUDGET"
    )
    scene_config_time_budget: float = Field(
        20.0, alias="HAMCP_SCENE_CONFIG_TIME_BUDGET"
    )

    # Per-request timeout and concurrency of the smart-search per-id
    # config-fetch fallback (Attempt C). On HA servers that serve
    # /config/<domain>/config/<id> serially, a full batch of concurrent
    # requests queues behind one another and the tail of each batch can
    # exceed the per-request timeout even though every request would
    # succeed — lowering the batch size (toward 1) and/or raising the
    # timeout lets such instances scan exhaustively (issue #1784). Same
    # consumption model as the budgets above: import-time constants in
    # tools/smart_search/_config.py, restart required.
    individual_config_timeout: float = Field(
        5.0, alias="HAMCP_INDIVIDUAL_CONFIG_TIMEOUT"
    )
    individual_fetch_batch_size: int = Field(
        10, alias="HAMCP_INDIVIDUAL_FETCH_BATCH_SIZE"
    )

    # Backup tool configuration
    backup_hint: str = Field("normal", alias="BACKUP_HINT")

    # WebSocket configuration (essential for async operations)
    enable_websocket: bool = Field(True, alias="ENABLE_WEBSOCKET")

    # Settings UI sidecar (stdio mode only, #1587). 0 = pick a free
    # ephemeral port at every spawn (default); 1024-65535 pins the sidecar
    # to a fixed port so the settings URL/origin stays stable across
    # restarts (bookmarks, browser localStorage). Read by run_main() in
    # stdio_settings_sidecar.py.
    sidecar_pin_port: int = Field(0, alias="HA_MCP_SIDECAR_PORT")

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

    # Read Only Mode — global safety toggle (discussion #1569). When on,
    # write-capable tools are hidden from the MCP catalog and every write
    # operation is blocked at call time with a structured READ_ONLY_MODE
    # error. Mixed read/write tools whose read surface has no pure-read
    # duplicate stay available with their write actions blocked (see
    # read_only.py:READ_ONLY_EXEMPT_TOOLS). Off by default.
    read_only_mode: bool = Field(False, alias="READ_ONLY_MODE")

    # Master beta-features toggle. UI-only — intentionally not in any
    # addon config.yaml schema. Consumed by the master gate in
    # ``_apply_feature_flag_overrides``, which force-sets the
    # ``BETA_FEATURE_FIELDS`` sub-flags to False whenever this master is
    # off. Dev addon ``start.py`` auto-writes ``ENABLE_BETA_FEATURES=true``
    # whenever any beta sub-flag key is present in ``/data/options.json``
    # so the dev-addon UX is unchanged.
    enable_beta_features: bool = Field(False, alias="ENABLE_BETA_FEATURES")

    # Managed YAML config editing — allows ha_config_set_yaml to add,
    # replace, or remove top-level keys in configuration.yaml and package
    # files. Disabled by default; only for YAML-only features with no UI/API path.
    enable_yaml_config_editing: bool = Field(False, alias="ENABLE_YAML_CONFIG_EDITING")

    # Two-step confirmation for ha_config_set_yaml (#1720). When on (the
    # default), the first edit call returns a unified diff preview plus a
    # confirm token and writes NOTHING; the edit lands only when repeated
    # with that token. Sub-toggle of enable_yaml_config_editing (nested
    # beneath it in the UI). Default ON deliberately: the diff preview is
    # what lets the calling agent catch collateral changes before they
    # reach disk. Listed in BETA_FEATURE_FIELDS purely for the addon-mode
    # override path; the master-off cascade forcing it False is moot
    # because the yaml tool itself is unregistered then.
    enable_yaml_edit_confirm: bool = Field(True, alias="ENABLE_YAML_EDIT_CONFIRM")

    # Per-key gates for ``automation`` / ``script`` / ``scene`` under
    # ``packages/*.yaml``. The custom component accepts these three
    # PACKAGES_ONLY_YAML_KEYS unconditionally; ha-mcp's UI exposes a
    # toggle per key so an operator who wants YAML-managed
    # automations/scripts/scenes in packages but not the others can
    # narrow the surface. ha_config_set_yaml rejects packages/*.yaml
    # writes for a disabled key client-side, and passes the disabled set
    # to the custom component so the underlying service rejects too
    # (writes of these keys to configuration.yaml are rejected
    # independently of these flags). Each
    # toggle is meaningful only when ``enable_yaml_config_editing`` is
    # on; the UI nests these rows under that parent and dims them when
    # the parent is off.
    enable_yaml_packages_automation: bool = Field(
        False, alias="ENABLE_YAML_PACKAGES_AUTOMATION"
    )
    enable_yaml_packages_script: bool = Field(
        False, alias="ENABLE_YAML_PACKAGES_SCRIPT"
    )
    enable_yaml_packages_scene: bool = Field(False, alias="ENABLE_YAML_PACKAGES_SCENE")

    # Operator-configured extra top-level keys ha_config_set_yaml may write,
    # comma-separated, on top of the custom component's built-in allowlist
    # (#1887). For YAML-first integrations that are valid on one install but
    # not worth hardcoding globally. Additive only, and never a way past the
    # component's YAML_KEY_DENYLIST: that floor is enforced component-side
    # (the authoritative layer) and is deliberately not mirrored here, so
    # there is one copy to keep correct. A denied key typed into this setting
    # is simply ignored, with the component's explanation on first use.
    # Empty (the default) keeps today's behaviour exactly.
    # Registered in ADVANCED_SETTINGS_FIELDS (section ``beta_yamlkeys``), not
    # FEATURE_FLAG_FIELDS, because it is a value rather than a toggle – the
    # same placement the code-mode sub-settings use. Meaningful only when
    # ``enable_yaml_config_editing`` is on; the UI nests it under that parent
    # like the per-key toggles above.
    extra_yaml_write_keys: str = Field("", alias="HA_MCP_EXTRA_YAML_KEYS")

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

    # Mandatory best-practice skills — server-side master switch for the
    # write-tool skill_content delivery feature (issue #1182). When True
    # (default), the six write tools (automations / scripts / scenes /
    # helpers / dashboards / yaml) attach the canonical best-practice
    # reference files under ``skill_content`` on every successful write,
    # plus auto-embed any sections cited by best-practice warnings. The
    # per-call ``MandatoryBPS`` parameter on each tool controls whether
    # the canonical files ship for that one call. This setting is the
    # master gate above that — when False, NO skill_content goes out
    # regardless of the per-call param or BP warnings. Default on.
    enable_mandatory_bps: bool = Field(True, alias="ENABLE_MANDATORY_BPS")

    # Strict best-practices gate (issue #1779) — child flag of
    # ``enable_mandatory_bps``. When effective, the six write tools are
    # HARD-BLOCKED unless the call carries the acknowledgment key that is
    # published only inside the best-practices skill content served by
    # ``ha_get_skill_guide`` (modeled on the Hubitat MCP acknowledgment
    # gate). Default ON so strict mode is active whenever the parent is on;
    # inert when the parent is off — that cascade is enforced at the
    # consumption site (``strict_bps.strict_bps_effective``), not here,
    # because this flag is deliberately NOT a beta sub-flag and there is no
    # config-level parent gate for non-beta flags.
    enable_strict_mandatory_bps: bool = Field(True, alias="ENABLE_STRICT_MANDATORY_BPS")

    # Filesystem tools — read/write/delete/list under the HA config dir.
    # Previously gated by a direct ``os.getenv`` call in
    # ``tools/tools_filesystem.py`` so callers (and the settings UI)
    # couldn't see it through ``Settings``. Promoted to a first-class
    # Settings field so the same precedence path applies as for every
    # other gated capability.
    enable_filesystem_tools: bool = Field(False, alias="HAMCP_ENABLE_FILESYSTEM_TOOLS")

    # Dashboard screenshot mode — the ``ha_get_dashboard_screenshot`` tool
    # plus the ``include_screenshot`` / ``return_screenshot`` params on the
    # dashboard get/set tools. Renders responsive Lovelace images via a
    # separate, opt-in headless-Chromium screenshot add-on (balloob's Puppet
    # add-on, or a docker-compose sidecar). Off by default; nothing heavy is
    # pulled unless the user enables it AND installs the engine.
    enable_dashboard_screenshot: bool = Field(
        False, alias="HAMCP_ENABLE_DASHBOARD_SCREENSHOT"
    )

    # Base URL of the screenshot engine (e.g. ``http://puppet:10000`` or a
    # docker-compose sidecar). A connection string, NOT a beta toggle, so
    # it is intentionally absent from FEATURE_FLAG_FIELDS. Left blank, the
    # provisioner auto-discovers the Puppet add-on via the Supervisor in
    # HA OS / Supervised mode; Container / Core users set it explicitly.
    dashboard_screenshot_engine_url: str = Field(
        "", alias="HAMCP_DASHBOARD_SCREENSHOT_ENGINE_URL"
    )

    # Developer mode (issue #1775) — registers the hidden ha_dev_* tools
    # (server update/restart, direct settings editing). Deliberately NOT a
    # beta flag: it is a development aid, not a feature preview, and must
    # not ride the beta master gate. The toggle renders in its own
    # "Developer" section at the very bottom of the web UI's Server
    # Settings tab; it is intentionally absent from the add-on config
    # schemas so it stays out of the add-on Configuration page.
    enable_dev_mode: bool = Field(False, alias="HAMCP_ENABLE_DEV_MODE")

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

    # Snapshot-tarball deletion gate (#1861). Off by default: an agent
    # deleting a full HA snapshot is categorically riskier than the
    # lightweight `edits`-scope auto-backups (which already delete freely),
    # since a snapshot may be the last recovery point after the agent
    # itself broke something. A human must opt in via env var, the web
    # settings UI override file, or (in the add-on) the Supervisor options
    # — never something the agent can flip on itself.
    enable_snapshot_delete: bool = Field(False, alias="ENABLE_SNAPSHOT_DELETE")

    # Minimum age (days) a snapshot must have before it's deletable. This is
    # the load-bearing guard, not `enable_snapshot_delete`: a count-based
    # "keep the last N" rule is defeatable by an agent flooding new
    # snapshots before deleting old ones, but it cannot forge a backup's
    # HA-stamped creation date. 0 disables the age floor (still gated by
    # enable_snapshot_delete + the newest-snapshot / automatic-backup
    # guards enforced in tools/backup.py).
    snapshot_delete_min_age_days: int = Field(
        7, ge=0, le=365, alias="SNAPSHOT_DELETE_MIN_AGE_DAYS"
    )

    # Mirror the legacy ``os.getenv("FLAG", "").lower() in ("true", ...)``
    # semantics for the ex-direct-getenv ``enable_filesystem_tools`` flag (and
    # its sibling toggles listed above): an empty env var value MUST be treated
    # as False rather than raising
    # ``ValidationError``. Pydantic v2's bool parser raises on ``""``
    # which broke ``test_tools_filesystem.py::TestFeatureFlag::
    # test_disabled_with_empty_string`` after the migration; this
    # validator restores the contract callers rely on.
    @field_validator(
        "enable_filesystem_tools",
        "enable_dashboard_screenshot",
        mode="before",
    )
    @classmethod
    def _empty_string_means_false(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return False
        return v

    @field_validator(
        "automation_config_time_budget",
        "script_config_time_budget",
        "scene_config_time_budget",
        "individual_config_timeout",
        "individual_fetch_batch_size",
        mode="before",
    )
    @classmethod
    def _lenient_time_budget(cls, v: object, info: ValidationInfo) -> object:
        """Coerce the smart-search Attempt-C knobs (the three time budgets,
        the per-request timeout, and the fetch batch size), falling back to
        the field default (with a warning) instead of crashing startup.

        Preserves the parse-tolerance of the removed ``_env_float`` helper
        (empty / unparseable -> default) and additionally enforces the same
        ``_ADVANCED_SETTINGS_BOUNDS`` range as the override-file / UI-POST
        path, so the env-var path can't smuggle in an out-of-range or
        non-finite value. A ``<= 0`` budget or timeout would silently
        disable the per-id config-fetch scan, and ``inf`` / ``nan`` would
        uncap it; the ``lo <= val <= hi`` test rejects all three (NaN
        comparisons are False), keeping the env and override-file paths
        consistent. Int fields (batch size) additionally reject fractional
        values rather than truncating them."""
        field_name = info.field_name
        if field_name is None:  # always set for field_validator; defensive
            return v
        default = cls.model_fields[field_name].default
        if v is None or (isinstance(v, str) and not v.strip()):
            return default
        try:
            val = float(v)  # type: ignore[arg-type]
        except (ValueError, TypeError):
            logger.warning(
                "Invalid value for %s=%r; using default %s", field_name, v, default
            )
            return default
        lo, hi = _ADVANCED_SETTINGS_BOUNDS[field_name]
        if not (lo <= val <= hi):
            logger.warning(
                "%s=%r is outside %s-%s; using default %s",
                field_name,
                v,
                lo,
                hi,
                default,
            )
            return default
        if isinstance(default, int) and not isinstance(default, bool):
            if val != int(val):
                logger.warning(
                    "Invalid value for %s=%r (must be a whole number); "
                    "using default %s",
                    field_name,
                    v,
                    default,
                )
                return default
            return int(val)
        return val

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

    @field_validator("dashboard_screenshot_engine_url")
    @classmethod
    def validate_dashboard_screenshot_engine_url(cls, v: str) -> str:
        """Validate the optional screenshot-engine URL (env/.env only).

        Blank = auto-discover the engine add-on via the Supervisor. When set
        (the Docker/Container sidecar path) it must be an http(s) URL, so a
        typo fails loudly at startup instead of silently 0-byte-failing later.
        """
        if not v:
            return v
        if not v.startswith(("http://", "https://")):
            raise ValueError(
                "Screenshot engine URL must start with http:// or https://"
            )
        return v.rstrip("/")

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

    @field_validator("sidecar_pin_port", mode="before")
    @classmethod
    def _lenient_sidecar_pin_port(cls, v: object) -> int:
        """0 (default) = ephemeral port; otherwise a non-privileged port.

        Lenient like the time-budget validators: an empty / unparseable /
        out-of-range value falls back to 0 (ephemeral) with a warning rather
        than raising, so a bad ``HA_MCP_SIDECAR_PORT`` can never crash the MCP
        server or the best-effort settings sidecar.
        """
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return 0
        # bool is an int subclass but never a meaningful port; reject it
        # along with anything that isn't int/str-parseable.
        if v is None or isinstance(v, bool) or not isinstance(v, int | str):
            logger.warning("Invalid HA_MCP_SIDECAR_PORT=%r; using ephemeral port", v)
            return 0
        try:
            port = int(v)
        except (ValueError, TypeError):
            logger.warning("Invalid HA_MCP_SIDECAR_PORT=%r; using ephemeral port", v)
            return 0
        if port != 0 and not 1024 <= port <= 65535:
            logger.warning(
                "HA_MCP_SIDECAR_PORT=%r outside 1024-65535; using ephemeral port", v
            )
            return 0
        return port

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
    # Non-beta global safety toggle (discussion #1569). Lives here so the
    # Tools-tab toggle and the Server Settings row share the same
    # /api/settings/features plumbing, override-file persistence, and
    # addon Supervisor routing as every other feature flag.
    FeatureFlagField("read_only_mode", "READ_ONLY_MODE", bool),
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
    AdvancedField("entity_search_limit", "ENTITY_SEARCH_LIMIT", int, "search", True),
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
    # Settings UI sidecar (stdio-only). Pin the sidecar's port so the
    # settings URL/origin is stable across restarts; 0 = ephemeral
    # (default). #1587.
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
    "automation_config_time_budget": (1.0, 600.0),
    "script_config_time_budget": (1.0, 600.0),
    "scene_config_time_budget": (1.0, 600.0),
    "individual_config_timeout": (1.0, 600.0),
    "individual_fetch_batch_size": (1, 100),
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
    "verify_ssl",
)


def get_feature_flag_origin(env_name: str) -> str:
    """Return where the live value for ``env_name`` is sourced from.

    Used by the web UI to label each feature-flag field with its
    source and decide whether the field is editable from the web UI:

    - ``"addon"``: running inside the HA add-on AND the env var is
      currently set. ``start.py`` writes env vars from ``config.yaml``
      on every addon start; the override file is ignored. Web UI
      edits are routed through Supervisor ``/addons/self/options`` so
      ``config.yaml`` stays authoritative.
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

    Addon-mode handling for the master and beta sub-flags:

    - ``ENABLE_BETA_FEATURES`` (master) is in the DEV addon schema
      only. In dev addon mode, ``start.py`` writes
      the env var from ``/data/options.json`` when the key is present;
      that signals "Supervisor authoritative" and ``"addon"`` is
      returned here. In stable addon mode the key is absent from
      schema, ``start.py`` doesn't write the env var, and the master
      falls through to env / file / default precedence so the
      standalone web UI master path remains the gate.
    - The ``BETA_FEATURE_FIELDS`` (sub-flags) follow the same
      shape — present in dev addon schema, absent from stable. Same
      env-var-presence signal distinguishes them at runtime.
    """
    field_name = next(
        (fname for fname, ename, _ in FEATURE_FLAG_FIELDS if ename == env_name),
        None,
    )
    is_master = field_name == "enable_beta_features"
    is_beta_sub = field_name in BETA_FEATURE_FIELDS
    # is_running_in_addon() (not a raw SUPERVISOR_TOKEN read) so the in-process
    # embedded server — which carries SUPERVISOR_TOKEN on HAOS but is not an
    # add-on — is treated as a standalone deployment: its settings-UI edits
    # persist to override files under HA_MCP_CONFIG_DIR instead of being routed
    # to a Supervisor add-on that does not exist.
    in_addon = is_running_in_addon()

    if in_addon:
        if is_master or is_beta_sub:
            # Dev addon: start.py wrote the env var from options.json
            # → Supervisor is the source of truth, mark addon-editable.
            # Stable addon: env var never written → fall through to
            # file/default. The master moved from "never schema-bound"
            # to "schema-bound on dev only"; the same env-var-presence
            # signal now distinguishes both for the master and the
            # beta sub-flags.
            if os.environ.get(env_name) is not None:
                return "addon"
            # else: stable / legacy-dev-no-master-key, fall through.
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


def _coerce_feature_flag_value(
    field_name: str, ftype: RegistryFieldType, raw: object
) -> tuple[bool, Any]:
    """Coerce + bounds-check one override-file value for FEATURE_FLAG_FIELDS.

    Returns ``(ok, coerced)``. ``ok=False`` means the value was rejected
    (a warning has already been logged) and the caller should skip
    applying it. Mirrors the original inline ``continue`` behavior.
    """
    if ftype is bool:
        if not isinstance(raw, bool | int):
            logger.warning(
                "Override for %r is %s; expected bool — ignoring.",
                field_name,
                type(raw).__name__,
            )
            return False, None
        return True, bool(raw)
    elif ftype is int:
        if isinstance(raw, bool) or not isinstance(raw, int):
            logger.warning(
                "Override for %r is %s; expected int — ignoring.",
                field_name,
                type(raw).__name__,
            )
            return False, None
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
            return False, None
        return True, coerced
    return False, None


def _apply_one_feature_flag_override(
    settings: "Settings",
    field_name: str,
    env_name: str,
    ftype: RegistryFieldType,
    overrides: dict[str, object],
    in_addon: bool,
    beta_fields: set[str],
) -> None:
    """Apply a single FEATURE_FLAG_FIELDS override-file entry, if eligible."""
    is_beta = field_name in beta_fields
    if in_addon and not is_beta:
        # Non-beta addon mode: start.py owns it. Skip.
        return
    if os.environ.get(env_name) is not None:
        # Explicit env var wins over file for that field.
        return
    if field_name not in overrides:
        return
    ok, coerced = _coerce_feature_flag_value(field_name, ftype, overrides[field_name])
    if not ok:
        return
    if not hasattr(settings, field_name):
        logger.warning(
            "Override for %r (value=%r) targets a field that does "
            "not exist on Settings; ignoring. Likely a stale entry "
            "after a field was renamed/removed.",
            field_name,
            coerced,
        )
        return
    try:
        setattr(settings, field_name, coerced)
    except (ValueError, TypeError) as err:
        logger.warning(
            "Override for %r (value=%r) rejected by Settings (%s); ignoring.",
            field_name,
            coerced,
            err,
        )


def parse_extra_yaml_write_keys(settings: "Settings") -> list[str]:
    """Parse ``extra_yaml_write_keys`` into a clean key list (#1887).

    Whitespace and empty entries are dropped and the result is deduplicated
    and sorted, so the service payload is deterministic and unaffected by how
    the operator spaced the setting.

    The component's ``YAML_KEY_DENYLIST`` is NOT applied here: that floor
    lives component-side, in the layer that authorizes the write. Mirroring it
    would mean a second copy to keep in lockstep for no gain: a denied key sent
    on the wire is dropped there anyway.

    Lives in this module rather than next to the YAML tool because the backup
    restore path needs it too, and ``backup_manager`` importing a ``tools_*``
    module would add an import edge that binds ``tools_yaml_config``'s
    module-level names at restore time.
    """
    # Direct attribute access, matching ``_disabled_packages_keys``: a future
    # rename must raise loudly rather than silently return an empty list,
    # which would read as "the operator configured nothing".
    raw = settings.extra_yaml_write_keys or ""
    return sorted({segment.strip() for segment in raw.split(",") if segment.strip()})


def _apply_beta_master_gate(settings: "Settings") -> None:
    """Force BETA_FEATURE_FIELDS to False when ``enable_beta_features`` is off.

    This is the "master toggle" semantics: even a power user who sets
    ENABLE_YAML_CONFIG_EDITING=true via env var still needs to flip the
    master before the flag takes effect.
    """
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
            if current and sub not in _BETA_GATE_LOGGED:
                # Dedup per-process: cascade-clear (an earlier behavior
                # that wrote False to the override file for every truthy
                # sub-flag whenever the master was saved off) was
                # removed, so the file now holds truthy sub-flag values
                # long-term and this gate runs on every Settings
                # rebuild. Logging the force-False line every time would
                # spam addon logs. First-time-per-process is enough to
                # leave an audit trail for operators debugging "why is
                # my beta tool off?".
                logger.info(
                    "Beta master toggle is off; forcing %s=False "
                    "(was True via env/file).",
                    sub,
                )
                _BETA_GATE_LOGGED.add(sub)
            try:
                setattr(settings, sub, False)
            except (ValueError, TypeError) as err:
                logger.warning(
                    "Could not force %s=False via master gate (%s); ignoring.",
                    sub,
                    err,
                )


def _apply_feature_flag_overrides(settings: "Settings") -> None:
    """Patch ``settings`` with override-file values + apply the master beta gate.

    Two behaviors interleave:

    1. **Per-field override-file application**: reads
       ``feature_flags.json`` and applies values for each
       FEATURE_FLAG_FIELDS entry, subject to: explicit env var wins over
       file; addon mode (SUPERVISOR_TOKEN set) normally short-circuits
       this branch because start.py owns env vars from config.yaml.

       EXCEPTION: the beta-master + beta-sub-flag fields skip the
       addon-mode short-circuit. The master isn't in any addon schema;
       the sub-flags are in the dev-addon schema (where ``start.py``
       writes the env var from options.json — env-var-wins skips the
       file read here, leaving Supervisor authoritative) but NOT in the
       stable schema (where the env var is never written, so the file
       is read and applied). In standalone mode neither is addon-routed.

    2. **Beta master gate**: after the per-field pass, if
       ``enable_beta_features`` is False on the resolved Settings,
       force-set the BETA_FEATURE_FIELDS to False regardless of
       how they currently look. This is the "master toggle" semantics —
       even a power user who sets ENABLE_YAML_CONFIG_EDITING=true via
       env var still needs to flip the master before the flag takes
       effect.
    """
    # is_running_in_addon() (not a raw SUPERVISOR_TOKEN read) so the in-process
    # embedded server — which carries SUPERVISOR_TOKEN on HAOS but is not an
    # add-on — applies its settings-UI feature-flag saves like a standalone
    # deployment instead of short-circuiting here as if start.py owned the env.
    in_addon = is_running_in_addon()
    overrides = _read_feature_flag_override_file()

    known = {fname: (ename, ftype) for fname, ename, ftype in FEATURE_FLAG_FIELDS}
    beta_fields = {"enable_beta_features", *BETA_FEATURE_FIELDS}

    for field_name, (env_name, ftype) in known.items():
        _apply_one_feature_flag_override(
            settings, field_name, env_name, ftype, overrides, in_addon, beta_fields
        )

    _apply_beta_master_gate(settings)


def _coerce_advanced_override_value(
    fname: str, ftype: RegistryFieldType, raw: object
) -> tuple[bool, Any]:
    """Coerce one override-file value to its ADVANCED_SETTINGS_FIELDS type.

    Returns ``(ok, coerced)``. ``ok=False`` means the value was rejected
    (a warning has already been logged) and the caller should skip
    applying it.
    """
    if ftype is bool:
        if not isinstance(raw, bool | int):
            logger.warning(
                "Advanced override for %r is %s; expected bool — ignoring.",
                fname,
                type(raw).__name__,
            )
            return False, None
        return True, bool(raw)
    elif ftype is int:
        if isinstance(raw, bool) or not isinstance(raw, int):
            logger.warning(
                "Advanced override for %r is %s; expected int — ignoring.",
                fname,
                type(raw).__name__,
            )
            return False, None
        return True, int(raw)
    elif ftype is float:
        if isinstance(raw, bool) or not isinstance(raw, int | float):
            logger.warning(
                "Advanced override for %r is %s; expected float — ignoring.",
                fname,
                type(raw).__name__,
            )
            return False, None
        return True, float(raw)
    elif ftype is str:
        if not isinstance(raw, str):
            logger.warning(
                "Advanced override for %r is %s; expected str — ignoring.",
                fname,
                type(raw).__name__,
            )
            return False, None
        if "\x00" in raw:
            logger.warning(
                "Advanced override for %r contains null byte; ignoring.",
                fname,
            )
            return False, None
        return True, raw
    return False, None


def _advanced_override_passes_constraints(fname: str, coerced: Any) -> bool:
    """Bounds/sentinel/choices gate for a coerced ADVANCED_SETTINGS_FIELDS value."""
    bounds = _ADVANCED_SETTINGS_BOUNDS.get(fname)
    sentinel = _ADVANCED_SETTINGS_SENTINELS.get(fname)
    if (
        bounds is not None
        and coerced != sentinel
        and not (bounds[0] <= coerced <= bounds[1])
    ):
        logger.warning(
            "Advanced override for %r is %s, outside %s-%s — ignoring.",
            fname,
            coerced,
            bounds[0],
            bounds[1],
        )
        return False
    choices = _ADVANCED_SETTINGS_CHOICES.get(fname)
    if choices is not None and coerced not in choices:
        logger.warning(
            "Advanced override for %r is %r, not in %s — ignoring.",
            fname,
            coerced,
            choices,
        )
        return False
    return True


def _apply_one_advanced_override(
    settings: "Settings",
    fname: str,
    env_name: str,
    ftype: RegistryFieldType,
    editable: bool,
    overrides: dict[str, object],
) -> None:
    """Apply a single ADVANCED_SETTINGS_FIELDS override-file entry, if eligible."""
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
        return
    if os.environ.get(env_name) is not None:
        return
    if fname not in overrides:
        return
    ok, coerced = _coerce_advanced_override_value(fname, ftype, overrides[fname])
    if not ok:
        return
    if not _advanced_override_passes_constraints(fname, coerced):
        return
    try:
        setattr(settings, fname, coerced)
    except (ValueError, TypeError):
        # Narrowed from bare ``Exception`` to match the parallel
        # _apply_feature_flag_overrides handler. Pydantic validation
        # surfaces failures as ValueError; an
        # attribute that doesn't exist on the model would be a
        # programming bug we want to crash, not silently swallow.
        logger.warning(
            "Advanced override for %r could not be applied; ignoring.",
            fname,
            exc_info=True,
        )


def _apply_advanced_overrides(settings: "Settings") -> None:
    """Patch ``settings`` with advanced-section override values from
    ``feature_flags.json``.

    Mirrors ``_apply_feature_flag_overrides`` but iterates
    ``ADVANCED_SETTINGS_FIELDS`` and supports float / str in addition
    to bool / int. Display-only fields (``editable=False`` in the
    registry) are NEVER applied — chicken-and-egg safeguard for
    connection settings.

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
        _apply_one_advanced_override(
            settings, fname, env_name, ftype, editable, overrides
        )


# Global settings instance
_settings: Settings | None = None

# In-process (embedded) HA connection, set only when ha-mcp runs inside Home
# Assistant core via the ha_mcp_tools custom component's in-process server entry.
# The component hands the loopback URL + a provisioned admin token to ha-mcp
# THROUGH THIS DICT — never
# via os.environ — so the admin token can't be read from the shared HA process
# environment. Applied onto the Settings singleton in ``get_global_settings``.
_EMBEDDED_CONNECTION: dict[str, str | bool] = {}


def set_embedded_connection(
    url: str, token: str, verify_ssl: bool | None = None
) -> None:
    """Register the in-process HA connection for embedded mode.

    Embedded-mode only: called by the ha_mcp_tools custom component's in-process
    server entry inside its server worker thread, before the server is
    constructed, so the loopback URL
    and admin token reach ``Settings`` in memory instead of through ``os.environ``.
    The values survive ``_reset_global_settings()``: the settings-UI reset+rebuild
    path re-applies them on the next ``get_global_settings()`` call.

    Also applies to an ALREADY-BUILT singleton: importing ``ha_mcp`` runs the
    package's eager import chain, and ``tools/smart_search/_config.py`` builds
    the settings singleton at import time (its documented read-once budgets).
    Registration therefore cannot assume it runs before the first build — the
    integration imports this function from the very package whose import
    creates the singleton.

    ``verify_ssl`` lets the component disable certificate verification when it
    derives an ``https://127.0.0.1`` loopback URL from Home Assistant's SSL
    config (issue #1890): HA's certificate is issued for its hostname, never
    for 127.0.0.1, so verification on the loopback connection can only fail.
    ``None`` (the default, and what pre-#1890 components pass implicitly)
    leaves ``Settings.verify_ssl`` alone.
    """
    _EMBEDDED_CONNECTION["url"] = url
    _EMBEDDED_CONNECTION["token"] = token
    if verify_ssl is None:
        _EMBEDDED_CONNECTION.pop("verify_ssl", None)
    else:
        _EMBEDDED_CONNECTION["verify_ssl"] = verify_ssl
    if _settings is not None:
        _apply_embedded_connection(_settings)


def _reset_embedded_connection() -> None:
    """Drop the registered embedded connection (test seam).

    Sibling to :func:`_reset_global_settings`; lets suites that exercise the
    in-process token channel isolate state between tests. Not used in production —
    the connection is registered once per worker thread and is meant to persist.
    """
    _EMBEDDED_CONNECTION.clear()


# Names of beta sub-flags the master gate has already logged a
# force-False line for in this process. Used to dedup the gate's
# INFO log so we don't spam addon logs on every Settings rebuild
# now that the cascade-clear is gone and the file may carry truthy
# sub-flag values long-term. Reset alongside
# the Settings singleton in ``_reset_global_settings``.
_BETA_GATE_LOGGED: set[str] = set()


# Auto-backup runtime-editable fields (#1288 web UI editor). Each entry
# is (field_name, env_var_name, python_type). The web UI's
# /api/settings/backups/config GET/POST endpoints iterate this tuple to
# advertise per-field origin (env / addon / file / default) and to
# validate incoming writes. Keep aligned with the matching ``Settings``
# fields above — adding a fourth runtime-editable setting means a new
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
    BackupOverrideField("enable_snapshot_delete", "ENABLE_SNAPSHOT_DELETE", bool),
    BackupOverrideField(
        "snapshot_delete_min_age_days", "SNAPSHOT_DELETE_MIN_AGE_DAYS", int
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
    # is_running_in_addon() rather than a raw SUPERVISOR_TOKEN read so the
    # embedded in-process server (SUPERVISOR_TOKEN present on HAOS, but not an
    # add-on) is labeled a standalone deployment and its backup settings persist
    # to the override file instead of a non-existent Supervisor add-on.
    if is_running_in_addon():
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


def _coerce_backup_int_value(field_name: str, raw: object) -> tuple[bool, Any]:
    """Coerce + per-field range-check one int-typed BACKUP_OVERRIDE_FIELDS value.

    Split out of ``_coerce_backup_override_value`` (mccabe complexity):
    handles the int-parse plus the three field-specific range checks.
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        logger.warning(
            "backup_settings.json: %s expects int, got %s; ignoring",
            field_name,
            type(raw).__name__,
        )
        return False, None
    coerced = int(raw)
    if field_name == "auto_backup_throttle_minutes" and not 0 <= coerced <= 1440:
        logger.warning(
            "backup_settings.json: auto_backup_throttle_minutes=%d out of "
            "range 0..1440; ignoring",
            coerced,
        )
        return False, None
    if field_name == "auto_backup_retain_per_entity" and not 1 <= coerced <= 10_000:
        logger.warning(
            "backup_settings.json: auto_backup_retain_per_entity=%d out of "
            "range 1..10000; ignoring",
            coerced,
        )
        return False, None
    if field_name == "auto_backup_calendar_lookahead_days" and not 1 <= coerced <= 365:
        logger.warning(
            "backup_settings.json: auto_backup_calendar_lookahead_days=%d out of "
            "range 1..365; ignoring",
            coerced,
        )
        return False, None
    if field_name == "snapshot_delete_min_age_days" and not 0 <= coerced <= 365:
        logger.warning(
            "backup_settings.json: snapshot_delete_min_age_days=%d out of "
            "range 0..365; ignoring",
            coerced,
        )
        return False, None
    return True, coerced


def _coerce_backup_override_value(
    field_name: str, ftype: RegistryFieldType, raw: object
) -> tuple[bool, Any]:
    """Coerce + range-check one override-file value for BACKUP_OVERRIDE_FIELDS.

    Returns ``(ok, coerced)``. ``ok=False`` means the value was rejected
    (a warning has already been logged) and the caller should skip
    applying it.
    """
    if ftype is bool:
        if not isinstance(raw, bool | int):
            logger.warning(
                "backup_settings.json: %s expects bool, got %s; ignoring",
                field_name,
                type(raw).__name__,
            )
            return False, None
        return True, bool(raw)
    elif ftype is int:
        return _coerce_backup_int_value(field_name, raw)
    elif ftype is str:
        if not isinstance(raw, str):
            logger.warning(
                "backup_settings.json: %s expects str, got %s; ignoring",
                field_name,
                type(raw).__name__,
            )
            return False, None
        if "\x00" in raw:
            logger.warning(
                "backup_settings.json: %s contains null byte; ignoring",
                field_name,
            )
            return False, None
        return True, raw
    return False, None


def _apply_one_backup_override(
    settings: "Settings",
    field_name: str,
    env_name: str,
    ftype: RegistryFieldType,
    overrides: dict[str, object],
) -> None:
    """Apply a single BACKUP_OVERRIDE_FIELDS override-file entry, if eligible."""
    if os.environ.get(env_name) is not None:
        return
    if field_name not in overrides:
        return
    ok, coerced = _coerce_backup_override_value(
        field_name, ftype, overrides[field_name]
    )
    if not ok:
        return
    try:
        setattr(settings, field_name, coerced)
    except (ValueError, TypeError) as err:
        logger.warning(
            "backup_settings.json: setattr(%s, %r) rejected by Settings (%s); ignoring",
            field_name,
            coerced,
            err,
        )


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
    # is_running_in_addon() (embedded-aware) so the in-process server applies
    # its backup override file like a standalone deployment; see the matching
    # rationale in _apply_feature_flag_overrides / get_backup_setting_origin.
    if is_running_in_addon():
        return
    overrides = _read_backup_override_file()
    if not overrides:
        return
    for field_name, env_name, ftype in BACKUP_OVERRIDE_FIELDS:
        _apply_one_backup_override(settings, field_name, env_name, ftype, overrides)


def _apply_embedded_connection(settings: "Settings") -> None:
    """Apply the in-process embedded HA connection (url/token/verify_ssl) if registered.

    No-op outside embedded mode. Plain ``setattr`` (``validate_assignment`` is off
    on ``Settings``, mirroring ``_apply_backup_overrides``), so the loopback URL
    and admin token are set in memory without ever passing through ``os.environ``.
    Applied last so it wins over any env/override-file connection values.
    """
    url = _EMBEDDED_CONNECTION.get("url")
    token = _EMBEDDED_CONNECTION.get("token")
    if isinstance(url, str) and url:
        settings.homeassistant_url = url.rstrip("/")
    if isinstance(token, str) and token:
        settings.homeassistant_token = token
    verify_ssl = _EMBEDDED_CONNECTION.get("verify_ssl")
    if isinstance(verify_ssl, bool):
        settings.verify_ssl = verify_ssl


def get_global_settings() -> Settings:
    """Get global settings instance (singleton pattern).

    Applies override files at first read so web-UI edits take effect
    on the next ``get_global_settings()`` call after
    ``_reset_global_settings()`` is called by the POST handler:

    - Feature flags persisted to ``<data_dir>/feature_flags.json``
    - Auto-backup settings persisted to ``<data_dir>/backup_settings.json``

    In embedded mode, the in-process HA connection registered via
    ``set_embedded_connection`` is applied last (so a settings-UI reset+rebuild
    re-picks it up).
    """
    global _settings
    if _settings is None:
        _settings = get_settings()
        _apply_feature_flag_overrides(_settings)
        _apply_backup_overrides(_settings)
        _apply_advanced_overrides(_settings)
        _apply_embedded_connection(_settings)
    return _settings


def reset_global_settings() -> None:
    """Public seam to drop the cached settings singleton.

    The in-process (embedded) server calls this at every start: a config-entry
    reload reuses the same Python process, so without an explicit reset the
    singleton built on the FIRST start would keep serving stale feature-flag /
    override values forever (the add-on gets fresh settings for free from its
    process restart).
    """
    _reset_global_settings()


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
    # Drop the gate-log dedup set too — once Settings has been
    # rebuilt, an operator who's re-investigating "why is my beta
    # tool off?" should see the next gate fire logged. This keeps
    # the dedup tight to the lifetime of one cached Settings.
    _BETA_GATE_LOGGED.clear()


# Import-time validator for cross-registry invariants.
#
# Catches a class of silent runtime bugs at module load:
#  - registry rows referencing fields that don't exist on ``Settings``
#    (rename / removal drift)
#  - registries overlapping by name (same field applied by two
#    ``_apply_*_overrides`` functions with potentially divergent
#    coercion policies)
#  - bounds entries pointing at non-numeric fields (would silently
#    no-op in the apply loop)
#  - choices entries pointing at non-str fields (same)
#  - ``BETA_FEATURE_FIELDS`` referencing names not in
#    ``FEATURE_FLAG_FIELDS`` (master gate would write to phantom
#    Settings attributes)
def _validate_registry_fields_exist(
    settings_fields: set[str],
    advanced_names: set[str],
    flag_names: set[str],
    backup_names: set[str],
) -> None:
    """Every registry row must reference a real Settings field."""
    for registry_name, names in (
        ("ADVANCED_SETTINGS_FIELDS", advanced_names),
        ("FEATURE_FLAG_FIELDS", flag_names),
        ("BACKUP_OVERRIDE_FIELDS", backup_names),
    ):
        missing = names - settings_fields
        if missing:
            raise RuntimeError(
                f"{registry_name} references fields not on Settings: {sorted(missing)}"
            )


def _validate_registries_disjoint(
    advanced_names: set[str], flag_names: set[str], backup_names: set[str]
) -> None:
    """Registries must be name-disjoint to avoid double-apply with divergent policies."""
    overlaps = {
        ("advanced", "flags"): advanced_names & flag_names,
        ("advanced", "backup"): advanced_names & backup_names,
        ("flags", "backup"): flag_names & backup_names,
    }
    for (a, b), shared in overlaps.items():
        if shared:
            raise RuntimeError(
                f"Registry overlap between {a} and {b}: {sorted(shared)}. "
                "Each field must be applied by exactly one of "
                "_apply_feature_flag_overrides / _apply_advanced_overrides "
                "/ _apply_backup_overrides."
            )


def _validate_advanced_bounds(advanced_by_name: dict[str, AdvancedField]) -> None:
    """_ADVANCED_SETTINGS_BOUNDS keys must be advanced fields AND numeric."""
    for name in _ADVANCED_SETTINGS_BOUNDS:
        if name not in advanced_by_name:
            raise RuntimeError(
                f"_ADVANCED_SETTINGS_BOUNDS[{name!r}] is not in "
                "ADVANCED_SETTINGS_FIELDS"
            )
        if advanced_by_name[name].ftype not in (int, float):
            raise RuntimeError(
                f"_ADVANCED_SETTINGS_BOUNDS[{name!r}] is on a non-numeric "
                f"field (type={advanced_by_name[name].ftype.__name__})"
            )


def _validate_advanced_choices(advanced_by_name: dict[str, AdvancedField]) -> None:
    """_ADVANCED_SETTINGS_CHOICES keys must be advanced fields AND str."""
    for name in _ADVANCED_SETTINGS_CHOICES:
        if name not in advanced_by_name:
            raise RuntimeError(
                f"_ADVANCED_SETTINGS_CHOICES[{name!r}] is not in "
                "ADVANCED_SETTINGS_FIELDS"
            )
        if advanced_by_name[name].ftype is not str:
            raise RuntimeError(
                f"_ADVANCED_SETTINGS_CHOICES[{name!r}] is on a non-str "
                f"field (type={advanced_by_name[name].ftype.__name__})"
            )


def _validate_beta_subset(flag_names: set[str]) -> None:
    """BETA_FEATURE_FIELDS must be a subset of FEATURE_FLAG_FIELDS.

    The master gate writes to them via setattr; phantom names would
    silently land on extras with no effect on the runtime gate.
    """
    beta_set = set(BETA_FEATURE_FIELDS)
    not_in_flags = beta_set - flag_names
    if not_in_flags:
        raise RuntimeError(
            f"BETA_FEATURE_FIELDS contains names not in FEATURE_FLAG_FIELDS: "
            f"{sorted(not_in_flags)}"
        )


def _validate_registries() -> None:
    settings_fields = set(Settings.model_fields.keys())

    advanced_names = {f.field for f in ADVANCED_SETTINGS_FIELDS}
    flag_names = {f.field for f in FEATURE_FLAG_FIELDS}
    backup_names = {f.field for f in BACKUP_OVERRIDE_FIELDS}

    _validate_registry_fields_exist(
        settings_fields, advanced_names, flag_names, backup_names
    )
    _validate_registries_disjoint(advanced_names, flag_names, backup_names)

    advanced_by_name = {f.field: f for f in ADVANCED_SETTINGS_FIELDS}
    _validate_advanced_bounds(advanced_by_name)
    _validate_advanced_choices(advanced_by_name)

    _validate_beta_subset(flag_names)


_validate_registries()
