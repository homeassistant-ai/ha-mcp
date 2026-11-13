"""Helpers shared by the camera tool unit tests."""

import json

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError


def png_bytes(width: int, height: int) -> bytes:
    """Minimal structurally-valid PNG header with the given dimensions."""
    ihdr_payload = (
        width.to_bytes(4, "big") + height.to_bytes(4, "big") + b"\x08\x06\x00\x00\x00"
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + len(ihdr_payload).to_bytes(4, "big")
        + b"IHDR"
        + ihdr_payload
    )


def error_body(exc_info: pytest.ExceptionInfo[ToolError]) -> dict:
    """Parse the structured error payload carried by a raised ToolError."""
    return json.loads(exc_info.value.args[0])["error"]
