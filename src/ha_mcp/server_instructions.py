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
from .utils.skill_loader import list_skills

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

# The tool, not the resources, whenever it is served (#2802): the strict-BPS
# key and the MandatoryBPS=false opt-out ride only on its responses, so a
# client that reads the resources still gets every reference file attached
# to its writes. The opt-out is deliberately not named here: an agent told
# about it up front could switch the attach off without reading anything.
_SKILL_TOOL_ACCESS = (
    f"Call {SKILL_TOOL_NAME}() with no arguments: it returns SKILL.md. "
    "Then pass file='<path>' for each reference file the table points "
    "to. Use this tool even if your client can read the skill:// "
    "resources."
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

        states = effective_tool_config(settings).get("tools", {})
        return states.get(
            SKILL_TOOL_NAME
        ) == "disabled" and SKILL_TOOL_NAME not in effective_mandatory_tools(settings)
    except Exception:
        logger.debug("skill-tool visibility lookup failed", exc_info=True)
        return False


def search_tool_skills_hint(tool_hidden: bool) -> str:
    """The skills hint on the tool-search description: same tool-first rule."""
    if tool_hidden:
        return (
            "This server also provides best-practice skills via skill:// "
            "resources. Read the relevant SKILL.md (or your locally "
            "installed copy of these skills) before creating automations "
            "or configuring devices."
        )
    return (
        f"This server also provides best-practice skills. Call "
        f"{SKILL_TOOL_NAME} (directly, no proxy needed) to read the "
        "relevant SKILL.md before creating automations or configuring "
        "devices, even if your client supports MCP resources."
    )


def skill_block(skill_name: str, description: str) -> str:
    """One skill's entry: its resource URI and its LLM-authored description."""
    uri = f"skill://{skill_name}/SKILL.md"
    return f"\n### Skill: {skill_name} ({uri})\n{description.strip()}"


def build_skills_instructions(
    skills_dir: Path | None, settings: Settings, tool_hidden: bool
) -> str | None:
    """Build the skills section from each bundled SKILL.md's frontmatter.

    The description field is authored for LLM consumption and included
    as-is, never parsed or restructured by code. Returns None when there is
    no skills directory or no parseable skill; the instructions then start
    with the notices.
    """
    skills = list_skills(skills_dir) if skills_dir else []
    if not skills:
        return None

    access_method = (
        _RESOURCE_ACCESS + _LOCAL_SKILLS_FALLBACK if tool_hidden else _SKILL_TOOL_ACCESS
    )
    header = _SKILLS_HEADER.format(access_method=access_method)
    instructions = header + "\n".join(
        skill_block(name, frontmatter["description"])
        for name, _dir, frontmatter in skills
    )

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
