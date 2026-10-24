"""Protocol support for the MCP Skills extension (SEP-2640)."""

from typing import TYPE_CHECKING
from ha_mcp._vendor.fastmcp.skills.models import (
    GetSkillResult,
    ListSkillsResult,
    Skill,
    SkillFrontmatter,
    SkillResource,
)

__all__ = [
    "GetSkillResult",
    "ListSkillsResult",
    "Skill",
    "SkillFrontmatter",
    "SkillResource",
    "SkillsClientExtension",
]


if TYPE_CHECKING:
    from ha_mcp._vendor.fastmcp.skills.client import SkillsClientExtension as SkillsClientExtension


def __getattr__(name: str) -> object:
    if name == "SkillsClientExtension":
        from ha_mcp._vendor.fastmcp.skills.client import SkillsClientExtension

        return SkillsClientExtension
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
