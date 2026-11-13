"""Reads the body and the error object out of a raised ToolError."""

from __future__ import annotations

import json
from typing import Any

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError


def _error(exc_info: pytest.ExceptionInfo[ToolError]) -> dict[str, Any]:
    error: dict[str, Any] = json.loads(str(exc_info.value))["error"]
    return error


def error_body(exc: pytest.ExceptionInfo[ToolError]) -> dict[str, Any]:
    """The structured body of a tool's rejection."""
    body: dict[str, Any] = json.loads(str(exc.value))
    return body
