"""The ``Settings`` model and the environment-file bootstrap it reads."""

import logging
import os

# Load environment variables from .env file with HAMCP_ENV_FILE support
# Use absolute path to ensure .env is found regardless of cwd
from pathlib import Path
from typing import Annotated

from dotenv import load_dotenv
from pydantic import Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ha_mcp._version import get_version
from ha_mcp.config_meta import AppOption, Setting, setting_of

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


# The app options declared by both flavors, and by the dev flavor only.
_ALL_FLAVORS = AppOption()
_DEV_FLAVOR = AppOption(flavors=("dev",))


def _one_of(model: type[BaseSettings], field_name: str, value: str) -> str:
    """Return ``value`` if it is one of the field's choices, else raise."""
    setting = setting_of(model.model_fields[field_name])
    assert setting is not None and setting.choices is not None
    if value not in setting.choices:
        raise ValueError(f"must be one of {', '.join(setting.choices)}")
    return value


def _parse_lenient(
    v: object, setting: Setting, whole: bool
) -> tuple[object, str | None]:
    """Parse a value for a ``lenient`` field.

    Returns the parsed value and ``None``, or ``None`` and why the value
    is invalid.
    """
    if setting.choices is not None:
        if v in setting.choices:
            return v, None
        return None, f"must be one of {', '.join(setting.choices)}"
    if isinstance(v, bool):
        return None, "not a number"
    try:
        val = float(v)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return None, "not a number"
    if val == setting.off_value:
        return int(val), None
    assert setting.range is not None
    lo, hi = setting.range
    # NaN and infinity fail this test too, so they cannot uncap a budget.
    if not lo <= val <= hi:
        return None, f"outside {lo:g}-{hi:g}"
    if whole and val != int(val):
        return None, "must be a whole number"
    return (int(val) if whole else val), None


