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


@pytest.fixture
async def written_paths(mcp_client_fs):
    paths: list[str] = []
    yield paths
    for path in paths:
        await safe_call_tool(
            mcp_client_fs, "ha_delete_file", {"path": path, "confirm": True}
        )


async def test_theme_write_and_edit_reload_themes(
    mcp_client_fs, written_paths: list[str]
) -> None:
    name = f"e2e_{uuid.uuid4().hex[:8]}"
    path = f"themes/{name}.yaml"
    written_paths.append(path)
    async with MCPAssertions(mcp_client_fs) as mcp:
        written = await mcp.call_tool_success(
            "ha_write_file",
            {"path": path, "content": f"{name}:\n  primary-color: red\n"},
        )
        edited = await mcp.call_tool_success(
            "ha_write_file",
            {"path": path, "edits": [{"old": "red", "new": "blue"}]},
        )

    for result in (written, edited):
        assert result["post_action"] == "reload_performed"
        assert result["reload_service"] == "frontend.reload_themes"


async def test_custom_template_core_skips_is_flagged(
    mcp_client_fs, written_paths: list[str]
) -> None:
    stem = f"custom_templates/e2e_{uuid.uuid4().hex[:8]}"
    written_paths.extend([f"{stem}.txt", f"{stem}.jinja"])
    async with MCPAssertions(mcp_client_fs) as mcp:
        skipped = await mcp.call_tool_success(
            "ha_write_file",
            {"path": f"{stem}.txt", "content": "{% macro x() %}{% endmacro %}"},
        )
        loaded = await mcp.call_tool_success(
            "ha_write_file",
            {"path": f"{stem}.jinja", "content": "{% macro x() %}{% endmacro %}"},
        )

    assert "*.jinja" in skipped["warnings"][0]
    assert "post_action" not in skipped
    assert loaded["reload_service"] == "homeassistant.reload_custom_templates"
    assert loaded["post_action"] == "reload_performed"
