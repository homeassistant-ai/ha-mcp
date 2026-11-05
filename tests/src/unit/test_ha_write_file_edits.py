"""ha_write_file(edits=...) routes to the component's edit_file service."""

import asyncio
import inspect
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools import auto_backup, config_file_visibility, tools_filesystem
from ha_mcp.tools.tools_filesystem import MCP_TOOLS_DOMAIN, FilesystemTools

EDITS = [{"old": "30000", "new": "10000"}]


def _client(*services: str) -> MagicMock:
    client = MagicMock()
    client.get_services = AsyncMock(
        return_value=[
            {
                "domain": MCP_TOOLS_DOMAIN,
                "services": {service: {} for service in services},
            }
        ]
    )
    return client


async def _call_tool(
    client: Any,
    service_result: dict[str, Any] | None = None,
    refuse: AsyncMock | None = None,
    **kwargs: Any,
) -> tuple[dict[str, Any], AsyncMock]:
    service = AsyncMock(return_value=service_result or {"success": True})
    with (
        patch.object(tools_filesystem, "_assert_mcp_tools_available", AsyncMock()),
        patch.object(tools_filesystem, "call_mcp_tools_service", service),
        patch.object(
            config_file_visibility,
            "refuse_reading_hidden_definitions",
            refuse or AsyncMock(),
        ),
    ):
        tool = inspect.unwrap(FilesystemTools.ha_write_file)
        try:
            return await tool(FilesystemTools(client), **kwargs), service
        except ToolError as err:
            err.service = service  # type: ignore[attr-defined]
            raise


@pytest.mark.asyncio
async def test_edits_reach_the_edit_service_without_a_whole_file_write() -> None:
    _, service = await _call_tool(_client("edit_file"), path="www/card.js", edits=EDITS)

    service.assert_awaited_once()
    assert service.await_args.args[1:] == (
        "edit_file",
        {"path": "www/card.js", "edits": EDITS},
    )


@pytest.mark.asyncio
async def test_content_write_works_on_a_component_without_edit_file() -> None:
    """Whole-file writes must not force a component update."""
    _, service = await _call_tool(_client("write_file"), path="www/a.css", content="x")

    assert service.await_args.args[1] == "write_file"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "modes",
    [{}, {"content": "x", "edits": EDITS}],
    ids=["neither", "both"],
)
async def test_exactly_one_of_content_and_edits_is_required(
    modes: dict[str, Any],
) -> None:
    with pytest.raises(ToolError) as excinfo:
        await _call_tool(_client("edit_file"), path="www/a.css", **modes)

    assert excinfo.value.service.await_count == 0  # type: ignore[attr-defined]
    assert "VALIDATION_INVALID_PARAMETER" in str(excinfo.value)


@pytest.mark.asyncio
async def test_component_without_edit_file_gets_an_update_prompt() -> None:
    """Asks the registry, not a version: an unreleased component reports the
    previous release's number and still has the service."""
    with pytest.raises(ToolError) as excinfo:
        await _call_tool(_client("write_file"), path="www/a.css", edits=EDITS)

    assert excinfo.value.service.await_count == 0  # type: ignore[attr-defined]
    assert "COMPONENT_NOT_INSTALLED" in str(excinfo.value)


@pytest.mark.asyncio
async def test_component_refusal_reaches_the_model_with_its_reason() -> None:
    refusal = {
        "success": False,
        "error": "Edit 0: 'old' text not found in www/a.css. Nothing was saved.",
        "reason": "old_not_found",
        "edit_index": 0,
    }

    with pytest.raises(ToolError) as excinfo:
        await _call_tool(
            _client("edit_file"),
            service_result=refusal,
            path="www/a.css",
            edits=EDITS,
        )

    payload = json.loads(str(excinfo.value))
    assert payload["reason"] == "old_not_found"
    assert payload["edit_index"] == 0


@pytest.mark.asyncio
async def test_edits_are_snapshotted_before_the_file_changes() -> None:
    """An edit overwrites the file, so it must take the mandatory backup even
    though it sends neither content nor overwrite."""
    capture = AsyncMock(return_value=None)
    service = AsyncMock(return_value={"success": True})
    settings = MagicMock(enable_auto_backup=True)

    def backup_taken_first(*_args: Any, **_kwargs: Any) -> dict[str, bool]:
        capture.assert_awaited_once()
        return {"success": True}

    service.side_effect = backup_taken_first
    with (
        patch.object(auto_backup, "get_global_settings", return_value=settings),
        patch.object(auto_backup, "_capture_pre_write_snapshot", capture),
        patch.object(tools_filesystem, "_assert_mcp_tools_available", AsyncMock()),
        patch.object(tools_filesystem, "call_mcp_tools_service", service),
        patch.object(
            config_file_visibility, "refuse_reading_hidden_definitions", AsyncMock()
        ),
    ):
        await FilesystemTools(_client("edit_file")).ha_write_file(
            path="www/a.css", edits=EDITS
        )

    service.assert_awaited_once()
    assert capture.await_args.kwargs["domain"] == "file"
    assert capture.await_args.kwargs["mandatory"] is True


@pytest.mark.asyncio
async def test_parallel_writes_snapshot_and_write_one_at_a_time() -> None:
    """A second snapshot taken before the first write finishes holds the state
    before both, so restoring it would also remove the first edit. The two paths
    could be a link and its target, so the lock can't be keyed by path."""
    events: list[str] = []

    async def capture(*_args: Any, **_kwargs: Any) -> None:
        events.append("snapshot")
        await asyncio.sleep(0)

    async def service(*_args: Any, **_kwargs: Any) -> dict[str, bool]:
        events.append("write")
        await asyncio.sleep(0)
        return {"success": True}

    settings = MagicMock(enable_auto_backup=True)
    with (
        patch.object(auto_backup, "get_global_settings", return_value=settings),
        patch.object(auto_backup, "_capture_pre_write_snapshot", capture),
        patch.object(tools_filesystem, "_assert_mcp_tools_available", AsyncMock()),
        patch.object(tools_filesystem, "call_mcp_tools_service", service),
        patch.object(
            config_file_visibility, "refuse_reading_hidden_definitions", AsyncMock()
        ),
    ):
        tools = FilesystemTools(_client("edit_file"))
        await asyncio.gather(
            tools.ha_write_file(path="www/a.css", edits=EDITS),
            tools.ha_write_file(path="www/link_to_a.css", edits=EDITS),
        )

    assert events == ["snapshot", "write", "snapshot", "write"]


@pytest.mark.asyncio
async def test_edits_cannot_probe_a_file_that_defines_a_hidden_item() -> None:
    """Which edit failed would tell the caller whether a guessed text is in a
    file ha_read_file refuses to show."""
    refuse = AsyncMock(side_effect=ToolError("hidden"))

    with pytest.raises(ToolError) as excinfo:
        await _call_tool(
            _client("edit_file"), refuse=refuse, path="www/a.yaml", edits=EDITS
        )

    refuse.assert_awaited_once()
    assert excinfo.value.service.await_count == 0  # type: ignore[attr-defined]
