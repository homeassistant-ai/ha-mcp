"""ha_write_file(edits=...) against the real edit_file service and its schema."""

from __future__ import annotations

import os
import uuid

import pytest

from ...utilities.assertions import MCPAssertions

# edit_file registers only in the "File & YAML Tools" config entry (#2292).
pytestmark = [pytest.mark.requires_tools_entry, pytest.mark.filesystem]

FEATURE_FLAG = "HAMCP_ENABLE_FILESYSTEM_TOOLS"


@pytest.fixture(scope="module")
def filesystem_tools_enabled(ha_container_with_fresh_config):
    os.environ[FEATURE_FLAG] = "true"
    yield
    os.environ.pop(FEATURE_FLAG, None)


@pytest.fixture
async def filesystem_client(filesystem_tools_enabled, mcp_server, mcp_client):
    """An MCP client whose server was built with the filesystem flag set."""
    if mcp_server is None:
        # Out-of-process servers get the flag at install time; the session
        # client owns its lifecycle.
        yield mcp_client
        return

    from ha_mcp._vendor.fastmcp import Client

    async with Client(mcp_server.mcp) as client:
        yield client


@pytest.fixture
def card_path(cleanup_tracker) -> str:
    path = f"www/test_edits_{uuid.uuid4().hex[:8]}.js"
    cleanup_tracker.track("file", path)
    return path


async def test_edit_changes_only_the_matched_text_and_keeps_crlf(
    filesystem_client, card_path: str
) -> None:
    async with MCPAssertions(filesystem_client) as mcp:
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
    filesystem_client, card_path: str
) -> None:
    async with MCPAssertions(filesystem_client) as mcp:
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
            expected_error="old_not_found",
        )

        read = await mcp.call_tool_success("ha_read_file", {"path": card_path})
        assert read["content"] == "one\n"


async def test_edit_outside_the_write_allowlist_is_refused(
    filesystem_client,
) -> None:
    async with MCPAssertions(filesystem_client) as mcp:
        await mcp.call_tool_failure(
            "ha_write_file",
            {
                "path": "configuration.yaml",
                "edits": [{"old": "homeassistant", "new": "x"}],
            },
            expected_error="Write not allowed",
        )
