"""Call ``ha_report_issue`` on the server's own client, for the report tests."""

from __future__ import annotations

import re
from typing import Any

import pytest

from ha_mcp._vendor.fastmcp import Client, FastMCP
from ha_mcp._vendor.mcp.types import Implementation
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.helpers import log_tool_usage
from ha_mcp.tools.tools_bug_report import BugReportTools, register_bug_report_tools


async def report_issue(
    client: HomeAssistantClient,
    *,
    client_info: Implementation | None = None,
    **arguments: Any,
) -> dict[str, Any]:
    """The report an agent gets from ``ha_report_issue``.

    With ``client_info`` the call comes from a connected MCP client that sent
    it in its handshake; without, the call carries no MCP context at all.
    """
    if client_info is None:
        report: dict[str, Any] = await BugReportTools(client).ha_report_issue(
            **arguments
        )
        return report
    mcp = FastMCP("report-issue")
    register_bug_report_tools(mcp, client)
    async with Client(mcp, client_info=client_info) as mcp_client:
        result = await mcp_client.call_tool("ha_report_issue", arguments)
    assert isinstance(result.structured_content, dict)
    return result.structured_content


def report_line(report: dict[str, Any], label: str) -> str:
    """The value of one ``<label>: value`` line of the formatted report."""
    found = re.search(
        rf"^{re.escape(label)}: (.*)$", report["formatted_report"], re.MULTILINE
    )
    assert found, f"no {label!r} line in the report"
    return found.group(1)


async def fail_a_tool_call(error: BaseException, tool_name: str = "ha_probe") -> None:
    """Record a failed tool call the way every tool records one."""

    async def probe() -> None:
        raise error

    probe.__name__ = tool_name
    with pytest.raises(type(error)):
        await log_tool_usage(probe)()
