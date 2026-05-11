"""Lite docstrings transform for ha-mcp.

Replaces the description on a configurable set of tools with a shorter,
"lite" variant that defers detailed guidance to the
``ha_get_skill_home_assistant_best_practices`` tool (or its skill://
resource). Trades catalog token usage for the assumption that the LLM
will read the skill when it needs more detail — see issue #1062.

Opt-in via ``enable_lite_docstrings``; see ``docs/beta.md`` for trade-offs.
Tools NOT in the mapping pass through unchanged, so smaller tools keep
their existing descriptions without us having to enumerate them.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

from fastmcp.server.transforms import Transform
from fastmcp.tools import Tool

if TYPE_CHECKING:
    from fastmcp.server.transforms import GetToolNext
    from fastmcp.utilities.versions import VersionSpec

logger = logging.getLogger(__name__)


class LiteDocstringsTransform(Transform):
    """Replace heavy tool descriptions with shorter variants.

    Mirrors ``SearchKeywordsTransform``: an empty mapping is a no-op,
    so installing the transform unconditionally would be safe. The
    caller (``server._apply_lite_docstrings``) only installs it when
    the feature flag is on, but the empty-dict-as-no-op contract keeps
    the transform self-contained.

    The transform deliberately does not auto-append a pointer to
    ``ha_get_skill_home_assistant_best_practices`` — the mapped lite
    description owns its own pointer text so per-tool wording can stay
    natural.
    """

    def __init__(self, replacements: dict[str, str] | None = None) -> None:
        self._replacements: dict[str, str] = replacements or {}

    def _rewrite(self, tool: Tool) -> Tool:
        lite = self._replacements.get(tool.name)
        if lite is None:
            return tool
        return tool.model_copy(update={"description": lite})

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
