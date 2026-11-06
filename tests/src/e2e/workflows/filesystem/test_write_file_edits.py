"""ha_write_file(edits=...) against the real edit_file service and its schema."""

from __future__ import annotations

import uuid

import pytest

from ...utilities.assertions import MCPAssertions, safe_call_tool

# edit_file registers only in the "File & YAML Tools" config entry (#2292).
pytestmark = [pytest.mark.requires_tools_entry, pytest.mark.filesystem]


@pytest.fixture
async def card_path(mcp_client_fs):
    path = f"www/test_edits_{uuid.uuid4().hex[:8]}.js"
    yield path
    await safe_call_tool(
        mcp_client_fs, "ha_delete_file", {"path": path, "confirm": True}
    )


async def test_edit_changes_only_the_matched_text_and_keeps_crlf(
    mcp_client_fs, card_path: str
) -> None:
    async with MCPAssertions(mcp_client_fs) as mcp:
        await mcp.call_tool_success(
            "ha_write_file", {"path": card_path, "content": "one\r\ntwo\r\n"}
        )

        edited = await mcp.call_tool_success(
            "ha_write_file",
            {"path": card_path, "edits": [{"old": "two", "new": "TWO"}]},
        )

        # Read back as text, a CRLF file looks like LF; only the size shows the
        # two carriage returns survived.
        assert edited["size"] == len(b"one\r\nTWO\r\n")
        read = await mcp.call_tool_success("ha_read_file", {"path": card_path})
        assert read["content"] == "one\nTWO\n"


async def test_edit_that_cannot_apply_saves_nothing(
    mcp_client_fs, card_path: str
) -> None:
    async with MCPAssertions(mcp_client_fs) as mcp:
        await mcp.call_tool_success(
            "ha_write_file", {"path": card_path, "content": "one\n"}
        )

        await mcp.call_tool_failure(
            "ha_write_file",
            {
                "path": card_path,
                "edits": [
                    {"old": "one", "new": "1"},
                    {"old": "missing", "new": "x"},
                ],
            },
            expected_error="text not found",
        )

        read = await mcp.call_tool_success("ha_read_file", {"path": card_path})
        assert read["content"] == "one\n"


async def test_edit_outside_the_write_allowlist_is_refused(
    mcp_client_fs,
) -> None:
    async with MCPAssertions(mcp_client_fs) as mcp:
        await mcp.call_tool_failure(
            "ha_write_file",
            {
                "path": "configuration.yaml",
                "edits": [{"old": "homeassistant", "new": "x"}],
            },
            expected_error="Write not allowed",
        )
