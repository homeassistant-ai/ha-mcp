"""Prompt management functionality."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ha_mcp._vendor.mcp_types import InputRequiredResult

from ha_mcp._vendor.mcp.server.mcpserver.prompts.base import Message, Prompt
from ha_mcp._vendor.mcp.server.mcpserver.utilities.logging import get_logger

if TYPE_CHECKING:
    from ha_mcp._vendor.mcp.server.context import LifespanContextT, RequestT
    from ha_mcp._vendor.mcp.server.mcpserver.context import Context

logger = get_logger(__name__.removeprefix("ha_mcp._vendor."))


class PromptManager:
    """Manages MCPServer prompts."""

    def __init__(self, warn_on_duplicate_prompts: bool = True):
        self._prompts: dict[str, Prompt] = {}
        self.warn_on_duplicate_prompts = warn_on_duplicate_prompts

    def get_prompt(self, name: str) -> Prompt | None:
        """Get prompt by name."""
        return self._prompts.get(name)

    def list_prompts(self) -> list[Prompt]:
        """List all registered prompts."""
        return list(self._prompts.values())

    def add_prompt(
        self,
        prompt: Prompt,
    ) -> Prompt:
        """Add a prompt to the manager."""

        # Check for duplicates
        existing = self._prompts.get(prompt.name)
        if existing:
            if self.warn_on_duplicate_prompts:
                logger.warning(f"Prompt already exists: {prompt.name}")
            return existing

        self._prompts[prompt.name] = prompt
        return prompt

    def remove_prompt(self, name: str) -> None:
        """Remove a prompt by name."""
        if name not in self._prompts:
            raise ValueError(f"Unknown prompt: {name}")
        del self._prompts[name]

    async def render_prompt(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        context: Context[LifespanContextT, RequestT],
    ) -> list[Message] | InputRequiredResult:
        """Render a prompt by name with arguments."""
        prompt = self.get_prompt(name)
        if not prompt:
            raise ValueError(f"Unknown prompt: {name}")

        return await prompt.render(arguments, context)
