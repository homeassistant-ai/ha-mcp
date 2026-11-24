"""tool_error_ha_code and ha_error_code_field: Core's code, and nothing else.

Callers branch on the code (a dashboard with no stored config, an unknown
command, a bulk row's ha_error_code), so a body that does not carry one as a
string must read as no code at all, never as some code.
"""

import json

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools.helpers import ha_error_code_field, tool_error_ha_code


def test_the_code_core_gave_is_read() -> None:
    body = {"success": False, "error": {"code": "X"}, "ha_error_code": "not_found"}
    assert tool_error_ha_code(ToolError(json.dumps(body))) == "not_found"


@pytest.mark.parametrize(
    "text",
    [
        "Command failed: not_found",
        json.dumps(["not_found"]),
        json.dumps({"ha_error_code": 404}),
        json.dumps({"error": {"code": "X"}}),
    ],
    ids=["not json", "not an object", "not a string", "no code"],
)
def test_a_body_without_a_string_code_has_none(text: str) -> None:
    assert tool_error_ha_code(ToolError(text)) is None


def test_a_failure_row_keeps_cores_code() -> None:
    assert ha_error_code_field("invalid_format") == {"ha_error_code": "invalid_format"}


@pytest.mark.parametrize("code", [None, ""])
def test_a_failure_row_without_a_code_gets_no_key(code: object) -> None:
    """A failure without a string code from Core (a lost connection, a fault
    on this side) gets no key rather than null."""
    assert ha_error_code_field(code) == {}
