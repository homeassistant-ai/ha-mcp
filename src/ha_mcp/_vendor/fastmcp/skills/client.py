"""Client capability declaration for the MCP Skills extension."""

from ha_mcp._vendor.mcp.client.extension import ClientExtension

from ha_mcp._vendor.fastmcp.skills._constants import SKILLS_EXTENSION_ID


class SkillsClientExtension(ClientExtension):
    """Declare support for the SEP-2640 Skills extension."""

    identifier = SKILLS_EXTENSION_ID
