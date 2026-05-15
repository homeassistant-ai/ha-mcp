"""
Core Smart MCP Server implementation.

Implements lazy initialization pattern for improved startup time:
- Settings and FastMCP server are created immediately (fast)
- Smart tools and device tools are created lazily on first access
- Tool modules are discovered at startup but imported on first use
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, cast

import yaml  # type: ignore[import-untyped]
from fastmcp import FastMCP
from mcp.types import Icon
from pydantic import Field

from .config import _PACKAGE_VERSION, get_global_settings
from .errors import ErrorCode, create_error_response
from .tools.enhanced import EnhancedToolsMixin
from .tools.helpers import raise_tool_error
from .tools.util_helpers import strip_internal_fields
from .transforms import DEFAULT_PINNED_TOOLS

if TYPE_CHECKING:
    from .client.rest_client import HomeAssistantClient
    from .tools.registry import ToolsRegistry

logger = logging.getLogger(__name__)

# Name of the consolidated polymorphic skill tool. Defined as a module
# constant so settings UI, instructions, tests, and pinning all agree on
# one canonical string. 18 chars — well under the 40-char cap that
# Cloudflare's MCP portal enforces (#1121).
SKILL_TOOL_NAME = "ha_get_skill_guide"

# Names this tool replaced in #1134. Appended to the catalog description
# so agents trained on the prior catalog (or pasting old instructions)
# see the redirect inside the tool itself, not just via BM25 keyword
# enrichment.
_OLD_SKILL_TOOL_ALIASES = (
    "Replaces (and supersedes) the prior tools: ha_list_resources, "
    "ha_read_resource, and ha_get_skill_home_assistant_best_practices. "
    "If you were going to call any of those, call this instead."
)


# Server icon configuration using GitHub-hosted images
# These icons are bundled in packaging/mcpb/ and also available via GitHub raw URLs
SERVER_ICONS = [
    Icon(
        src="https://raw.githubusercontent.com/homeassistant-ai/ha-mcp/master/packaging/mcpb/icon.svg",
        mimeType="image/svg+xml",
    ),
    Icon(
        src="https://raw.githubusercontent.com/homeassistant-ai/ha-mcp/master/packaging/mcpb/icon-128.png",
        mimeType="image/png",
        sizes=["128x128"],
    ),
]


class HomeAssistantSmartMCPServer(EnhancedToolsMixin):
    """Home Assistant MCP Server with smart tools and fuzzy search.

    Uses lazy initialization to improve startup time:
    - Client, smart_tools, device_tools are created on first access
    - Tool modules are discovered at startup but imported when first called
    """

    def __init__(
        self,
        client: HomeAssistantClient | None = None,
        server_name: str = "ha-mcp",
        server_version: str = _PACKAGE_VERSION,
    ):
        """Initialize the smart MCP server with lazy loading support."""
        # Load settings first (fast operation)
        self.settings = get_global_settings()

        # Store provided client or mark for lazy creation
        self._client: HomeAssistantClient | None = client
        self._client_provided = client is not None

        # Lazy initialization placeholders
        self._smart_tools: Any = None
        self._device_tools: Any = None
        self._tools_registry: ToolsRegistry | None = None
        # Populated by _apply_settings_visibility from tool_config.json on startup
        self._user_pinned_tools: list[str] = []

        # Get server name/version from settings if no client provided
        if not self._client_provided:
            server_name = self.settings.mcp_server_name
            server_version = self.settings.mcp_server_version

        # Build server instructions from bundled skills (if enabled)
        instructions = self._build_skills_instructions()

        # Create FastMCP server with Home Assistant icons for client UI display
        self.mcp = FastMCP(
            name=server_name,
            version=server_version,
            icons=SERVER_ICONS,
            instructions=instructions,
        )

        # Register all tools and expert prompts
        self._initialize_server()

    @property
    def client(self) -> HomeAssistantClient:
        """Lazily create and return the Home Assistant client."""
        if self._client is None:
            from .client.rest_client import HomeAssistantClient

            self._client = HomeAssistantClient()
            logger.debug("Lazily created HomeAssistantClient")
        return self._client

    @property
    def smart_tools(self) -> Any:
        """Lazily create and return the smart search tools."""
        if self._smart_tools is None:
            from .tools.smart_search import create_smart_search_tools

            self._smart_tools = create_smart_search_tools(self.client)
            logger.debug("Lazily created SmartSearchTools")
        return self._smart_tools

    @property
    def device_tools(self) -> Any:
        """Lazily create and return the device control tools."""
        if self._device_tools is None:
            from .tools.device_control import create_device_control_tools

            self._device_tools = create_device_control_tools(self.client)
            logger.debug("Lazily created DeviceControlTools")
        return self._device_tools

    @property
    def tools_registry(self) -> ToolsRegistry:
        """Lazily create and return the tools registry."""
        if self._tools_registry is None:
            from .tools.registry import ToolsRegistry

            self._tools_registry = ToolsRegistry(
                self, enabled_modules=self.settings.enabled_tool_modules
            )
            logger.debug("Lazily created ToolsRegistry")
        return self._tools_registry

    def _initialize_server(self) -> None:
        """Initialize all server components."""
        # Register tools
        self.tools_registry.register_all_tools()

        # Register enhanced tools for first/second interaction success
        self.register_enhanced_tools()

        # Register bundled skills as MCP resources
        self._register_skills()

        # Apply user-configured tool visibility (must come before keyword
        # enrichment / tool search so disabled tools are excluded from
        # search indexing too).
        self._apply_settings_visibility()

        # Replace heavy tool descriptions with lite variants when
        # ENABLE_LITE_DOCSTRINGS=true. Must come BEFORE keyword
        # enrichment so BM25 keywords append to the lite text (instead
        # of the full description we just discarded).
        self._apply_lite_docstrings()

        # Enrich tool descriptions with BM25 keyword boosts. Runs
        # unconditionally so Claude's native deferred-tool search
        # (claude.ai) benefits even when ENABLE_TOOL_SEARCH is off.
        # Must come before _apply_tool_search so CategorizedSearchTransform
        # indexes the enriched descriptions.
        self._apply_search_keyword_enrichment()

        # Apply tool search transform (must come after all tools and
        # the skill guide tool are registered so it can wrap everything)
        self._apply_tool_search()

    def _get_skills_dir(self) -> Path | None:
        """Return the bundled skills directory if it exists.

        Skills are vendored via a git submodule at resources/skills-vendor/.
        The actual skill directories live under the skills/ subdirectory
        within that repo.
        """
        skills_dir = Path(__file__).parent / "resources" / "skills-vendor" / "skills"
        return skills_dir if skills_dir.exists() else None

    def _build_skills_instructions(self) -> str | None:
        """Build server instructions from bundled skill frontmatter.

        Reads the description field from each SKILL.md's YAML frontmatter
        and includes it as-is in the server instructions. The description
        is authored for LLM consumption and should not be parsed or
        restructured by code.

        Returns None when no skills directory or no parseable skills are
        present, leaving instructions unchanged from the default (None).
        """
        skills_dir = self._get_skills_dir()
        if not skills_dir:
            return None

        try:
            entries = sorted(skills_dir.iterdir())
        except OSError as e:
            logger.warning("Could not read skills directory %s: %s", skills_dir, e)
            return None

        skill_blocks: list[str] = []
        for skill_dir in entries:
            main_file = skill_dir / "SKILL.md"
            if not skill_dir.is_dir() or not main_file.exists():
                continue

            block = self._build_skill_block(skill_dir.name, main_file)
            if block:
                skill_blocks.append(block)

        if not skill_blocks:
            return None

        access_method = (
            "Read the skill via MCP resources (resources/read with the "
            "skill:// URI) — if you can read these instructions, you "
            "should be able to access resources as well. If for any "
            f"reason you cannot access MCP resources, use the {SKILL_TOOL_NAME} "
            "tool as a fallback. If you can access resources normally, do "
            "not waste time or tokens on that tool."
        )

        header = (
            "IMPORTANT: This server provides best-practice skills that MUST "
            "be consulted before performing matching actions. "
            "Read the SKILL.md for the matching skill "
            "\u2014 it contains a Reference Files table that maps tasks to "
            "specific reference files. You MUST read the referenced files "
            "that match your current task before proceeding. "
            "Do NOT load all reference files upfront "
            "\u2014 only the ones the table directs you to.\n\n"
            f"How to access: {access_method}\n"
        )

        instructions = header + "\n".join(skill_blocks)

        # Append tool search instructions when enabled
        if self.settings.enable_tool_search:
            instructions += (
                "\n\n## Tool Discovery\n"
                "This server uses search-based tool discovery. Most tools "
                "are NOT listed directly \u2014 use ha_search_tools to find them.\n\n"
                "WORKFLOW:\n"
                '1. Call ha_search_tools(query="...") to find relevant tools\n'
                "2. Results include name, description, parameters, and "
                "annotations (readOnlyHint/destructiveHint)\n"
                "3. Execute the discovered tool \u2014 two options:\n"
                "   a) DIRECT CALL (preferred): Call the tool directly by "
                "name. All discovered tools are callable without a proxy.\n"
                "   b) VIA PROXY: For permission-gated execution, use the "
                "matching proxy:\n"
                "      - ha_call_read_tool \u2014 safe, read-only operations\n"
                "      - ha_call_write_tool \u2014 creates or modifies data\n"
                "      - ha_call_delete_tool \u2014 removes data permanently\n\n"
                "Once you know a tool\u2019s name, you do NOT need to search "
                "again \u2014 call it directly.\n\n"
                f"A few critical tools are listed directly "
                f"({', '.join(DEFAULT_PINNED_TOOLS)}). Everything else must "
                f"be discovered via search.\n\n"
                "DO NOT assume a capability is unavailable because you "
                "don't see a direct tool for it. ALWAYS search first."
            )

        return instructions

    @staticmethod
    def _parse_skill_frontmatter(main_file: Path) -> dict | None:
        """Parse YAML frontmatter from a SKILL.md file.

        Returns the frontmatter dict if valid, or None with a logged
        warning for each failure case.
        """
        try:
            content = main_file.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("Could not read %s: %s", main_file, e)
            return None

        parts = content.split("---", 2)
        if len(parts) < 3:
            logger.warning("No valid frontmatter delimiters in %s", main_file)
            return None

        try:
            frontmatter = yaml.safe_load(parts[1])
        except yaml.YAMLError as e:
            # yaml.YAMLError exposes `.problem` and `.problem_mark` for
            # parse errors — both are the entire debugging payload for
            # an operator trying to fix the SKILL.md.
            logger.warning("Could not parse YAML frontmatter in %s: %s", main_file, e)
            return None

        if not isinstance(frontmatter, dict):
            logger.warning("Frontmatter is not a mapping in %s", main_file)
            return None

        description = frontmatter.get("description", "")
        if not description:
            logger.warning(
                "No description in frontmatter for %s", main_file.parent.name
            )
            return None
        if not isinstance(description, str):
            # Truthy non-string values (e.g. `description: [foo]` or
            # `description: 42`) would later crash on `.strip()` in the
            # callers. Fail closed here so malformed bundles only break
            # themselves, not the whole skill registration.
            logger.warning(
                "Description in frontmatter for %s is not a string (got %s); skipping",
                main_file.parent.name,
                type(description).__name__,
            )
            return None

        return frontmatter

    def _build_skill_block(self, skill_name: str, main_file: Path) -> str | None:
        """Build an instruction block for a single skill.

        Reads the description field from YAML frontmatter and includes it
        verbatim. The description is designed for LLM consumption and
        contains its own trigger conditions and symptom indicators.
        """
        frontmatter = self._parse_skill_frontmatter(main_file)
        if not frontmatter:
            return None

        description = frontmatter["description"]
        uri = f"skill://{skill_name}/SKILL.md"

        return f"\n### Skill: {skill_name} ({uri})\n{description.strip()}"

    def _apply_settings_visibility(self) -> None:
        """Apply persisted tool visibility from ``tool_config.json``.

        Reads the saved enable/disable/pin state and applies it to the
        FastMCP instance via ``apply_tool_visibility``. HTTP routes for
        the settings UI are registered separately by entry-point callers
        (start.py / main_web) so they can be mounted under the secret
        path; that keeps the routes inert in stdio mode and behind the
        same auth posture as the MCP endpoint in HTTP mode.
        """
        from .settings_ui import apply_tool_visibility, load_tool_config

        config = load_tool_config(self.settings)
        if config:
            pinned = apply_tool_visibility(self.mcp, config, self.settings)
            if pinned:
                self._user_pinned_tools = list(pinned)
            logger.info(
                "Applied persisted tool config (%d entries)",
                len(config.get("tools", {})),
            )

    # Tools pinned outside the search transform for individual permission gating.
    # These are always visible in list_tools() regardless of search transform.
    _PINNED_TOOLS: ClassVar[list[str]] = list(DEFAULT_PINNED_TOOLS)

    # Description for the unified search tool
    _SEARCH_TOOL_DESCRIPTION = (
        "Search ALL Home Assistant tools by keyword. Returns matching tools "
        "with descriptions, parameters, and annotations (read/write/delete). "
        "Categories: entities, states, automations, scripts, dashboards, "
        "helpers, HACS, calendar, zones, labels, groups, areas, floors, "
        "history, statistics, devices, integrations, services, backups, "
        "todo, camera, blueprints, system, and more.\n\n"
        "WORKFLOW:\n"
        "1. ha_search_tools(query='...') \u2014 find tools (this tool)\n"
        "2. Execute: call the tool DIRECTLY by name (preferred), or use "
        "a proxy for permission gating:\n"
        "   - ha_call_read_tool \u2014 readOnlyHint tools (safe, no side effects)\n"
        "   - ha_call_write_tool \u2014 destructiveHint tools that create/update\n"
        "   - ha_call_delete_tool \u2014 destructiveHint tools that remove/delete\n"
        "Once you know a tool name, call it directly \u2014 no need to search "
        "again.\n\n"
        "If using proxies, call with TWO top-level params:\n"
        '   ha_call_read_tool(name="ha_search_entities", arguments={"query": "..."})\n'
        "   Do NOT nest name/arguments inside the arguments param.\n"
        "   Call proxy tools SEQUENTIALLY, not in parallel.\n\n"
        "ALWAYS search before assuming a capability is unavailable. "
        "Most tools are discoverable only through this search."
    )

    # Extra keywords appended to tool descriptions for BM25 ranking.
    # Applied unconditionally via SearchKeywordsTransform so they also
    # improve retrieval for Claude's native deferred-tool search on
    # claude.ai, which indexes tool names and descriptions with BM25
    # (no semantic matching). Original tool docstrings stay unchanged;
    # these keywords are appended by the transform at list-tools time.
    _SEARCH_KEYWORDS: ClassVar[dict[str, str]] = {
        # s02: "find entities" → ha_search_entities should outrank ha_deep_search
        "ha_search_entities": (
            "find entities lookup discover search lights sensors switches "
            "covers climate fans media_player binary_sensor device_tracker "
            "person weather automation script helper input_boolean input_number"
        ),
        # s07: "get/read automation" → ha_config_get_automation should outrank set
        "ha_config_get_automation": (
            "read inspect fetch view existing automation config triggers "
            "conditions actions get show detail"
        ),
        # s09: "create helper" → ha_config_set_helper should outrank remove_helper
        # Covers all 27 helper types (12 simple + 15 flow-based, unified in #967).
        "ha_config_set_helper": (
            "create update new add helper "
            "input_boolean input_button input_number input_text input_datetime "
            "input_select counter timer schedule zone person tag "
            "template group utility_meter derivative min_max threshold "
            "integration statistics trend random filter tod "
            "generic_thermostat switch_as_x generic_hygrostat"
        ),
        # Boost tools that compete with ha_deep_search for common queries
        "ha_config_get_script": (
            "read inspect fetch view existing script config sequence "
            "actions get show detail"
        ),
        "ha_config_list_helpers": (
            "list all helpers input_boolean input_number input_text "
            "counter timer input_datetime input_select"
        ),
        "ha_get_entity": (
            "get entity state attributes details single specific entity_id"
        ),
        "ha_get_state": (
            "get current state value single entity check status bulk multiple states"
        ),
        "ha_config_set_automation": (
            "create update modify edit automation triggers conditions actions "
            "new automation write save"
        ),
        "ha_config_set_script": (
            "create update modify edit script sequence actions new script write save"
        ),
        "ha_config_set_yaml": (
            "edit yaml configuration.yaml packages template sensor "
            "binary_sensor command_line rest mqtt platform yaml-only "
            "config file modify add remove replace"
        ),
        "ha_manage_addon": (
            "manage addon add-on configure settings options port network boot "
            "watchdog auto_update supervisor ingress proxy websocket api rest "
            "esphome nodered node-red frigate mosquitto mqtt zigbee2mqtt zigbee "
            "z-wave zwave appdaemon hacs studio code server file editor terminal "
            "ssh samba grafana influxdb deconz motioneye compile validate upload "
            "deploy firmware ota flash yaml device logs flows events stats"
        ),
        # Old tool names from before #1134 consolidation. BM25 retrieval
        # on agents that still know the previous catalog ("call
        # ha_list_resources", "use ha_get_skill_home_assistant_best_practices")
        # routes them to the replacement instead of failing tool lookup.
        "ha_get_skill_guide": (
            "best practices skill skills guide guides reference references "
            "documentation docs help tutorial automation script scene helper "
            "dashboard "
            "ha_list_resources ha_read_resource list_resources read_resource "
            "ha_get_skill_home_assistant_best_practices "
            "ha_get_skill_home_assistant home_assistant_best_practices"
        ),
    }

    # Lite docstrings — beta opt-in (enable_lite_docstrings, #1062).
    # Each entry replaces the full docstring on a heavy tool with a
    # shorter variant that defers schema/example detail to
    # ha_get_skill_guide. Every entry preserves
    # a pointer to that skill so the LLM still has a path to the full
    # guidance from inside the trimmed description. The trade-off
    # (LLMs that skip the skill tool get less guidance) is surfaced in
    # the dev-addon toggle, docs/beta.md, and a startup WARNING.
    _LITE_DOCSTRINGS: ClassVar[dict[str, str]] = {
        "ha_config_get_automation": (
            "Get a Home Assistant automation configuration by "
            "entity_id or unique_id. Returns the full config "
            "(trigger, condition, action, mode) plus a stable "
            "config_hash for use with python_transform on "
            "ha_config_set_automation.\n\n"
            "For schema and field-level details, see "
            "ha_get_skill_guide."
        ),
        "ha_config_set_automation": (
            "Create or update a Home Assistant automation.\n\n"
            "Supports two modes: full `config` replacement, or surgical "
            "`python_transform` on an existing automation (requires "
            "`identifier` and `config_hash` from "
            "ha_config_get_automation). Omit `identifier` to create a "
            "new automation.\n\n"
            "For schema details, examples, and native-vs-template "
            "guidance, see ha_get_skill_guide."
        ),
        "ha_config_get_script": (
            "Get a Home Assistant script configuration by "
            "script_id or entity_id. Returns the full config (sequence, "
            "mode, fields) plus a stable config_hash for use with "
            "python_transform on ha_config_set_script.\n\n"
            "For schema details, see "
            "ha_get_skill_guide."
        ),
        "ha_config_set_script": (
            "Create or update a Home Assistant script.\n\n"
            "Supports two modes: full `config` replacement, or surgical "
            "`python_transform` on an existing script (requires "
            "`identifier` and `config_hash` from "
            "ha_config_get_script). Omit `identifier` to create a new "
            "script.\n\n"
            "For schema details and examples, see "
            "ha_get_skill_guide."
        ),
        "ha_config_get_scene": (
            "Get a Home Assistant scene configuration by "
            "scene_id or entity_id. Returns the full config plus a "
            "stable config_hash for use with python_transform on "
            "ha_config_set_scene.\n\n"
            "For schema details, see "
            "ha_get_skill_guide."
        ),
        "ha_config_set_scene": (
            "Create or update a Home Assistant scene.\n\n"
            "Supports two modes: full `config` replacement, or surgical "
            "`python_transform` on an existing scene (requires "
            "`identifier` and `config_hash`).\n\n"
            "For schema details and examples, see "
            "ha_get_skill_guide."
        ),
        "ha_config_list_helpers": (
            "List Home Assistant helpers of a given simple type. "
            "Accepts the 12 storage-backed helper types only: "
            "input_button, input_boolean, input_select, input_number, "
            "input_text, input_datetime, counter, timer, schedule, "
            "zone, person, tag. Flow-based helpers (template, group, "
            "utility_meter, derivative, statistics, trend, threshold, "
            "filter, switch_as_x, etc.) cannot be listed through this "
            "tool — use ha_search_entities or ha_deep_search.\n\n"
            "For per-type schemas, see ha_get_helper_schema and "
            "ha_get_skill_guide."
        ),
        "ha_config_set_helper": (
            "Create or update a Home Assistant helper. Supports all "
            "supported helper types: the simple types (input_*, "
            "counter, timer, schedule, zone, person, tag) and the "
            "flow-based types (template, group, utility_meter, "
            "derivative, statistics, trend, threshold, filter, "
            "switch_as_x, and others).\n\n"
            "For per-type config schemas, call "
            "ha_get_helper_schema(helper_type) first. For decision "
            "matrix and worked examples (which helper type for which "
            "use case), see ha_get_skill_guide."
        ),
        "ha_config_get_dashboard": (
            "Get Home Assistant dashboard info (list mode, search "
            "mode, or full config).\n\n"
            "Three modes: (1) list — `list_only=True` returns all "
            "storage-mode dashboards with metadata. (2) search — pass "
            "any of `entity_id`, `card_type`, `heading` to find cards "
            "(and their `jq_path`) inside a specific dashboard; the "
            "result includes a `config_hash` you can pair with "
            "ha_config_set_dashboard(python_transform=...) to edit "
            "matched cards surgically. (3) get — no search params "
            "returns the full Lovelace config plus a stable "
            "`config_hash`. Use `url_path='default'` for the main "
            "dashboard.\n\n"
            "For card-type taxonomy and search workflow examples, see "
            "ha_get_skill_guide."
        ),
        "ha_config_set_dashboard": (
            "Create or update a Home Assistant dashboard.\n\n"
            "Supports two modes: full `config` replacement (new "
            "dashboards or full restructures), or surgical "
            "`python_transform` on an existing dashboard (requires "
            "`config_hash` from ha_config_get_dashboard; recommended "
            "for edits). Use `url_path` of 'default' or 'lovelace' "
            "to target the built-in dashboard.\n\n"
            "For card types, layout patterns, and python_transform "
            "security rules, see "
            "ha_get_skill_guide."
        ),
        "ha_call_service": (
            "Execute a Home Assistant service to control entities or "
            "trigger automations. Calls `<domain>.<service>` "
            "(e.g., light.turn_on, climate.set_temperature). Use "
            "ha_search_entities to find entity IDs and ha_get_state "
            "to read current values before changing them.\n\n"
            "For service-parameter details and per-domain guidance, "
            "see ha_get_skill_guide."
        ),
        "ha_config_set_yaml": (
            "Update raw YAML in configuration.yaml or packages/*.yaml "
            "via add / replace / remove on a single top-level key "
            "(LAST RESORT).\n\n"
            "Dedicated tools (ha_config_set_automation, "
            "ha_config_set_script, ha_config_set_scene, "
            "ha_config_set_helper) cover almost every use case and "
            "should be preferred. Use this only for YAML-only "
            "integrations (command_line, rest, shell_command, notify) "
            "or registering YAML-mode dashboards via "
            "`lovelace.dashboards.<url_path>`. Most edits require a "
            "full HA restart; template, mqtt, and group support "
            "reload.\n\n"
            "For routing guidance and the full allowlist, see "
            "ha_get_skill_guide."
        ),
    }

    # Description overrides that REPLACE the original description for BM25.
    # Used to narrow overly broad tools so they stop matching generic queries
    # against ha-mcp's internal BM25 search tool. Only applied when
    # enable_tool_search=True, because they are tuned specifically for the
    # categorized search transform and replacing the base description would
    # unnecessarily trim context for other clients.
    _SEARCH_DESCRIPTION_OVERRIDES: ClassVar[dict[str, str]] = {
        "ha_deep_search": (
            "Search INSIDE automation, script, and helper YAML configurations. "
            "Use ONLY when you need to find where a specific service call, "
            "entity reference, or config field appears within existing "
            "automation/script/helper definitions. "
            "NOT for finding entities or discovering tools."
        ),
    }

    def _apply_lite_docstrings(self) -> None:
        """Swap heavy tool descriptions for shorter variants if enabled.

        Beta feature gated on ``settings.enable_lite_docstrings`` /
        ``ENABLE_LITE_DOCSTRINGS=true``. Replaces the description on
        each tool listed in ``_LITE_DOCSTRINGS`` with a shorter variant
        that defers detail to
        ``ha_get_skill_guide``. Tools not in the
        mapping pass through unchanged.

        Emits a startup WARNING when enabled so non-addon users (Docker,
        uvx, pip) see the trade-off in their logs — the addon UI surfaces
        the same warning via the toggle description. A second WARNING is
        emitted if the transform install fails, so users don't silently
        get full descriptions back after explicitly enabling the toggle.

        Runs before ``_apply_search_keyword_enrichment`` so BM25 keywords
        append to the lite text instead of the discarded full description.
        """
        if not self.settings.enable_lite_docstrings:
            return

        logger.warning(
            "ENABLE_LITE_DOCSTRINGS=true: replacing %d tool descriptions "
            "with shorter variants. This reduces idle catalog token usage "
            "but may degrade LLM performance — the trimmed descriptions "
            "rely on the LLM calling ha_get_skill_guide "
            "(or reading skill:// resources) for detail, which is not "
            "guaranteed. See docs/beta.md.",
            len(self._LITE_DOCSTRINGS),
        )

        try:
            from .transforms import LiteDocstringsTransform
        except ImportError:
            logger.exception(
                "LiteDocstringsTransform not importable — please file a "
                "bug. ENABLE_LITE_DOCSTRINGS=true is in effect but full "
                "tool descriptions will be exposed."
            )
            return

        try:
            self.mcp.add_transform(
                LiteDocstringsTransform(replacements=self._LITE_DOCSTRINGS)
            )
        except Exception:
            logger.exception("Failed to apply LiteDocstringsTransform")
            logger.warning(
                "ENABLE_LITE_DOCSTRINGS=true was set but the transform "
                "failed to install — full tool descriptions remain in "
                "effect. Catalog token usage will be unchanged from the "
                "default."
            )

    def _apply_search_keyword_enrichment(self) -> None:
        """Append BM25 keyword boosts to tool descriptions.

        Applied unconditionally so Claude's native deferred-tool search
        (claude.ai uses BM25 over tool names and descriptions) can find
        ha-mcp tools for common natural-language queries like "create
        automation" — the scenario in #940. The original tool docstrings
        in ``src/ha_mcp/tools/`` are unchanged; keywords are appended at
        list-tools time via ``SearchKeywordsTransform``.

        Description overrides (``_SEARCH_DESCRIPTION_OVERRIDES``) are only
        applied when ``enable_tool_search`` is also set, because they
        REPLACE the original description and are tuned specifically for
        ha-mcp's internal BM25 search tool.

        Runs before ``_apply_tool_search`` so downstream transforms
        index the enriched descriptions.
        """
        try:
            from .transforms import SearchKeywordsTransform
        except ImportError:
            logger.warning(
                "SearchKeywordsTransform not available; skipping description "
                "enrichment (tool discoverability on claude.ai may be degraded)."
            )
            return

        overrides = (
            self._SEARCH_DESCRIPTION_OVERRIDES
            if self.settings.enable_tool_search
            else None
        )
        try:
            self.mcp.add_transform(
                SearchKeywordsTransform(
                    keywords=self._SEARCH_KEYWORDS,
                    overrides=overrides,
                )
            )
            logger.info(
                "Search keyword enrichment applied (%d boosts%s)",
                len(self._SEARCH_KEYWORDS),
                f", {len(overrides)} overrides" if overrides else "",
            )
        except Exception:
            logger.exception("Failed to apply SearchKeywordsTransform")

    def _apply_tool_search(self) -> None:
        """Apply the CategorizedSearchTransform if enabled.

        Replaces the full tool catalog with a unified BM25 search tool and
        three categorized call proxies (read/write/delete). Pinned tools
        remain directly visible in list_tools() for individual permission
        gating. The polymorphic ``ha_get_skill_guide`` tool is pinned via
        ``DEFAULT_PINNED_TOOLS`` (transforms/categorized_search.py) so the
        bundled skill trigger-conditions stay in the catalog — no explicit
        ``pinned.append(...)`` for it here.

        Note: ``_apply_search_keyword_enrichment`` already ran before this
        method and installed ``SearchKeywordsTransform`` — the enriched
        catalog is what the categorized transform indexes.
        """
        if not self.settings.enable_tool_search:
            return

        try:
            from .transforms import CategorizedSearchTransform
        except ImportError:
            logger.error(
                "CategorizedSearchTransform not available but ENABLE_TOOL_SEARCH=true — "
                "full tool catalog will be exposed. Install fastmcp>=3.1 to fix."
            )
            return

        # Build the always_visible list: defaults + user-configured pins.
        # The skill guide tool is part of DEFAULT_PINNED_TOOLS and is
        # also in MANDATORY_TOOLS (settings UI strips it from any
        # disable list before applying), so the catalog presence is
        # protected from both the search transform and user disables.
        pinned = list(self._PINNED_TOOLS)
        pinned.extend(self._user_pinned_tools)

        # Pin code mode tool so it gets individual permission gating
        # rather than being hidden behind the BM25 search proxy.
        if self.settings.enable_code_mode:
            pinned.append("ha_manage_custom_tool")

        # The client may not support resources or server instructions — add
        # skills hint to the search tool description (the one place the LLM
        # is guaranteed to see).
        description = self._SEARCH_TOOL_DESCRIPTION + (
            "\n\nThis server also provides best-practice skills via "
            "skill:// resources. If your client supports MCP resources, "
            f"prefer reading them directly. Otherwise, call "
            f"{SKILL_TOOL_NAME} (directly, no proxy needed) to access the "
            "relevant SKILL.md before creating automations or configuring "
            "devices."
        )

        try:
            self.mcp.add_transform(
                CategorizedSearchTransform(
                    max_results=self.settings.tool_search_max_results,
                    always_visible=pinned,
                    search_tool_description=description,
                    # Pinned tools must be excluded from the proxy's
                    # category sets when code mode is on; otherwise sandbox
                    # code can launder a recursive ``ha_manage_custom_tool``
                    # invocation through ``ha_call_write_tool``. See the
                    # docstring on ``_rebuild_category_cache``.
                    enable_code_mode=self.settings.enable_code_mode,
                )
            )
            logger.info(
                "Tool search transform applied (%d pinned tools, max_results=%d, code_mode=%s)",
                len(pinned),
                self.settings.tool_search_max_results,
                self.settings.enable_code_mode,
            )
        except Exception:
            logger.exception("Failed to apply tool search transform")

    # Shared action-phrased keyword block for retrieval. Some MCP clients
    # (Claude Code, others) rank candidate tools by token-overlap between
    # the user's natural-language query and each tool's `description`
    # field; symptom-framed SKILL.md descriptions don't overlap with
    # task-phrased queries like "create automation" or "writing trigger".
    # This block lists the workflow positions where consulting the
    # bundled skill matters, so retrieval surfaces ha_get_skill_guide
    # when an agent is about to write config.
    _SKILL_USE_BEFORE_KEYWORDS: ClassVar[str] = (
        "Use BEFORE: creating or editing automations, scripts, scenes, "
        "helpers, or dashboards; writing triggers, conditions, actions, "
        "wait_template, or service calls; renaming entities or migrating "
        "device_id to entity_id; calling ha_config_set_automation, "
        "ha_config_set_script, ha_config_set_helper, ha_config_set_dashboard, "
        "or ha_set_entity."
    )

    def _register_skills(self) -> None:
        """Register bundled skills as MCP resources and a polymorphic tool.

        Two paths to the same content:

        - **Resources** — ``SkillsDirectoryProvider`` serves every skill
          file at ``skill://<skill>/<path>``. Resource-capable clients
          (Claude Code, Cursor, anything that supports the MCP
          ``resources/list`` / ``resources/read`` methods) discover and
          read skills natively. Best-effort: skipped if the provider
          can't be loaded or the skills dir is missing.
        - **Tool** — ``ha_get_skill_guide`` is a single polymorphic tool
          for tool-only clients (claude.ai, etc. that don't read server
          instructions). Three tiers: no args lists skills with their
          frontmatter descriptions; ``skill`` arg lists reference files;
          ``skill`` + ``file`` reads file content. **Registration is
          always attempted** so an absent tool isn't a silent failure
          mode — even if the skills submodule is missing, the tool
          surfaces that fact at call time via an explanatory empty
          listing with ``degraded: True``. A genuine registration
          failure (FastMCP API regression, etc.) is caught at the call
          site, logged with full traceback, and flips
          ``status["tool"] = "failed"`` so the summary log warns
          rather than aborting server startup.
        """
        status: dict[str, str | int] = {
            "provider": "skipped",
            "tool": "skipped",
            "guidance_count": 0,
        }

        skills_dir = self._get_skills_dir()

        # Phase 1+2: Best-effort MCP resource registration. The skill
        # tool stands on its own (just reads disk in the handler), so
        # provider failure is logged but doesn't block tool registration.
        if skills_dir is None:
            logger.warning(
                "Skills directory not found at %s; skill resources unavailable. "
                "%s will still be registered and report an empty listing.",
                Path(__file__).parent / "resources" / "skills-vendor" / "skills",
                SKILL_TOOL_NAME,
            )
        else:
            try:
                from fastmcp.server.providers.skills import SkillsDirectoryProvider
            except ImportError:
                logger.warning(
                    "SkillsDirectoryProvider not available in fastmcp; "
                    "skill resources unavailable. %s will still be registered.",
                    SKILL_TOOL_NAME,
                )
            else:
                try:
                    self.mcp.add_provider(
                        SkillsDirectoryProvider(
                            roots=[skills_dir], supporting_files="resources"
                        )
                    )
                    logger.info("Registered bundled skills as MCP resources")
                    status["provider"] = "ok"
                except Exception:
                    logger.exception("Failed to register skills as resources")
                    status["provider"] = "failed"

        # Phase 3: Register the polymorphic tool unconditionally. Tool
        # absence would be a silent failure for tool-only clients; an
        # always-registered tool that reports "no skills available" is
        # the loud-failure alternative. Wrap the registration call so a
        # FastMCP-side regression (renamed mcp.tool kwargs, etc.) emits a
        # WARNING-level summary instead of aborting server startup.
        try:
            guidance_count = self._register_skill_guide_tool(skills_dir)
            status["tool"] = "ok"
            status["guidance_count"] = guidance_count
        except Exception:
            logger.exception(
                "Failed to register %s — tool-only clients will not see skill guidance",
                SKILL_TOOL_NAME,
            )
            status["tool"] = "failed"

        self._log_skill_registration_summary(status)

    @staticmethod
    def _log_skill_registration_summary(status: dict[str, str | int]) -> None:
        """Emit one-line summary of skill registration outcome.

        ``info`` when both provider and tool registered AND at least one
        skill bundle parsed; ``warning`` otherwise. The guidance>0 gate
        catches the "tool registered but exposes nothing" case (skills
        directory missing, empty, or every SKILL.md fails to parse) —
        the tool stays present so the failure is reachable via a tool
        call, but operators should grep for this warning when a user
        reports missing skill features.
        """
        provider = status.get("provider")
        tool = status.get("tool")
        raw_guidance = status.get("guidance_count", 0)
        guidance = raw_guidance if isinstance(raw_guidance, int) else 0

        message = "Skill system summary: provider=%s, tool=%s, guidance_count=%d"
        args = (provider, tool, guidance)
        if provider == "ok" and tool == "ok" and guidance > 0:
            logger.info(message, *args)
        else:
            logger.warning(message, *args)

    def _list_bundled_skills(
        self, skills_dir: Path
    ) -> list[tuple[str, Path, dict[str, Any]]]:
        """Return parsed (name, dir, frontmatter) triples for valid bundled skills.

        Skips entries that aren't directories, lack ``SKILL.md``, or whose
        frontmatter doesn't parse. Sorted by directory name for stable
        ordering across clients.
        """
        try:
            entries = sorted(skills_dir.iterdir())
        except OSError as e:
            logger.warning("Could not read skills directory %s: %s", skills_dir, e)
            return []

        skills: list[tuple[str, Path, dict[str, Any]]] = []
        for skill_dir in entries:
            main_file = skill_dir / "SKILL.md"
            if not skill_dir.is_dir() or not main_file.exists():
                continue
            frontmatter = self._parse_skill_frontmatter(main_file)
            if not frontmatter:
                continue
            skills.append((skill_dir.name, skill_dir, frontmatter))
        return skills

    @staticmethod
    def _list_skill_files(skill_dir: Path) -> list[str]:
        """Return relative file paths for a skill, filtering symlinks and traversal.

        Symlinks are skipped (defense against a malicious skill bundle
        linking outside its dir) and ``is_relative_to(resolved_root)``
        rejects anything that resolves outside the skill's own tree.
        """
        files: list[str] = []
        resolved_root = skill_dir.resolve()
        try:
            for f in sorted(skill_dir.rglob("*")):
                if not f.is_file() or f.is_symlink():
                    continue
                if not f.resolve().is_relative_to(resolved_root):
                    continue
                files.append(str(f.relative_to(skill_dir)))
        except OSError as e:
            logger.warning("Error reading skill files in %s: %s", skill_dir, e)
        return files

    def _register_skill_guide_tool(self, skills_dir: Path | None) -> int:
        """Register the polymorphic ``ha_get_skill_guide`` tool unconditionally.

        Returns the number of bundled skills whose frontmatter parsed
        successfully (used by ``_register_skills`` for the summary log).
        The tool is **always** registered regardless of the count — a
        missing tool would be a silent failure for tool-only clients.
        When no skills are reachable (missing submodule, empty dir, all
        frontmatter unparseable), the tool's description says so and
        Tier 1 returns an empty list with an explanatory note.

        The tool's description embeds every available skill's
        frontmatter ``description`` so claude.ai (which doesn't read
        server instructions) still sees trigger conditions in the
        catalog — same model the prior per-skill guidance tools used,
        collapsed into one tool.
        """
        skills = self._list_bundled_skills(skills_dir) if skills_dir is not None else []

        if skills:
            # Build the tool description with each skill's trigger
            # conditions. Keeps the "CALL THIS FIRST" framing the
            # per-skill tools used so claude.ai's catalog-level retrieval
            # surfaces it for relevant tasks.
            skill_blocks = [
                f"### {name} ({f'skill://{name}/SKILL.md'})\n"
                f"{fm['description'].strip()}"
                for name, _dir, fm in skills
            ]
            tool_description = (
                "Get bundled Home Assistant best-practice skill guides. "
                "CALL THIS FIRST before performing matching actions.\n\n"
                "Three modes (progressive disclosure):\n"
                "- No args: list bundled skills with their trigger conditions.\n"
                "- skill arg: list reference files for that skill.\n"
                "- skill + file args: read the file content.\n\n"
                "Bundled skills:\n\n"
                + "\n\n".join(skill_blocks)
                + f"\n\n{self._SKILL_USE_BEFORE_KEYWORDS}\n\n"
                + _OLD_SKILL_TOOL_ALIASES
            )
        else:
            # Degraded mode: tool registered but skills directory is
            # missing/empty. The description signals this so the LLM
            # doesn't keep retrying calls expecting content.
            # Even in degraded mode, append the action-phrased keyword
            # block so BM25 retrieval still ranks this tool for the
            # workflow positions the description covers — the tool is
            # mandatory-pinned, so it stays in the catalog regardless,
            # but the keywords keep ranking sane for tool-search.
            tool_description = (
                "Get bundled Home Assistant best-practice skill guides. "
                "No skill bundles are currently available on this server — "
                "the skills directory is missing, empty, or all SKILL.md "
                "files failed to parse. Calls return an empty listing; "
                "ask the operator to verify the skills-vendor submodule "
                f"is initialized.\n\n{self._SKILL_USE_BEFORE_KEYWORDS}\n\n"
                + _OLD_SKILL_TOOL_ALIASES
            )

        async def ha_get_skill_guide(
            skill: Annotated[
                str | None,
                Field(
                    description=(
                        "Skill name from the no-args listing "
                        "(e.g., 'home-assistant-best-practices')."
                    ),
                ),
            ] = None,
            file: Annotated[
                str | None,
                Field(
                    description=(
                        "Reference file path within the skill, relative "
                        "to the skill directory (e.g., 'SKILL.md' or "
                        "'references/automation-patterns.md'). Requires "
                        "skill to be set."
                    ),
                ),
            ] = None,
        ) -> dict[str, Any]:
            # ``skills_dir`` is captured from the enclosing scope at
            # registration time. The current ``_get_skills_dir()`` is
            # effectively static per process (it inspects an on-disk
            # path that doesn't change), so the closure is fine. If a
            # future change makes the skills location dynamic (env-var
            # override, etc.), the closure won't pick that up — re-read
            # via ``self._get_skills_dir()`` here instead.
            return self._handle_skill_guide_call(skills_dir, skill, file)

        self.mcp.tool(
            name=SKILL_TOOL_NAME,
            description=tool_description,
            annotations={"readOnlyHint": True, "idempotentHint": True},
            tags={"System"},
        )(ha_get_skill_guide)
        logger.info(
            "Registered %s (%d bundled skill(s))",
            SKILL_TOOL_NAME,
            len(skills),
        )
        return len(skills)

    def _handle_skill_guide_call(
        self,
        skills_dir: Path | None,
        skill: str | None,
        file: str | None,
    ) -> dict[str, Any]:
        """Dispatch a ``ha_get_skill_guide`` call to the right tier.

        Split out from the registered async closure so the same logic is
        unit-testable without round-tripping through the MCP tool layer.
        Synchronous because every operation is bounded local disk I/O.

        Return shape per tier:
        - Tier 1 (no args): ``{"success": True, "skills": [...], "how_to_use": ...}``
        - Tier 2 (skill): ``{"success": True, "skill": ..., "files": [...], ...}``
        - Tier 3 (skill+file): ``{"success": True, "skill": ..., "file": ..., "content": ...}``

        ``skills_dir`` is ``None`` when no skills directory exists on
        disk. In that case Tier 1 returns ``{"success": True,
        "degraded": True, "skills": [], ...}`` — the explicit
        ``degraded`` flag lets LLM clients branch on the
        misconfiguration without parsing the ``how_to_use`` prose,
        while ``success: True`` keeps generic "call succeeded"
        predicates honest. Tier 2/3 in degraded mode raise so the
        caller gets a clear error instead of a confusing empty result.
        Tool-level failures raise ``ToolError`` (via
        ``raise_tool_error``) per AGENTS.md, so clients see
        ``isError=true`` rather than a success payload with an
        embedded error.
        """
        # Degraded mode: no skills directory. Always return a structured
        # response so callers can detect the situation rather than
        # silently believing the tool list is just empty.
        if skills_dir is None:
            if not skill:
                # Explicit ``degraded`` flag so LLM clients can detect the
                # misconfiguration signal without parsing the
                # ``how_to_use`` prose. ``success: True`` is kept so
                # generic "call succeeded" predicates don't trip — the
                # tool DID return a structured response — but
                # ``degraded`` is the actionable branch.
                return {
                    "success": True,
                    "degraded": True,
                    "skills": [],
                    "how_to_use": (
                        "No skill bundles are available on this server. "
                        "The skills-vendor submodule may be missing or "
                        "uninitialized. Contact the server operator."
                    ),
                }
            raise_tool_error(
                create_error_response(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    message=(
                        "Cannot read skill: no skills directory is available "
                        "on this server."
                    ),
                    context={"skill": skill, "file": file},
                    suggestions=[
                        f"Call {SKILL_TOOL_NAME}() with no args to confirm "
                        "skill availability.",
                        "Ask the server operator to initialize the "
                        "skills-vendor submodule "
                        "(`git submodule update --init`).",
                    ],
                )
            )

        # Tier 1: no args → list bundled skills with frontmatter
        if not skill:
            skills = self._list_bundled_skills(skills_dir)
            return {
                "success": True,
                "skills": [
                    {
                        "skill": name,
                        "uri": f"skill://{name}/SKILL.md",
                        "description": fm["description"].strip(),
                    }
                    for name, _dir, fm in skills
                ],
                "how_to_use": (
                    f"Call {SKILL_TOOL_NAME}(skill='<name>') to list a "
                    f"skill's reference files, then "
                    f"{SKILL_TOOL_NAME}(skill='<name>', file='<path>') "
                    "to read content. Resource-capable clients can also "
                    "read skill:// URIs via resources/read."
                ),
            }

        skill_dir = skills_dir / skill
        # Reject four classes of bad ``skill`` argument before any I/O on
        # the resolved path:
        #
        # (a) Traversal — ``"../something"`` lets tier 2 list directories
        #     above the skills root.
        # (b) Symlinked skill DIRECTORY — applies the same anti-symlink
        #     stance as ``_list_skill_files`` (which filters symlinks
        #     per-file inside a skill) one level up, at the skill-dir
        #     entry point. The two scopes differ but the intent is the
        #     same: don't follow symlinks added to the skill bundle.
        # (c) Root-aliases — ``"."``, ``"./"``, ``"x/.."`` all resolve
        #     to the skills root itself. Without this check tier 2
        #     silently downgrades from "list one skill's files" to
        #     "list every file across every bundle." Not a security
        #     escape (skills are bundled content) but a contract
        #     mismatch with tier 1.
        # (d) Resolve failures — bubble as a structured INTERNAL_ERROR
        #     rather than a generic INTERNAL_ERROR from fastmcp's
        #     wrapper, mirroring tier 3.
        try:
            skill_resolved = skill_dir.resolve()
            skills_resolved = skills_dir.resolve()
        except OSError as e:
            raise_tool_error(
                create_error_response(
                    ErrorCode.INTERNAL_ERROR,
                    message=(f"Could not resolve path for skill {skill!r}: {e}"),
                    context={"skill": skill},
                    suggestions=[
                        "Check filesystem permissions on the skills-vendor directory.",
                        "Check the server logs for the underlying OSError.",
                    ],
                )
            )
        if (
            not skill_dir.exists()
            or not skill_dir.is_dir()
            or not skill_resolved.is_relative_to(skills_resolved)
            or skill_resolved == skills_resolved
            or skill_dir.is_symlink()
        ):
            raise_tool_error(
                create_error_response(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    message=f"Unknown skill: {skill!r}.",
                    context={"skill": skill},
                    suggestions=[
                        f"Call {SKILL_TOOL_NAME}() with no args to list "
                        "available skills.",
                        "Check the skill name for typos or path separators.",
                    ],
                )
            )

        # Tier 2: skill only → list reference files
        if not file:
            files = self._list_skill_files(skill_dir)
            return {
                "success": True,
                "skill": skill,
                "uri": f"skill://{skill}/SKILL.md",
                "files": [
                    {"name": name, "uri": f"skill://{skill}/{name}"} for name in files
                ],
                "how_to_use": (
                    f"Call {SKILL_TOOL_NAME}(skill={skill!r}, file='<name>') "
                    "to read a specific file. Start with SKILL.md for the "
                    "decision workflow."
                ),
            }

        # Tier 3: skill + file → read content.
        #
        # Check ``candidate.is_symlink()`` HERE, before ``candidate.resolve()``.
        # ``resolve()`` returns the canonical non-symlink path, so a
        # post-resolve ``is_symlink()`` check would always be False —
        # the pre-resolve check is the only one that actually catches a
        # symlink. Matches the is_symlink() filter in _list_skill_files
        # (tier 2 listings hide
        # symlinks, so tier 3 must reject them with the same semantics).
        candidate = skill_dir / file
        if candidate.is_symlink():
            raise_tool_error(
                create_error_response(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    message=(
                        f"Refusing to follow symlink at {file!r} in skill {skill!r}."
                    ),
                    context={"skill": skill, "file": file},
                    suggestions=[
                        f"Call {SKILL_TOOL_NAME}(skill={skill!r}) to see the "
                        "non-symlink files this skill exposes.",
                        "Ask the operator to replace the symlink with a "
                        "regular file inside the skill directory.",
                    ],
                )
            )
        try:
            target = candidate.resolve()
        except OSError as e:
            raise_tool_error(
                create_error_response(
                    ErrorCode.INTERNAL_ERROR,
                    message=(
                        f"Could not resolve path for file {file!r} in skill "
                        f"{skill!r}: {e}"
                    ),
                    context={"skill": skill, "file": file},
                    suggestions=[
                        "Check filesystem permissions on the skills-vendor directory.",
                        "Check the server logs for the underlying OSError.",
                    ],
                )
            )
        if not target.is_relative_to(skill_dir.resolve()) or not target.is_file():
            raise_tool_error(
                create_error_response(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    message=f"Unknown file {file!r} in skill {skill!r}.",
                    context={"skill": skill, "file": file},
                    suggestions=[
                        f"Call {SKILL_TOOL_NAME}(skill={skill!r}) to list "
                        "available files.",
                        "Verify the file path is relative to the skill "
                        "directory (e.g., 'references/foo.md').",
                    ],
                )
            )
        try:
            content = target.read_text(encoding="utf-8")
        except OSError as e:
            raise_tool_error(
                create_error_response(
                    ErrorCode.INTERNAL_ERROR,
                    message=f"Could not read file {file!r} in skill {skill!r}: {e}",
                    context={"skill": skill, "file": file},
                    suggestions=[
                        "Check filesystem permissions on the skills-vendor directory.",
                        "Check the server logs for the underlying OSError.",
                    ],
                )
            )

        return {
            "success": True,
            "skill": skill,
            "file": file,
            "uri": f"skill://{skill}/{file}",
            "content": content,
        }

    # Helper methods required by EnhancedToolsMixin

    async def smart_entity_search(
        self, query: str, domain_filter: str | None = None, limit: int = 10
    ) -> dict[str, Any]:
        """Bridge method to existing smart search implementation."""
        return cast(
            dict[str, Any],
            await self.smart_tools.smart_entity_search(
                query=query, limit=limit, include_attributes=False
            ),
        )

    async def get_entity_state(self, entity_id: str) -> dict[str, Any]:
        """Bridge method to existing entity state implementation."""
        return await self.client.get_entity_state(entity_id)

    async def call_service(
        self,
        domain: str,
        service: str,
        entity_id: str | None = None,
        data: dict | None = None,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """Bridge method to existing service call implementation."""
        service_data = data or {}
        if entity_id:
            service_data["entity_id"] = entity_id
        return await self.client.call_service(domain, service, service_data)

    async def get_entities_by_area(self, area_name: str) -> dict[str, Any]:
        """Bridge method to existing area functionality.

        ``smart_tools.get_entities_by_area`` enriches per-entity dicts
        with leading-underscore internals (``_hidden_by`` etc.) so
        downstream search branches can apply the score penalty without
        a second registry lookup. Strip them here so this public bridge
        doesn't leak internals to MCP clients.
        """
        result = await self.smart_tools.get_entities_by_area(
            area_query=area_name, group_by_domain=True
        )
        strip_internal_fields(result)
        return cast(dict[str, Any], result)

    async def start(self) -> None:
        """Start the Smart MCP server with async compatibility."""
        logger.info(
            f"🚀 Starting Smart {self.settings.mcp_server_name} v{self.settings.mcp_server_version}"
        )

        # Test connection on startup
        try:
            success, error = await self.client.test_connection()
            if success:
                config = await self.client.get_config()
                logger.info(
                    f"✅ Successfully connected to Home Assistant: {config.get('location_name', 'Unknown')}"
                )
            else:
                logger.warning(f"⚠️ Failed to connect to Home Assistant: {error}")
        except Exception as e:
            logger.error(f"❌ Error testing connection: {e}")

        # Log available tools count
        logger.info("🔧 Smart server with enhanced tools loaded")

        # Run the MCP server with async compatibility
        await self.mcp.run_async()

    async def close(self) -> None:
        """Close the MCP server and cleanup resources."""
        # Only close client if it was actually created
        if self._client is not None and hasattr(self._client, "close"):
            await self._client.close()
        logger.info("🔧 Home Assistant Smart MCP Server closed")
