"""Run sandbox code the way an agent does: through ``ha_manage_custom_tool``.

The tool is registered on a FastMCP server with Code Mode switched on, so the
code runs in the real sandbox with the real bridge to the client.
"""

from __future__ import annotations

from typing import Any

import pytest

from ha_mcp._vendor.fastmcp import FastMCP
from ha_mcp.config import get_global_settings
from ha_mcp.tools.tools_code import register_code_tools


async def run_custom_tool(
    client: Any, monkeypatch: pytest.MonkeyPatch, code: str, **settings: Any
) -> Any:
    """Run ``code`` and return what it evaluated to.

    ``settings`` overrides Code Mode settings for this run (for example
    ``code_mode_max_duration=1.0``). A failed run raises the tool's ToolError.
    """
    current = get_global_settings()
    monkeypatch.setattr(current, "enable_code_mode", True)
    for name, value in settings.items():
        monkeypatch.setattr(current, name, value)
    mcp = FastMCP("code-mode")
    register_code_tools(mcp, client)
    result = await mcp.call_tool(
        "ha_manage_custom_tool", {"code": code, "justification": "unit test"}
    )
    assert result.structured_content is not None
    return result.structured_content["data"]["result"]
