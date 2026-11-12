"""ha_report_issue's suggested title uses the error message, not the raw JSON envelope (issue #2548).

Each test fails a tool call the way any tool records its failure, then asks
``ha_report_issue`` on the server's own client for the issue title.
"""

from __future__ import annotations

import asyncio
import json

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient

from ._report_issue import fail_a_tool_call, report_issue


async def _title(client: HomeAssistantClient) -> str:
    report = await report_issue(client, fields="issue_title")
    return str(report["issue_title"])


async def test_title_uses_structured_error_message(
    core_client: HomeAssistantClient,
) -> None:
    envelope = json.dumps(
        {"success": False, "error": {"code": "X", "message": "Entity not found"}}
    )
    await fail_a_tool_call(ToolError(envelope))

    assert (await _title(core_client)).endswith("ha_probe: Entity not found")


async def test_title_keeps_plain_error_text(core_client: HomeAssistantClient) -> None:
    # A cancelled call records the exception's class name as its error.
    await fail_a_tool_call(asyncio.CancelledError())

    assert (await _title(core_client)).endswith("ha_probe: CancelledError")


async def test_title_falls_back_when_envelope_has_no_message(
    core_client: HomeAssistantClient,
) -> None:
    await fail_a_tool_call(ToolError(json.dumps({"success": False, "error": "boom"})))

    assert "ha_probe: {" in await _title(core_client)


async def test_title_scrubs_secrets(core_client: HomeAssistantClient) -> None:
    envelope = json.dumps({"error": {"message": "bad token=abc123secret"}})
    await fail_a_tool_call(ToolError(envelope))

    assert "abc123secret" not in await _title(core_client)


async def test_title_stays_on_one_line(core_client: HomeAssistantClient) -> None:
    envelope = json.dumps({"error": {"message": "first line\nsecond line"}})
    await fail_a_tool_call(ToolError(envelope))

    assert (await _title(core_client)).endswith("ha_probe: first line second line")
