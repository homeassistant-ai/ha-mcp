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


def test_title_falls_back_when_envelope_has_no_message() -> None:
    envelope = json.dumps({"success": False, "error": "boom"})
    logs = [{"tool_name": "ha_call_service", "error_message": envelope}]
    assert _generate_bug_title({}, logs).startswith("ha_call_service: {")


def test_title_scrubs_secrets() -> None:
    envelope = json.dumps({"error": {"message": "bad token=abc123secret"}})
    logs = [{"tool_name": "ha_call_service", "error_message": envelope}]
    assert "abc123secret" not in _generate_bug_title({}, logs)


def test_title_stays_on_one_line() -> None:
    envelope = json.dumps({"error": {"message": "first line\nsecond line"}})
    logs = [{"tool_name": "x", "error_message": envelope}]
    assert _generate_bug_title({}, logs) == "x: first line second line"
