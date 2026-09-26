"""Guard: no tool docstring may carry a parameter section.

FastMCP (3.2.4+) runs every tool docstring through a Google/NumPy/Sphinx
parser. When the parser finds a parameter section (``Args:``, ``Parameters``,
``:param x:``), the published description is only the text before the first
section it recognises, and everything after -- modes, caveats, ``EXAMPLES``
-- is silently dropped from ``tools/list``. The parameter entries then reach the schema only
for parameters that have no ``Field(description=...)``. Parameter meaning
belongs in ``Field(description=...)``; a tool docstring carries no parameter
section.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest

from .test_container_param_coercion_complete import _all_registered_tools


@pytest.fixture
def all_tools() -> dict[str, Any]:
    """Every registered tool, dev mode included (``ha_dev_*`` are not flags)."""
    from ha_mcp.config import _reset_global_settings

    previous = os.environ.get("HAMCP_ENABLE_DEV_MODE")
    os.environ["HAMCP_ENABLE_DEV_MODE"] = "true"
    try:
        return _all_registered_tools()
    finally:
        if previous is None:
            os.environ.pop("HAMCP_ENABLE_DEV_MODE", None)
        else:
            os.environ["HAMCP_ENABLE_DEV_MODE"] = previous
        _reset_global_settings()


def test_no_tool_docstring_has_a_parameter_section(
    all_tools: dict[str, Any],
) -> None:
    from ha_mcp._vendor.fastmcp.utilities.docstring_parsing import parse_docstring

    with_section = sorted(
        name for name, tool in all_tools.items() if parse_docstring(tool.fn).parameters
    )

    assert with_section == [], (
        "These tool docstrings carry a parameter section, which FastMCP turns "
        "into a truncated description; move each entry into "
        f"Field(description=...): {with_section}"
    )


def test_the_guard_detects_a_parameter_section() -> None:
    """Positive control: an ``Args:`` section does truncate the description."""
    from ha_mcp._vendor.fastmcp import FastMCP

    mcp = FastMCP("control")

    @mcp.tool
    def sample(x: int) -> int:
        """Do a thing.

        Args:
            x: A number.

        EXAMPLES:
        - sample(x=1)
        """
        return x

    from ha_mcp._vendor.fastmcp.utilities.docstring_parsing import parse_docstring

    tool = asyncio.run(mcp.get_tool("sample"))
    assert tool is not None
    assert tool.description == "Do a thing."
    assert parse_docstring(tool.fn).parameters == {"x": "A number."}
