"""Sample code for FastMCP using MCPMixin."""

from ha_mcp._vendor.fastmcp import FastMCP
from ha_mcp._vendor.fastmcp.contrib.bulk_tool_caller import BulkToolCaller

mcp = FastMCP()


@mcp.tool
def echo_tool(text: str) -> str:
    """Echo the input text"""
    return text


bulk_tool_caller = BulkToolCaller()

bulk_tool_caller.register_tools(mcp)
