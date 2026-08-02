"""MCP Prompts layer for Home Assistant.

Provides reusable, parameterized prompts that guide LLMs through
structured workflows for safety, troubleshooting, automation,
status checks, and security-sensitive operations.
"""

from __future__ import annotations

import logging

from fastmcp import FastMCP

from .automation import register_automation_prompts
from .safety import register_safety_prompts
from .security import register_security_prompts
from .status import register_status_prompts
from .troubleshooting import register_troubleshooting_prompts

logger = logging.getLogger(__name__)


def register_all_prompts(mcp: FastMCP) -> None:
    """Register all prompt modules with the MCP server."""
    register_safety_prompts(mcp)
    register_troubleshooting_prompts(mcp)
    register_automation_prompts(mcp)
    register_status_prompts(mcp)
    register_security_prompts(mcp)
    logger.info(
        "Registered MCP prompt modules "
        "(safety, troubleshooting, automation, status, security)"
    )
