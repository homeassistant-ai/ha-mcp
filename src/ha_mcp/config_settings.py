"""The ``Settings`` model and the environment-file bootstrap it reads."""

import logging
import os

# Load environment variables from .env file with HAMCP_ENV_FILE support
# Use absolute path to ensure .env is found regardless of cwd
from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ha_mcp._version import get_version
from ha_mcp.config_registry import _ADVANCED_SETTINGS_BOUNDS

# All config modules log under the ``ha_mcp.config`` logger name.
logger = logging.getLogger("ha_mcp.config")

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
if not env_path.exists():
    # Fallback to default .env
    env_path = project_root / ".env"

# Load the environment file (silently, since env vars may come from other sources)
if env_path.exists():
    load_dotenv(env_path)


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

    # Optional process-wide outer tool-call concurrency. Zero preserves the
    # existing unlimited behavior; constrained installs can opt into queuing.
    ha_tool_concurrency: int = Field(0, ge=0, le=32, alias="HA_TOOL_CONCURRENCY")

    # Smart-search config-fetch time budgets (seconds). Bound how long
    # ha_search spends fetching automation/script/scene
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

    # Optional preflight bounds for recorder queries. Disabled by default to
    # preserve the established ha_get_history request contract.
    enable_history_query_guardrails: bool = Field(
        False, alias="HAMCP_ENABLE_HISTORY_QUERY_GUARDRAILS"
    )

    # Backup tool configuration
    backup_hint: str = Field("normal", alias="BACKUP_HINT")

    # WebSocket configuration (essential for async operations)
    enable_websocket: bool = Field(True, alias="ENABLE_WEBSOCKET")

    # Settings UI sidecar (stdio mode only, #1587). 0 (default) = pick a
    # free ephemeral port on the first spawn and reuse it afterwards
    # (persisted in ui.state, #2131) so the settings URL/origin stays
    # stable across restarts; 1024-65535 pins a preferred fixed port
    # instead (best-effort: falls back to an ephemeral one if taken).
    # Read by run_main() in stdio_settings_sidecar.py.
    sidecar_pin_port: int = Field(0, alias="HA_MCP_SIDECAR_PORT")

    # Development/Debug configuration
    debug: bool = Field(False, alias="DEBUG")
    log_level: str = Field("INFO", alias="LOG_LEVEL")

    # Opt-in HTTP experiments, applied at app construction (restart required).
    http_transport_diagnostics: bool = Field(
        False, alias="HAMCP_HTTP_TRANSPORT_DIAGNOSTICS"
    )
    http_json_response: bool = Field(False, alias="HAMCP_HTTP_JSON_RESPONSE")

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

    # Security-policy editing tool (issue #2148) — registers
    # ha_manage_security_policy, an ordinary MCP tool that reads and rewrites
    # the same tool_policy.json the Tool Security Policies tab edits. Disabled
    # by default, and deliberately NOT a beta flag: it is a standing safety
    # decision, not a preview feature, so it must stay out of
    # BETA_FEATURE_FIELDS (where the beta master would force it off). Its
    # toggle renders on the Policies tab, next to the master switch above.
    enable_security_policy_tool: bool = Field(
        False, alias="ENABLE_SECURITY_POLICY_TOOL"
    )

    # Read Only Mode — global safety toggle (discussion #1569). When on,
    # write-capable tools are hidden from the MCP catalog and every write
    # operation is blocked at call time with a structured READ_ONLY_MODE
    # error. Mixed read/write tools whose read surface has no pure-read
    # duplicate stay available with their write actions blocked (see
    # read_only.py:READ_ONLY_EXEMPT_TOOLS). Off by default.
    read_only_mode: bool = Field(False, alias="READ_ONLY_MODE")

    # Redact Secrets — opt-in secret redaction (issue #2157). When on,
    # add-on option values whose schema entry carries ``format: password``
    # and integration option fields marked with a password selector are
    # replaced with set/empty sentinels, and any tool response is scrubbed
    # of secret values already seen while serving those surfaces (see
    # redaction.py). Off by default; no redaction runs while off, with one
    # deliberate exception: the sentinel write guards are unconditional, so
    # a submitted value that is or contains a redaction marker is rejected
    # even with the flag off — a marker captured while it was on must never
    # overwrite a live credential.
    redact_secrets: bool = Field(False, alias="REDACT_SECRETS")

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
    # Nor does it lift the packages-only restriction on automation/script/
    # scene: those keep reaching packages/*.yaml through their own per-key
    # toggles and stay rejected in configuration.yaml.
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

    # Dev-tools access to tool-security policy state (issue #2141).
    # Developer mode may stay on while this stays off: the dev tools'
    # policy-override surfaces — set_policy, set_tool(gated=...),
    # approve/deny of queued approvals, and set/reset of
    # enable_tool_security_policies — are refused while it is off, so a
    # connected agent cannot rewrite the rules that gate it nor click
    # "accept" on its own gated calls. The guard reads env var + override
    # file fresh per call (NOT this cached Settings object), so a change
    # applies live without a restart even in stdio mode, where the web
    # settings UI runs in a detached sidecar process whose POST cannot
    # reset this process' settings singleton. Editable
    # from the web settings UI (Developer section) or the env var ONLY —
    # the dev tools' own settings surfaces refuse to write this field, in
    # either direction, and it is absent from the add-on config schemas
    # like enable_dev_mode. A leash on those surfaces, NOT a sandbox: dev
    # mode's update_source/restart can still replace the running server
    # build, and in add-on mode ha_manage_app can reach the add-on's
    # own options and ingress — gate those tools with policy rules (or
    # keep dev mode off) where that boundary matters.
    dev_tools_security_policy_access: bool = Field(
        False, alias="HAMCP_DEV_SECURITY_POLICY_ACCESS"
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
    # otherwise ``<data dir>/backups`` (see ``backup_manager._resolve_default_dir``).
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
        "enable_security_policy_tool",
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
        # Absolute, and the same file load_dotenv already resolved above. A
        # relative ".env" here would be a second, independent read that
        # pydantic-settings resolves against the process's working directory —
        # so HAMCP_ENV_FILE would not govern it, and a stray .env in whatever
        # directory the server was launched from would silently supply values.
        env_file=str(env_path),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="allow",
    )


def get_settings() -> Settings:
    """Get application settings."""
    return Settings()  # type: ignore[call-arg]
