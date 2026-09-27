"""ha_report_issue's suggested title uses the error message, not the raw JSON envelope (issue #2548)."""

from __future__ import annotations

import json

from ha_mcp.tools.tools_bug_report import _generate_bug_title


def test_title_uses_structured_error_message() -> None:
    envelope = json.dumps(
        {"success": False, "error": {"code": "X", "message": "Entity not found"}}
    )
    logs = [{"tool_name": "ha_call_service", "error_message": envelope}]
    assert _generate_bug_title({}, logs) == "ha_call_service: Entity not found"


def test_title_keeps_plain_error_text() -> None:
    logs = [{"tool_name": "ha_get_state", "error_message": "CancelledError"}]
    assert _generate_bug_title({}, logs) == "ha_get_state: CancelledError"