class Settings(BaseSettings):
    """Application settings loaded from environment variables.

    Each field's ``Setting`` metadata declares where it appears: the web UI
    surface, the app option and the range. ``config_registry`` and
    ``scripts/generate_app_options.py`` derive everything else from it, so
    the field order within each web UI surface is the order of its rows.
    """

    # ===== Advanced settings (web UI Advanced section) =====

    # Home Assistant connection
    # In OAuth mode, these are optional and provided per-request.
    # Display-only in the UI (chicken-and-egg: if you could break the
    # connection from the UI you couldn't use the same UI to fix it).
    homeassistant_url: Annotated[
        str, Setting(surface="advanced", section="connection", editable=False)
    ] = Field(default=OAUTH_MODE_URL, alias="HOMEASSISTANT_URL")
    homeassistant_token: Annotated[
        str, Setting(surface="advanced", section="connection", editable=False)
    ] = Field(default=OAUTH_MODE_TOKEN, alias="HOMEASSISTANT_TOKEN")

    # Server configuration. The REST client is built once at startup, so
    # these need a restart even though they apply per request.
    timeout: Annotated[
        int,
        Setting(
            surface="advanced",
            section="connection",
            restart_required=True,
            range=(1, 600),
            lenient=True,
        ),
    ] = Field(30, alias="HA_TIMEOUT")
    max_retries: Annotated[
        int,
        Setting(
            surface="advanced",
            section="connection",
            restart_required=True,
            range=(0, 20),
            lenient=True,
        ),
    ] = Field(3, alias="HA_MAX_RETRIES")

    # False = skip TLS verification (self-signed / hostname mismatch). Trusted networks only.
    verify_ssl: Annotated[
        bool,
        Setting(
            surface="advanced",
            section="operations",
            app=_ALL_FLAVORS,
            restart_required=True,
        ),
    ] = Field(True, alias="HA_VERIFY_SSL")

    # Tool configuration. Read once by the SmartSearchTools singleton, so a
    # change needs a restart to rebuild the searcher.
    fuzzy_threshold: Annotated[
        int,
        Setting(
            surface="advanced",
            section="search",
            restart_required=True,
            range=(0, 100),
        ),
    ] = Field(60, alias="FUZZY_THRESHOLD")

    # Smart-search config-fetch time budgets (seconds). Bound how long
    # ha_search spends fetching automation/script/scene
    # definitions during the per-id fallback before reporting a partial
    # result. Surfaced in the Advanced settings panel (issue #1538) so
    # add-on users — who cannot set raw env vars — can tune them. Consumed
    # as import-time module constants in tools/smart_search/_config.py, so
    # a change requires an MCP-host restart to take effect. A ``<= 0``
    # budget would silently disable the per-id config-fetch scan, and
    # ``inf`` / ``nan`` would uncap it, so values outside the range fall
    # back to the default.
    automation_config_time_budget: Annotated[
        float,
        Setting(
            surface="advanced",
            section="search",
            restart_required=True,
            range=(1.0, 600.0),
            lenient=True,
        ),
    ] = Field(30.0, alias="HAMCP_AUTOMATION_CONFIG_TIME_BUDGET")
    script_config_time_budget: Annotated[
        float,
        Setting(
            surface="advanced",
            section="search",
            restart_required=True,
            range=(1.0, 600.0),
            lenient=True,
        ),
    ] = Field(20.0, alias="HAMCP_SCRIPT_CONFIG_TIME_BUDGET")
    scene_config_time_budget: Annotated[
        float,
        Setting(
            surface="advanced",
            section="search",
            restart_required=True,
            range=(1.0, 600.0),
            lenient=True,
        ),
    ] = Field(20.0, alias="HAMCP_SCENE_CONFIG_TIME_BUDGET")

    # Per-request timeout and concurrency of the smart-search per-id
    # config-fetch fallback (Attempt C). On HA servers that serve
    # /config/<domain>/config/<id> serially, a full batch of concurrent
    # requests queues behind one another and the tail of each batch can
    # exceed the per-request timeout even though every request would
    # succeed — lowering the batch size (toward 1) and/or raising the
    # timeout lets such instances scan exhaustively (issue #1784). Same
    # consumption model as the budgets above: import-time constants in
    # tools/smart_search/_config.py, restart required.
    individual_config_timeout: Annotated[
        float,
        Setting(
            surface="advanced",
            section="search",
            restart_required=True,
            range=(1.0, 600.0),
            lenient=True,
        ),
    ] = Field(5.0, alias="HAMCP_INDIVIDUAL_CONFIG_TIMEOUT")
    individual_fetch_batch_size: Annotated[
        int,
        Setting(
            surface="advanced",
            section="search",
            restart_required=True,
            range=(1, 100),
            lenient=True,
        ),
    ] = Field(10, alias="HAMCP_INDIVIDUAL_FETCH_BATCH_SIZE")

    # Optional preflight bounds for recorder queries. Disabled by default to
    # preserve the established ha_get_history request contract.
    enable_history_query_guardrails: Annotated[
        bool, Setting(surface="advanced", section="operations")
    ] = Field(False, alias="HAMCP_ENABLE_HISTORY_QUERY_GUARDRAILS")

    # Optional process-wide outer tool-call concurrency. Zero preserves the
    # existing unlimited behavior; constrained installs can opt into queuing.
    ha_tool_concurrency: Annotated[
        int,
        Setting(
            surface="advanced",
            section="operations",
            app=_ALL_FLAVORS,
            restart_required=True,
            range=(0, 32),
        ),
    ] = Field(0, alias="HA_TOOL_CONCURRENCY")

    # Backup tool configuration. The app requires this option.
    backup_hint: Annotated[
        str,
        Setting(
            surface="advanced",
            section="operations",
            app=AppOption(required=True),
            choices=("strong", "normal", "weak", "auto"),
        ),
    ] = Field("normal", alias="BACKUP_HINT")

    # WebSocket configuration (essential for async operations)
    enable_websocket: Annotated[
        bool,
        Setting(surface="advanced", section="operations", restart_required=True),
    ] = Field(True, alias="ENABLE_WEBSOCKET")

    # Base URL of the screenshot engine (e.g. ``http://puppet:10000`` or a
    # docker-compose sidecar). A connection string, NOT a beta toggle, so
    # it is not a feature flag. Left blank, the
    # provisioner auto-discovers the Puppet add-on via the Supervisor in
    # HA OS / Supervised mode; Container / Core users set it explicitly.
    # Resolved live per capture (resolve_engine), so unlike the time
    # budgets it takes effect without a restart (#1538).
    dashboard_screenshot_engine_url: Annotated[
        str, Setting(surface="advanced", section="operations")
    ] = Field("", alias="HAMCP_DASHBOARD_SCREENSHOT_ENGINE_URL")

    # Tool filtering - comma-separated list of module names to enable
    # Special values: "all" (default), "automation" (automation-related tools only)
    # Examples: "tools_config_automations,tools_config_scripts,tools_traces"
    enabled_tool_modules: Annotated[
        str,
        Setting(surface="advanced", section="tools_surface", restart_required=True),
    ] = Field("all", alias="ENABLED_TOOL_MODULES")

    # Dashboard partial update tools (python_transform, find_card)
    # These are token-efficient alternatives to full config replacement.
    # Disable when using clients with programmatic tool use (future).
    enable_dashboard_partial_tools: Annotated[
        bool, Setting(surface="advanced", section="tools_surface")
    ] = Field(True, alias="ENABLE_DASHBOARD_PARTIAL_TOOLS")

    # MCP Server configuration
    mcp_server_name: Annotated[
        str,
        Setting(surface="advanced", section="diagnostics", restart_required=True),
    ] = Field("ha-mcp", alias="MCP_SERVER_NAME")
    # Editable (it has an env alias), but the UI warns that overriding it
    # can confuse clients.
    mcp_server_version: Annotated[
        str,
        Setting(surface="advanced", section="diagnostics", restart_required=True),
    ] = Field(default=_PACKAGE_VERSION, alias="MCP_SERVER_VERSION")

    # Environment configuration
    environment: Annotated[
        str,
        Setting(
            surface="advanced",
            section="diagnostics",
            restart_required=True,
            choices=("development", "production"),
            lenient=True,
        ),
    ] = Field("development", alias="ENVIRONMENT")

    # Development/Debug configuration
    log_level: Annotated[
        str,
        Setting(
            surface="advanced",
            section="diagnostics",
            restart_required=True,
            choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        ),
    ] = Field("INFO", alias="LOG_LEVEL")
    debug: Annotated[
        bool,
        Setting(surface="advanced", section="diagnostics", restart_required=True),
    ] = Field(False, alias="DEBUG")

    # Opt-in HTTP experiments, applied at app construction (restart required).
    http_transport_diagnostics: Annotated[
        bool,
        Setting(surface="advanced", section="diagnostics", restart_required=True),
    ] = Field(False, alias="HAMCP_HTTP_TRANSPORT_DIAGNOSTICS")
    http_json_response: Annotated[
        bool,
        Setting(surface="advanced", section="diagnostics", restart_required=True),
    ] = Field(False, alias="HAMCP_HTTP_JSON_RESPONSE")

    # Settings UI sidecar (stdio mode only, #1587). 0 (default) = pick a
    # free ephemeral port on the first spawn and reuse it afterwards
    # (persisted in ui.state, #2131) so the settings URL/origin stays
    # stable across restarts; 1024-65535 pins a preferred fixed port
    # instead (best-effort: falls back to an ephemeral one if taken).
    # Read by run_main() in stdio_settings_sidecar.py, which binds the
    # port once at spawn, so a change needs a restart. A bad
    # ``HA_MCP_SIDECAR_PORT`` falls back to 0 and can never crash the MCP
    # server or the best-effort settings sidecar.
    sidecar_pin_port: Annotated[
        int,
        Setting(
            surface="advanced",
            section="sidecar",
            restart_required=True,
            range=(1024, 65535),
            off_value=0,
            lenient=True,
        ),
    ] = Field(0, alias="HA_MCP_SIDECAR_PORT")

    # Code-mode limits (only meaningful when enable_code_mode is on). The UI
    # nests them under the beta section's enable_code_mode row, dimmed and
    # disabled when code mode is off. Range bounds reject zero/negative
    # values that would silently break the tool and clamp upper bounds at
    # sane safety margins (5 min wall-clock, 256 MB memory, 10k recursion,
    # 10k API/tool calls per execution).
    code_mode_max_duration: Annotated[
        float,
        Setting(
            surface="advanced",
            section="beta_codemode",
            restart_required=True,
            range=(1.0, 300.0),
        ),
    ] = Field(30.0, alias="CODE_MODE_MAX_DURATION")
    code_mode_max_memory: Annotated[
        int,
        Setting(
            surface="advanced",
            section="beta_codemode",
            restart_required=True,
            range=(1_048_576, 268_435_456),
        ),
    ] = Field(10_485_760, alias="CODE_MODE_MAX_MEMORY")  # 10 MB default
    code_mode_max_recursion: Annotated[
        int,
        Setting(
            surface="advanced",
            section="beta_codemode",
            restart_required=True,
            range=(1, 10_000),
        ),
    ] = Field(100, alias="CODE_MODE_MAX_RECURSION")
    code_mode_max_invocations: Annotated[
        int,
        Setting(
            surface="advanced",
            section="beta_codemode",
            restart_required=True,
            range=(1, 10_000),
        ),
    ] = Field(100, alias="CODE_MODE_MAX_INVOCATIONS")
    # Path to a JSON file for persisting saved custom tools across restarts.
    # Empty string disables persistence (saved tools live in process memory
    # and are lost on restart). The addon sets this to /data/saved_tools.json
    # by default so saved tools survive addon restarts (the /data directory
    # is mapped per-addon by Supervisor and is preserved across addon
    # updates).
    code_mode_saved_tools_path: Annotated[
        str,
        Setting(surface="advanced", section="beta_codemode", restart_required=True),
    ] = Field("", alias="CODE_MODE_SAVED_TOOLS_PATH")

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
    # An advanced setting (section ``beta_yamlkeys``), not a feature flag,
    # because it is a value rather than a toggle – the same placement the
    # code-mode sub-settings use. Meaningful only when
    # ``enable_yaml_config_editing`` is on; the UI nests it under that parent
    # like the per-key toggles.
    extra_yaml_write_keys: Annotated[
        str, Setting(surface="advanced", section="beta_yamlkeys")
    ] = Field("", alias="HA_MCP_EXTRA_YAML_KEYS")

    # Developer mode (issue #1775) — registers the hidden ha_dev_* tools
    # (server update/restart, direct settings editing). Deliberately NOT a
    # beta flag: it is a development aid, not a feature preview, and must
    # not ride the beta master gate. The toggle renders in its own
    # "Developer" section at the very bottom of the web UI's Server
    # Settings tab; it is intentionally not an app option, so it stays out
    # of the add-on Configuration page. Dev-mode tools register at
    # startup, so toggling needs a restart.
    enable_dev_mode: Annotated[
        bool,
        Setting(surface="advanced", section="developer", restart_required=True),
    ] = Field(False, alias="HAMCP_ENABLE_DEV_MODE")

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
    # either direction, and it is not an app option, like
    # enable_dev_mode. A leash on those surfaces, NOT a sandbox: dev
    # mode's update_source/restart can still replace the running server
    # build, and in add-on mode ha_manage_app can reach the add-on's
    # own options and ingress — gate those tools with policy rules (or
    # keep dev mode off) where that boundary matters.
    dev_tools_security_policy_access: Annotated[
        bool, Setting(surface="advanced", section="developer")
    ] = Field(False, alias="HAMCP_DEV_SECURITY_POLICY_ACCESS")

    # ===== Feature flags (web UI feature toggles) =====
    #
    # Precedence: explicit env var beats the override file, addon mode
    # (SUPERVISOR_TOKEN set) ignores the file entirely (start.py owns env
    # vars from config.yaml in that mode), and the field default is the
    # fallback.

    # Master beta-features toggle. In the dev app only, where it defaults
    # on so the user only has to opt into individual sub-tools. Consumed
    # by the master gate in ``_apply_feature_flag_overrides``, which
    # force-sets the ``beta`` sub-flags to False whenever this master is
    # off. Dev addon ``start.py`` auto-writes ``ENABLE_BETA_FEATURES=true``
    # whenever any beta sub-flag key is present in ``/data/options.json``
    # but this key is not, so the dev-addon UX is unchanged.
    enable_beta_features: Annotated[
        bool,
        Setting(surface="feature", app=AppOption(flavors=("dev",), dev_default=True)),
    ] = Field(False, alias="ENABLE_BETA_FEATURES")

    # Tool search transform — replaces the full tool catalog with a unified
    # BM25 search tool and categorized call proxies (read/write/delete).
    # Dramatically reduces idle context token usage for LLMs.
    enable_tool_search: Annotated[
        bool, Setting(surface="feature", app=_ALL_FLAVORS)
    ] = Field(False, alias="ENABLE_TOOL_SEARCH")

    # Max results returned by ha_search_tools.
    tool_search_max_results: Annotated[
        int, Setting(surface="feature", app=_ALL_FLAVORS, range=(2, 10))
    ] = Field(5, alias="TOOL_SEARCH_MAX_RESULTS")

    # Tool security policies middleware — opt-in gate that routes high-stakes
    # tool calls through a per-tool policy with out-of-band web-UI approval
    # (issue #966). Disabled by default.
    enable_tool_security_policies: Annotated[
        bool, Setting(surface="feature", app=_ALL_FLAVORS)
    ] = Field(False, alias="ENABLE_TOOL_SECURITY_POLICIES")

    # Security-policy editing tool (issue #2148) — registers
    # ha_manage_security_policy, an ordinary MCP tool that reads and rewrites
    # the same tool_policy.json the Tool Security Policies tab edits. Disabled
    # by default, and deliberately NOT a beta flag: it is a standing safety
    # decision, not a preview feature, so the beta master must not force it
    # off. Its toggle renders on the Policies tab, next to the master switch
    # above. It carries no ``features.*`` locale keys either: that is what
    # keeps it out of the generated FEATURE_META and therefore out of the
    # generic Server Settings feature list, while /api/settings/features
    # still serves and persists it for the Policies-tab toggle. It is an app
    # option because that toggle routes its save through Supervisor in
    # add-on mode, which rejects any option the schema does not declare.
    enable_security_policy_tool: Annotated[
        bool, Setting(surface="feature", app=_ALL_FLAVORS)
    ] = Field(False, alias="ENABLE_SECURITY_POLICY_TOOL")

    # Read Only Mode — global safety toggle (discussion #1569). When on,
    # write-capable tools are hidden from the MCP catalog and every write
    # operation is blocked at call time with a structured READ_ONLY_MODE
    # error. Mixed read/write tools whose read surface has no pure-read
    # duplicate stay available with their write actions blocked (see
    # read_only.py:READ_ONLY_EXEMPT_TOOLS). Off by default.
    read_only_mode: Annotated[bool, Setting(surface="feature", app=_ALL_FLAVORS)] = (
        Field(False, alias="READ_ONLY_MODE")
    )

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
    redact_secrets: Annotated[bool, Setting(surface="feature", app=_ALL_FLAVORS)] = (
        Field(False, alias="REDACT_SECRETS")
    )

    # Mandatory best-practice skills — server-side master switch for the
    # write-tool skill_content delivery feature (issue #1182). When True
    # (default), the six write tools (automations / scripts / scenes /
    # helpers / dashboards / yaml) attach the canonical best-practice
    # reference files under ``skill_content`` on every successful write,
    # plus auto-embed any sections cited by best-practice warnings. The
    # per-call ``MandatoryBPS`` parameter on each tool controls whether
    # the canonical files ship for that one call. This setting is the
    # master gate above that — when False, NO skill_content goes out
    # regardless of the per-call param or BP warnings. Default on, and not
    # a beta flag: the beta master must not gate it.
    enable_mandatory_bps: Annotated[
        bool, Setting(surface="feature", app=_ALL_FLAVORS)
    ] = Field(True, alias="ENABLE_MANDATORY_BPS")

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
    enable_strict_mandatory_bps: Annotated[
        bool, Setting(surface="feature", app=_ALL_FLAVORS)
    ] = Field(True, alias="ENABLE_STRICT_MANDATORY_BPS")

    # Managed YAML config editing — allows ha_config_set_yaml to add,
    # replace, or remove top-level keys in configuration.yaml and package
    # files. Disabled by default; only for YAML-only features with no UI/API path.
    enable_yaml_config_editing: Annotated[
        bool, Setting(surface="feature", beta=True, app=_DEV_FLAVOR)
    ] = Field(False, alias="ENABLE_YAML_CONFIG_EDITING")

    # Two-step confirmation for ha_config_set_yaml (#1720). When on (the
    # default), the first edit call returns a unified diff preview plus a
    # confirm token and writes NOTHING; the edit lands only when repeated
    # with that token. Sub-toggle of enable_yaml_config_editing (nested
    # beneath it in the UI). Default ON deliberately: the diff preview is
    # what lets the calling agent catch collateral changes before they
    # reach disk. A beta flag purely for the addon-mode override path; the
    # master-off cascade forcing it False is moot because the yaml tool
    # itself is unregistered then.
    enable_yaml_edit_confirm: Annotated[
        bool, Setting(surface="feature", beta=True, app=_DEV_FLAVOR)
    ] = Field(True, alias="ENABLE_YAML_EDIT_CONFIRM")

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
    # the parent is off. They are beta flags so they follow the same master
    # gate and addon-mode override path as the other beta flags — that is
    # what makes the web-UI toggle take effect on the stable add-on, where
    # they are not app options.
    enable_yaml_packages_automation: Annotated[
        bool, Setting(surface="feature", beta=True, app=_DEV_FLAVOR)
    ] = Field(False, alias="ENABLE_YAML_PACKAGES_AUTOMATION")
    enable_yaml_packages_script: Annotated[
        bool, Setting(surface="feature", beta=True, app=_DEV_FLAVOR)
    ] = Field(False, alias="ENABLE_YAML_PACKAGES_SCRIPT")
    enable_yaml_packages_scene: Annotated[
        bool, Setting(surface="feature", beta=True, app=_DEV_FLAVOR)
    ] = Field(False, alias="ENABLE_YAML_PACKAGES_SCENE")

    # Lite docstrings — replace selected heavy tool descriptions with
    # shorter variants that defer detailed guidance to the
    # ``ha_get_skill_guide`` skill tool/resource.
    # Reduces idle catalog token usage at the cost of relying on the LLM
    # to actually consult the skill when it needs detail. Beta feature
    # (issue #1062); a startup WARNING is emitted when enabled so
    # env-var users see the trade-off in their logs.
    enable_lite_docstrings: Annotated[
        bool, Setting(surface="feature", beta=True, app=_DEV_FLAVOR)
    ] = Field(False, alias="ENABLE_LITE_DOCSTRINGS")

    # Filesystem tools — read/write/delete/list under the HA config dir.
    # Previously gated by a direct ``os.getenv`` call in
    # ``tools/tools_filesystem.py`` so callers (and the settings UI)
    # couldn't see it through ``Settings``. Promoted to a first-class
    # Settings field so the same precedence path applies as for every
    # other gated capability.
    enable_filesystem_tools: Annotated[
        bool, Setting(surface="feature", beta=True, app=_DEV_FLAVOR)
    ] = Field(False, alias="HAMCP_ENABLE_FILESYSTEM_TOOLS")

    # Code Mode — sandboxed Python execution via pydantic-monty.
    # Provides an "escape hatch" tool (ha_manage_custom_tool) that lets LLMs write
    # custom one-off Python code when no existing tool covers the request.
    # Disabled by default due to the inherent risk of LLM-generated code.
    enable_code_mode: Annotated[
        bool, Setting(surface="feature", beta=True, app=_DEV_FLAVOR)
    ] = Field(False, alias="ENABLE_CODE_MODE")

    # Dashboard screenshot mode — the ``ha_get_dashboard_screenshot`` tool
    # plus the ``include_screenshot`` / ``return_screenshot`` params on the
    # dashboard get/set tools. Renders responsive Lovelace images via a
    # separate, opt-in headless-Chromium screenshot add-on (balloob's Puppet
    # add-on, or a docker-compose sidecar). Off by default; nothing heavy is
    # pulled unless the user enables it AND installs the engine.
    enable_dashboard_screenshot: Annotated[
        bool, Setting(surface="feature", beta=True, app=_DEV_FLAVOR)
    ] = Field(False, alias="HAMCP_ENABLE_DASHBOARD_SCREENSHOT")

    # ===== Auto-backup settings (web UI Backups tab, #1288) =====

    # Auto-backup of edited entities (#1288).
    # Captures the pre-write state of every wrapped write/destructive tool
    # to a local directory. Enabled by default — captures are best-effort
    # (failures log a WARNING but never block the wrapped write) and the
    # disk footprint is small (typically <10 KB per snapshot; default
    # retention is 100/entity, see ``auto_backup_retain_per_entity``).
    # Set ``ENABLE_AUTO_BACKUP=false`` to opt out.
    enable_auto_backup: Annotated[bool, Setting(surface="backup", app=_ALL_FLAVORS)] = (
        Field(True, alias="ENABLE_AUTO_BACKUP")
    )

    # Per-entity throttle window. 0 (default) = backup every write; N>0 =
    # at most one snapshot per N minutes per entity. Upper bound 1440
    # (one day) prevents accidental indefinite throttling via typo.
    auto_backup_throttle_minutes: Annotated[
        int, Setting(surface="backup", app=_ALL_FLAVORS, range=(0, 1440))
    ] = Field(0, alias="AUTO_BACKUP_THROTTLE_MINUTES")

    # Max snapshots kept per entity. Older snapshots beyond this cap
    # are rotated out on each successful capture.
    auto_backup_retain_per_entity: Annotated[
        int, Setting(surface="backup", app=_ALL_FLAVORS, range=(1, 10_000))
    ] = Field(100, alias="AUTO_BACKUP_RETAIN_PER_ENTITY")

    # Backup directory override. Empty ("") resolves at runtime to a
    # deployment-mode default: ``/data/ha_mcp_backups`` in the add-on,
    # otherwise ``<data dir>/backups`` (see ``backup_manager._resolve_default_dir``).
    auto_backup_dir: Annotated[str, Setting(surface="backup")] = Field(
        "", alias="HAMCP_BACKUP_DIR"
    )

    # Calendar event backups query an ahead-of-now window to locate the
    # event by uid. Default 7 days catches typical edits; widen for
    # far-future events.
    auto_backup_calendar_lookahead_days: Annotated[
        int, Setting(surface="backup", range=(1, 365))
    ] = Field(7, alias="HAMCP_AUTO_BACKUP_CALENDAR_LOOKAHEAD_DAYS")

    # Snapshot-tarball deletion gate (#1861). Off by default: an agent
    # deleting a full HA snapshot is categorically riskier than the
    # lightweight `edits`-scope auto-backups (which already delete freely),
    # since a snapshot may be the last recovery point after the agent
    # itself broke something. A human must opt in via env var, the web
    # settings UI override file, or (in the add-on) the Supervisor options
    # — never something the agent can flip on itself.
    enable_snapshot_delete: Annotated[
        bool, Setting(surface="backup", app=_ALL_FLAVORS)
    ] = Field(False, alias="ENABLE_SNAPSHOT_DELETE")

    # Minimum age (days) a snapshot must have before it's deletable. This is
    # the load-bearing guard, not `enable_snapshot_delete`: a count-based
    # "keep the last N" rule is defeatable by an agent flooding new
    # snapshots before deleting old ones, but it cannot forge a backup's
    # HA-stamped creation date. 0 disables the age floor (still gated by
    # enable_snapshot_delete + the newest-snapshot / automatic-backup
    # guards enforced in tools/backup.py).
    snapshot_delete_min_age_days: Annotated[
        int, Setting(surface="backup", app=_ALL_FLAVORS, range=(0, 365))
    ] = Field(7, alias="SNAPSHOT_DELETE_MIN_AGE_DAYS")

    # ===== Other settings =====

    # Seed values for tool visibility (comma-separated tool names).
    # Used as initial config when no tool_config.json exists.
    # The web settings UI (/settings) is the primary interface for managing these.
    disabled_tools: Annotated[str, Setting(app=_ALL_FLAVORS)] = Field(
        "", alias="DISABLED_TOOLS"
    )
    pinned_tools: Annotated[str, Setting(app=_ALL_FLAVORS)] = Field(
        "", alias="PINNED_TOOLS"
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

    @field_validator("*", mode="before")
    @classmethod
    def _lenient_value(cls, v: object, info: ValidationInfo) -> object:
        """Parse a ``lenient`` field, falling back to its default with a
        warning instead of failing startup.

        Empty or missing gives the default silently. A value that does not
        parse, is outside the range (and is not the off value) or is not
        one of the choices gives the default with a warning. Whole-number
        fields also reject fractions rather than truncating them.
        """
        field_name = info.field_name
        if field_name is None:  # always set for field_validator; defensive
            return v
        field = cls.model_fields[field_name]
        setting = setting_of(field)
        if setting is None or not setting.lenient:
            return v
        default = field.default
        if v is None or (isinstance(v, str) and not v.strip()):
            return default
        value, problem = _parse_lenient(v, setting, whole=field.annotation is int)
        if problem is None:
            return value
        logger.warning(
            "Invalid value for %s=%r (%s); using default %s",
            field_name,
            v,
            problem,
            default,
        )
        return default

    @field_validator("*")
    @classmethod
    def _within_range(cls, v: object, info: ValidationInfo) -> object:
        """Reject a value outside the field's range, unless it is the off value."""
        if info.field_name is None:
            return v
        setting = setting_of(cls.model_fields[info.field_name])
        if setting is None or setting.range is None or v == setting.off_value:
            return v
        lo, hi = setting.range
        if not lo <= v <= hi:  # type: ignore[operator]
            raise ValueError(f"must be between {lo:g} and {hi:g}")
        return v

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

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Accept a log level in any case."""
        return _one_of(cls, "log_level", v.upper())

    @field_validator("backup_hint")
    @classmethod
    def validate_backup_hint(cls, v: str) -> str:
        """Accept a backup hint in any case."""
        return _one_of(cls, "backup_hint", v.lower())

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
