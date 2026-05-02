"""
Core Smart MCP Server implementation.

Implements lazy initialization pattern for improved startup time:
- Settings and FastMCP server are created immediately (fast)
- Smart tools and device tools are created lazily on first access
- Tool modules are discovered at startup but imported on first use
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, cast

import yaml  # type: ignore[import-untyped]
from fastmcp import FastMCP
from mcp.types import Icon

from .config import _PACKAGE_VERSION, get_global_settings
from .tools.enhanced import EnhancedToolsMixin
from .transforms import DEFAULT_PINNED_TOOLS

if TYPE_CHECKING:
    from .client.rest_client import HomeAssistantClient
    from .tools.registry import ToolsRegistry

logger = logging.getLogger(__name__)

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
        self._skill_tool_names: list[str] = []
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

        # Enrich tool descriptions with BM25 keyword boosts. Runs
        # unconditionally so Claude's native deferred-tool search
        # (claude.ai) benefits even when ENABLE_TOOL_SEARCH is off.
        # Must come before _apply_tool_search so CategorizedSearchTransform
        # indexes the enriched descriptions.
        self._apply_search_keyword_enrichment()

        # Apply tool search transform (must come after all tools and
        # ResourcesAsTools are registered so it can wrap everything)
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

        Returns None when skills are disabled, leaving instructions unchanged
        from the default (None).
        """
        if not self.settings.enable_skills:
            return None

        skills_dir = self._get_skills_dir()
        if not skills_dir:
            return None

        try:
            entries = sorted(skills_dir.iterdir())
        except OSError:
            logger.warning("Could not read skills directory: %s", skills_dir)
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

        # Build the access method instruction based on config
        if self.settings.enable_skills_as_tools:
            access_method = (
                "Read the skill via MCP resources (resources/read with the "
                "skill:// URI) — if you can read these instructions, you "
                "should be able to access resources as well. If for any "
                "reason you cannot access MCP resources, use the "
                "list_resources and read_resource tools as a fallback. "
                "If you can access resources normally, do not waste "
                "time or tokens on those tools."
            )
        else:
            access_method = (
                "Read the skill via MCP resources (resources/read with the "
                "skill:// URI)."
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
        except OSError:
            logger.warning("Could not read %s", main_file)
            return None

        parts = content.split("---", 2)
        if len(parts) < 3:
            logger.warning("No valid frontmatter delimiters in %s", main_file)
            return None

        try:
            frontmatter = yaml.safe_load(parts[1])
        except yaml.YAMLError:
            logger.warning("Could not parse YAML frontmatter in %s", main_file)
            return None

        if not isinstance(frontmatter, dict):
            logger.warning("Frontmatter is not a mapping in %s", main_file)
            return None

        if not frontmatter.get("description", ""):
            logger.warning(
                "No description in frontmatter for %s", main_file.parent.name
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
            logger.info("Applied persisted tool config (%d entries)", len(config.get("tools", {})))

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
        gating. ResourcesAsTools (list_resources/read_resource) are also
        pinned when enabled.

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

        # Build the always_visible list: defaults + user-configured pins
        pinned = list(self._PINNED_TOOLS)
        pinned.extend(self._user_pinned_tools)

        # Pin ResourcesAsTools and skill guidance tools if skills-as-tools is enabled
        if self.settings.enable_skills_as_tools:
            pinned.extend(["list_resources", "read_resource"])
            # Forward-compatible: pin skill guidance tools registered by #732
            pinned.extend(getattr(self, "_skill_tool_names", []))

        # When skills-as-tools is enabled, the client likely doesn't support
        # resources or server instructions — add skills hint to the search
        # tool description (the one place the LLM is guaranteed to see).
        description = self._SEARCH_TOOL_DESCRIPTION
        if self.settings.enable_skills_as_tools:
            description += (
                "\n\nThis server also provides best-practice skills via "
                "skill:// resources. If your client supports MCP resources, "
                "prefer reading them directly. Otherwise, call "
                "list_resources and read_resource (directly, no proxy "
                "needed) to access the relevant SKILL.md before creating "
                "automations or configuring devices."
            )

        try:
            self.mcp.add_transform(
                CategorizedSearchTransform(
                    max_results=self.settings.tool_search_max_results,
                    always_visible=pinned,
                    search_tool_description=description,
                )
            )
            logger.info(
                "Tool search transform applied (%d pinned tools, max_results=%d)",
                len(pinned),
                self.settings.tool_search_max_results,
            )
        except Exception:
            logger.exception("Failed to apply tool search transform")

    def _register_skills(self) -> None:
        """Register bundled HA best-practice skills as MCP resources.

        Uses FastMCP's SkillsDirectoryProvider to serve skill files via skill:// URIs.
        Optionally exposes skills as tools (list_resources/read_resource) for clients
        that don't support MCP resources natively.

        Controlled by ENABLE_SKILLS and ENABLE_SKILLS_AS_TOOLS settings.
        """
        if not self.settings.enable_skills:
            return

        # Phase 1: Import SkillsDirectoryProvider
        try:
            from fastmcp.server.providers.skills import SkillsDirectoryProvider
        except ImportError:
            logger.warning(
                "SkillsDirectoryProvider not available in fastmcp, skipping skills"
            )
            return

        # Phase 2: Register skills as MCP resources
        try:
            skills_dir = self._get_skills_dir()
            if not skills_dir:
                logger.warning(
                    "Skills directory not found at %s, skipping skill registration",
                    Path(__file__).parent / "resources" / "skills-vendor" / "skills",
                )
                return

            self.mcp.add_provider(
                SkillsDirectoryProvider(
                    roots=[skills_dir], supporting_files="resources"
                )
            )
            logger.info("Registered bundled skills as MCP resources")
        except Exception:
            logger.exception("Failed to register skills as resources")
            return

        # Phase 3: Optionally expose skills as tools
        if not self.settings.enable_skills_as_tools:
            return

        try:
            from fastmcp.server.transforms import ResourcesAsTools
        except ImportError:
            logger.warning(
                "ResourcesAsTools not available in fastmcp, "
                "skills registered as resources but not exposed as tools"
            )
            return

        try:
            self.mcp.add_transform(ResourcesAsTools(self.mcp))
            logger.info("Skills also exposed as tools (ResourcesAsTools)")
        except Exception:
            logger.exception(
                "Failed to expose skills as tools (resources still available)"
            )

        # Phase 4: Register skill guidance tools for clients that don't read
        # server instructions (e.g., claude.ai). The tool description contains
        # the trigger conditions so the AI sees them in the tool listing.
        # Names stored for pinning in search transforms (always-visible).
        self._register_skill_guidance_tools(skills_dir)

    def _register_skill_guidance_tools(self, skills_dir: Path) -> None:
        """Register a lightweight guidance tool per skill.

        Clients like claude.ai don't read the MCP server instructions field,
        so the bootstrap prompt (trigger conditions, symptoms) is invisible.
        This registers a tool per skill whose description contains the trigger
        conditions. The tool itself just lists available reference files —
        actual content is loaded on demand via read_resource.
        """
        try:
            entries = sorted(skills_dir.iterdir())
        except OSError:
            logger.warning("Could not read skills directory: %s", skills_dir)
            return

        for skill_dir in entries:
            main_file = skill_dir / "SKILL.md"
            if not skill_dir.is_dir() or not main_file.exists():
                continue

            frontmatter = self._parse_skill_frontmatter(main_file)
            if not frontmatter:
                continue

            description = frontmatter["description"].strip()
            skill_name = skill_dir.name
            tool_name = f"ha_get_skill_{skill_name.replace('-', '_')}"
            uri = f"skill://{skill_name}/SKILL.md"

            tool_description = (
                f"CALL THIS FIRST before performing matching actions. "
                f"{description}\n\n"
                f"Returns available reference files. Use read_resource with "
                f"the file URI to load specific guides as needed."
            )

            ref_files = self._collect_skill_ref_files(skill_dir, skill_name)

            # Use factory to capture ref_files in closure
            def _make_skill_handler(
                s_name: str,
                s_uri: str,
                files: list[dict[str, str]],
            ) -> Callable[[], Coroutine[Any, Any, dict[str, Any]]]:
                async def handler() -> dict[str, Any]:
                    return {
                        "skill": s_name,
                        "skill_uri": s_uri,
                        "how_to_use": (
                            "Use read_resource with a file URI below to load "
                            "the specific reference you need. Start with "
                            "SKILL.md for the decision workflow."
                        ),
                        "available_files": files,
                    }

                return handler

            self.mcp.tool(
                name=tool_name,
                description=tool_description,
                annotations={"readOnlyHint": True},
            )(_make_skill_handler(skill_name, uri, ref_files))

            self._skill_tool_names.append(tool_name)
            logger.info(
                "Registered skill guidance tool %s (%d reference files)",
                tool_name,
                len(ref_files),
            )

    @staticmethod
    def _collect_skill_ref_files(
        skill_dir: Path, skill_name: str
    ) -> list[dict[str, str]]:
        """Collect reference files for a skill, filtering symlinks and path traversal."""
        ref_files: list[dict[str, str]] = []
        resolved_root = skill_dir.resolve()
        try:
            for f in sorted(skill_dir.rglob("*")):
                if not f.is_file() or f.is_symlink():
                    continue
                if not f.resolve().is_relative_to(resolved_root):
                    continue
                rel = f.relative_to(skill_dir)
                ref_files.append(
                    {"name": str(rel), "uri": f"skill://{skill_name}/{rel}"}
                )
        except OSError:
            logger.warning("Error reading skill files in %s", skill_dir)
        return ref_files

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
        """Bridge method to existing area functionality."""
        return cast(
            dict[str, Any],
            await self.smart_tools.get_entities_by_area(
                area_query=area_name, group_by_domain=True
            ),
        )

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
