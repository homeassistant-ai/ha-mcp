"""Lite docstrings transform for ha-mcp.

Replaces the description on a configurable set of tools with a shorter,
"lite" variant that defers detailed guidance to the
``ha_get_skill_home_assistant_best_practices`` tool (or its skill://
resource). Trades catalog token usage for the assumption that the LLM
will read the skill when it needs more detail — see issue #1062.

This transform is opt-in (``ENABLE_LITE_DOCSTRINGS`` / dev-addon
``enable_lite_docstrings``) and beta. Tools NOT in the mapping pass
through unchanged so smaller tools keep their existing one-line
descriptions without us having to enumerate them.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from fastmcp.server.transforms import Transform
from fastmcp.tools import Tool

if TYPE_CHECKING:
    from fastmcp.server.transforms import GetToolNext
    from fastmcp.utilities.versions import VersionSpec

logger = logging.getLogger(__name__)


class LiteDocstringsTransform(Transform):
    """Replace heavy tool descriptions with lighter variants when enabled.

    Behaviour:
    - If ``enabled`` is False (default), every tool passes through
      unchanged. The transform is effectively a no-op so installing it
      unconditionally is safe.
    - If ``enabled`` is True, any tool whose name appears in
      ``replacements`` has its ``description`` replaced with the mapped
      text. Tools not in the mapping are returned unchanged.

    The transform deliberately does not auto-append a pointer to
    ``ha_get_skill_home_assistant_best_practices`` — the mapped lite
    description owns its own pointer text so per-tool wording can stay
    natural.
    """

    def __init__(
        self,
        *,
        enabled: bool,
        replacements: dict[str, str] | None = None,
    ) -> None:
        self._enabled = enabled
        self._replacements: dict[str, str] = replacements or {}

    def _apply(self, tool: Tool) -> Tool:
        if not self._enabled:
            return tool
        lite = self._replacements.get(tool.name)
        if lite is None:
            return tool
        return tool.model_copy(update={"description": lite})

    async def list_tools(self, tools: Sequence[Tool]) -> Sequence[Tool]:
        return [self._apply(t) for t in tools]

    async def get_tool(
        self,
        name: str,
        call_next: GetToolNext,
        *,
        version: VersionSpec | None = None,
    ) -> Tool | None:
        tool: Any = await call_next(name, version=version)
        return self._apply(tool) if tool else None
