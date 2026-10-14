"""The instructions the server sends in the MCP initialize response.

Sections, in order: the bundled best-practice skills with how to read them
(plus the tool-search note when that mode is on), the Read Only Mode
warning, the issue-filing rule, and the installation owner's
``custom_instructions``. The skills header and access fallbacks are
defined here; the notices come from ``server_tool_text``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from .server_tool_text import (
    ISSUE_FILING_INSTRUCTIONS,
    READ_ONLY_INSTRUCTIONS,
    SKILL_TOOL_NAME,
    TOOL_DISCOVERY_INSTRUCTIONS,
)
from .transforms import DEFAULT_PINNED_TOOLS
from .utils.skill_loader import parse_skill_frontmatter

if TYPE_CHECKING:
    from .config_settings import Settings

logger = logging.getLogger(__name__)

OWNER_HEADING = "## Instructions from this installation's owner"

_SKILLS_HEADER = (
    "IMPORTANT: This server provides best-practice skills that MUST "
    "be consulted before performing matching actions. "
    "Read the SKILL.md for the matching skill "
    "— it contains a Reference Files table that maps tasks to "
    "specific reference files. You MUST read the referenced files "
    "that match your current task before proceeding. "
    "Do NOT load all reference files upfront "
    "— only the ones the table directs you to.\n\n"
    "How to access: {access_method}\n"
)

_RESOURCE_ACCESS = (
    "Read the skill via MCP resources (resources/read with the "
    "skill:// URI) — if you can read these instructions, you "
    "should be able to access resources as well. "
)

# The user disabled the skill tool (possible only outside strict-BPS mode,
# #1886) — directing clients to it would steer them into a tool that is not
# in the catalog. Their stated fallback is a local skills install.
_LOCAL_SKILLS_FALLBACK = (
    "If for any reason you cannot access MCP resources, refer to your "
    "locally installed copy of these skills instead (the "
    f"{SKILL_TOOL_NAME} fallback tool is disabled on this server)."
)

_SKILL_TOOL_FALLBACK = (
    f"If for any reason you cannot access MCP resources, call {SKILL_TOOL_NAME}() "
    "with no arguments as a fallback: it returns SKILL.md, then "
    "pass file='<path>' for a reference file. If you can access "
    "resources normally, do not waste time or tokens on that tool."
)


def skill_tool_hidden(settings: Settings) -> bool:
    """True when ha_get_skill_guide will be absent from the catalog.

    The tool is user- or env-disableable only while strict-BPS mode
    is off (#1886) — apply_tool_visibility strips the disable
    otherwise. Consulted at instruction-build time so the skills
    header doesn't direct clients to a tool that is not being
    served. Fails toward False (mention the tool): a wrong mention
    costs one unknown-tool error, a wrong omission hides the only
    non-resource access path.
    """
    try:
        from .settings_ui._persistence import effective_tool_config
        from .settings_ui._tools_meta import effective_mandatory_tools

        states = effective_tool_config().get("tools", {})
        return states.get(
            SKILL_TOOL_NAME
        ) == "disabled" and SKILL_TOOL_NAME not in effective_mandatory_tools(settings)
    except Exception:
        logger.debug("skill-tool visibility lookup failed", exc_info=True)
        return False


def build_skill_block(skill_name: str, main_file: Path) -> str | None:
    """Build an instruction block for a single skill.

    Reads the description field from YAML frontmatter and includes it
    verbatim. The description is designed for LLM consumption and
    contains its own trigger conditions and symptom indicators.
    """
    frontmatter = parse_skill_frontmatter(main_file)
    if not frontmatter:
        return None

    description = frontmatter["description"]
    uri = f"skill://{skill_name}/SKILL.md"

    return f"\n### Skill: {skill_name} ({uri})\n{description.strip()}"


def build_skills_instructions(
    skills_dir: Path | None, settings: Settings, tool_hidden: bool
) -> str | None:
    """Build server instructions from bundled skill frontmatter.

    Reads the description field from each SKILL.md's YAML frontmatter
    and includes it as-is in the server instructions. The description
    is authored for LLM consumption and should not be parsed or
    restructured by code.

    Returns None when no skills directory or no parseable skills are
    present, leaving instructions unchanged from the default (None).
    """
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

        block = build_skill_block(skill_dir.name, main_file)
        if block:
            skill_blocks.append(block)

    if not skill_blocks:
        return None

    fallback = _LOCAL_SKILLS_FALLBACK if tool_hidden else _SKILL_TOOL_FALLBACK
    header = _SKILLS_HEADER.format(access_method=_RESOURCE_ACCESS + fallback)
    instructions = header + "\n".join(skill_blocks)

    if settings.enable_tool_search:
        instructions += TOOL_DISCOVERY_INSTRUCTIONS.format(
            pinned=", ".join(DEFAULT_PINNED_TOOLS)
        )

    return instructions


def compose_instructions(skills: str | None, settings: Settings) -> str:
    """Join the skills section with the startup notices and the owner's text."""
    sections = [skills] if skills else []
    # Surface Read Only Mode in the startup instructions so clients
    # that show server instructions warn the model up front. Startup
    # state only — live flips are covered by the structured
    # READ_ONLY_MODE call errors and the ha_get_overview field.
    if settings.read_only_mode:
        sections.append(READ_ONLY_INSTRUCTIONS)
    sections.append(ISSUE_FILING_INSTRUCTIONS)
    custom = settings.custom_instructions.strip()
    if custom:
        # Length only: the text itself is the owner's and may be personal.
        logger.info("custom_instructions active (%d characters)", len(custom))
        sections.append(f"{OWNER_HEADING}\n{custom}")
    return "\n\n".join(sections)
