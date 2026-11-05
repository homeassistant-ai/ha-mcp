"""ha_write_file(edits=...) routes to the component's edit_file service."""

import inspect
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools import tools_filesystem
from ha_mcp.tools.tools_filesystem import FilesystemTools

EDITS = [{"old": "30000", "new": "10000"}]


async def _call_tool(
    client: Any, service_result: dict[str, Any] | None = None, **kwargs: Any
) -> tuple[dict[str, Any], AsyncMock]:
    service = AsyncMock(return_value=service_result or {"success": True})
    with (
        patch.object(tools_filesystem, "_assert_mcp_tools_available", AsyncMock()),
        patch.object(tools_filesystem, "call_mcp_tools_service", service),
    ):
        tool = inspect.unwrap(FilesystemTools.ha_write_file)
        try:
            return await tool(FilesystemTools(client), **kwargs), service
        except ToolError as err:
            err.service = service  # type: ignore[attr-defined]
            raise


@pytest.fixture
def client_on_version() -> Any:
    def _client(version: str) -> MagicMock:
        client = MagicMock()
        tools_filesystem._COMPONENT_VERSION_CACHE[client] = version
        tools_filesystem._CALLER_TOKEN_CACHE[client] = "token"
        return client

    return _client


CURRENT = tools_filesystem.MIN_COMPONENT_VERSION_FILE_EDITS


@pytest.mark.asyncio
async def test_edits_reach_the_edit_service_without_a_whole_file_write(
    client_on_version: Any,
) -> None:
    _, service = await _call_tool(
        client_on_version(CURRENT), path="www/card.js", edits=EDITS
    )

    service.assert_awaited_once()
    assert service.await_args.args[1:] == (
        "edit_file",
        {"path": "www/card.js", "edits": EDITS},
    )


@pytest.mark.asyncio
async def test_content_write_works_on_a_component_without_edit_file(
    client_on_version: Any,
) -> None:
    """Whole-file writes must not force a component update."""
    _, service = await _call_tool(
        client_on_version("1.2.0"), path="www/a.css", content="x"
    )

    assert service.await_args.args[1] == "write_file"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "modes",
    [{}, {"content": "x", "edits": EDITS}],
    ids=["neither", "both"],
)
async def test_exactly_one_of_content_and_edits_is_required(
    client_on_version: Any, modes: dict[str, Any]
) -> None:
    with pytest.raises(ToolError) as excinfo:
        await _call_tool(client_on_version(CURRENT), path="www/a.css", **modes)

    assert excinfo.value.service.await_count == 0  # type: ignore[attr-defined]
    assert "VALIDATION_INVALID_PARAMETER" in str(excinfo.value)


@pytest.mark.asyncio
async def test_older_component_gets_an_update_prompt_not_a_service_error(
    client_on_version: Any,
) -> None:
    with (
        patch.object(tools_filesystem, "_fetch_caller_token", AsyncMock()),
        pytest.raises(ToolError) as excinfo,
    ):
        await _call_tool(client_on_version("1.2.0"), path="www/a.css", edits=EDITS)

    assert excinfo.value.service.await_count == 0  # type: ignore[attr-defined]
    assert "COMPONENT_NOT_INSTALLED" in str(excinfo.value)


@pytest.mark.asyncio
async def test_component_refusal_reaches_the_model_with_its_reason(
    client_on_version: Any,
) -> None:
    refusal = {
        "success": False,
        "error": "Edit 0: 'old' text not found in www/a.css. Nothing was saved.",
        "reason": "old_not_found",
        "edit_index": 0,
    }

    with pytest.raises(ToolError) as excinfo:
        await _call_tool(
            client_on_version(CURRENT),
            service_result=refusal,
            path="www/a.css",
            edits=EDITS,
        )

    payload = json.loads(str(excinfo.value))
    assert payload["reason"] == "old_not_found"
    assert payload["edit_index"] == 0
