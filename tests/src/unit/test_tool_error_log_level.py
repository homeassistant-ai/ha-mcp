"""ha-mcp's ToolErrors are user-facing failures and must not log at ERROR."""

import ast
import json
import logging
from pathlib import Path

import pytest

from ha_mcp._vendor.fastmcp import Client, FastMCP
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.errors import TOOL_ERROR_LOG_LEVEL, ErrorCode, create_error_response
from ha_mcp.tools.helpers import raise_tool_error
from ha_mcp.tools.util_helpers import augment_tool_error_with_skill_content

_SRC = Path(__file__).resolve().parents[3] / "src" / "ha_mcp"


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.mark.asyncio
async def test_tool_errors_log_at_warning_and_crashes_at_error():
    mcp = FastMCP("probe")

    @mcp.tool
    def refuse() -> str:
        raise_tool_error(
            create_error_response(ErrorCode.ENTITY_NOT_FOUND, "light.x not found")
        )

    @mcp.tool
    def typed(count: int) -> int:
        return count

    @mcp.tool
    def crash() -> str:
        raise RuntimeError("bug")

    logger = logging.getLogger("fastmcp.server.server")
    capture = _Capture()
    saved_level = logger.level
    logger.addHandler(capture)
    logger.setLevel(logging.DEBUG)
    try:
        async with Client(mcp) as client:
            await client.call_tool("refuse", {}, raise_on_error=False)
            await client.call_tool("typed", {"count": "many"}, raise_on_error=False)
            await client.call_tool("crash", {}, raise_on_error=False)
    finally:
        logger.removeHandler(capture)
        logger.setLevel(saved_level)

    levels = {record.getMessage(): record.levelno for record in capture.records}
    assert levels["Error calling tool 'refuse'"] == logging.WARNING
    assert levels["Error calling tool 'crash'"] == logging.ERROR
    invalid = [
        msg for msg in levels if msg.startswith("Invalid arguments for tool 'typed'")
    ]
    assert invalid and levels[invalid[0]] == logging.WARNING


def test_augmented_tool_error_keeps_its_log_level():
    error = ToolError(
        json.dumps({"success": False, "error": {"suggestions": []}}),
        log_level=TOOL_ERROR_LOG_LEVEL,
    )
    assert (
        augment_tool_error_with_skill_content(error).log_level == TOOL_ERROR_LOG_LEVEL
    )


def test_every_tool_error_ha_mcp_constructs_sets_its_log_level():
    offenders = [
        f"{path.relative_to(_SRC)}:{node.lineno}"
        for path in _SRC.rglob("*.py")
        if "_vendor" not in path.relative_to(_SRC).parts
        for node in ast.walk(ast.parse(path.read_text("utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ToolError"
        and not any(kw.arg == "log_level" for kw in node.keywords)
    ]
    assert not offenders, f"ToolError without log_level logs at ERROR: {offenders}"
