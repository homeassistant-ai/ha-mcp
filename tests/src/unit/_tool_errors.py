"""Reads the body and the error object out of a raised ToolError."""

from __future__ import annotations

import json
from collections.abc import Awaitable
from typing import Any

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError


def error_body(exc: pytest.ExceptionInfo[ToolError]) -> dict[str, Any]:
    """The structured body of a tool's rejection."""
    body: dict[str, Any] = json.loads(str(exc.value))
    return body


def error_of(exc: pytest.ExceptionInfo[ToolError]) -> dict[str, Any]:
    """The ``error`` object of a tool's rejection."""
    error: dict[str, Any] = error_body(exc)["error"]
    return error


async def rejected_call_body(call: Awaitable[Any]) -> dict[str, Any]:
    """Await a tool call that must fail and return its rejection body."""
    with pytest.raises(ToolError) as exc_info:
        _ = await call
    return error_body(exc_info)
