"""Append a client-facing note to every write tool's description.

Claude Desktop's manual-approval dialog for a local MCP server becomes
clickable while the model is still streaming the call's arguments; an
early "Allow once" silently drops the call and the client reports a
4-minute timeout (issue #2367, anthropics/claude-code#92014). The server
never sees those calls, so the only place to steer the user is the tool
description the agent reads. Read-only tools are left untouched: they are
short enough that the window is effectively closed.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from ha_mcp._vendor.fastmcp.server.transforms import Transform
from ha_mcp._vendor.fastmcp.tools import Tool

if TYPE_CHECKING:
    from ha_mcp._vendor.fastmcp.server.transforms import GetToolNext
    from ha_mcp._vendor.fastmcp.utilities.versions import VersionSpec

DESKTOP_APPROVAL_NOTE = (
    "If a Claude Desktop user gets a 4-minute timeout with no result on this "
    "call, tell them to wait a few seconds before clicking the manual-approve "
    "button, or to set this tool to Always allow."
)


class WriteToolNoteTransform(Transform):
    """Append ``note`` to the description of every ``destructive_hint`` tool."""

    def __init__(self, note: str = DESKTOP_APPROVAL_NOTE) -> None:
        self._note = note

    def _rewrite(self, tool: Tool) -> Tool:
        annotations = tool.annotations
        if not (annotations and annotations.destructive_hint):
            return tool
        base = (tool.description or "").rstrip()
        description = f"{base}\n\n{self._note}" if base else self._note
        return tool.model_copy(update={"description": description})

    async def list_tools(self, tools: Sequence[Tool]) -> Sequence[Tool]:
        return [self._rewrite(t) for t in tools]

    async def get_tool(
        self,
        name: str,
        call_next: GetToolNext,
        *,
        version: VersionSpec | None = None,
    ) -> Tool | None:
        tool = await call_next(name, version=version)
        return self._rewrite(tool) if tool else None
