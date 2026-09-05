"""``ha_report_issue`` validates ``fields`` before it collects anything.

The projection at the end of the tool was the only place ``fields`` was parsed,
outside any ``ValueError`` handler, so a malformed value reached FastMCP as a
bare exception after the whole report had been assembled (Codex, PR #2375).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_bug_report import register_bug_report_tools


def _build_tool(client: Any) -> Any:
    registered: dict[str, Any] = {}

    def capture_add_tool(method: Any) -> None:
        name = (
            method.__fastmcp__.name
            if hasattr(method, "__fastmcp__")
            else method.__name__
        )
        registered[name] = method

    mcp = MagicMock()
    mcp.add_tool = capture_add_tool
    register_bug_report_tools(mcp, client)
    return registered["ha_report_issue"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields",
    [
        pytest.param("[" * 20_000, id="over-nested"),
        pytest.param("[not json", id="invalid-json"),
    ],
)
async def test_malformed_fields_is_a_validation_error_before_collection(
    fields: str,
) -> None:
    client = MagicMock()
    tool = _build_tool(client)

    with pytest.raises(ToolError) as exc:
        await tool(fields=fields)

    payload = json.loads(str(exc.value))
    assert payload["error"]["code"].startswith("VALIDATION")
    assert "fields" in str(payload)
    # Refused before the report was assembled: the client was never consulted.
    assert client.method_calls == []
