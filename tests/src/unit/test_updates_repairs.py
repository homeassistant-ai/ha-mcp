"""Repairs issues through ha_manage_updates(action='ignore_repair' / 'unignore_repair')."""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.read_only import READ_ONLY_EXEMPT_TOOLS
from ha_mcp.tools.tools_updates import UpdateTools

_LISTED = {
    "success": True,
    "result": {"issues": [{"domain": "sun", "issue_id": "abc", "ignored": False}]},
}


def _client(*replies: dict[str, Any]) -> MagicMock:
    client = MagicMock()
    client.send_websocket_message = AsyncMock(side_effect=list(replies))
    return client


@pytest.mark.unit
@pytest.mark.parametrize(
    ("action", "ignore"), [("ignore_repair", True), ("unignore_repair", False)]
)
async def test_sets_ignored_flag_on_listed_issue(action: str, ignore: bool) -> None:
    ok = {"success": True, "result": None}
    client = _client(_LISTED, ok) if ignore else _client(ok)

    result = await UpdateTools(client).ha_manage_updates(
        action=action, repairs=[{"domain": "sun", "issue_id": "abc"}]
    )

    assert result["success"] is True
    assert result["succeeded"] == 1
    assert result["results"] == [
        {"success": True, "domain": "sun", "issue_id": "abc", "ignored": ignore}
    ]
    client.send_websocket_message.assert_awaited_with(
        {
            "type": "repairs/ignore_issue",
            "domain": "sun",
            "issue_id": "abc",
            "ignore": ignore,
        }
    )


@pytest.mark.unit
async def test_unknown_issue_is_reported_per_item() -> None:
    client = _client(_LISTED, {"success": True, "result": None})

    result = await UpdateTools(client).ha_manage_updates(
        action="ignore_repair",
        repairs=[
            {"domain": "sun", "issue_id": "missing"},
            {"domain": "sun", "issue_id": "abc"},
        ],
    )

    assert result["success"] is False
    assert result["succeeded"] == 1
    assert result["failed"] == 1
    assert result["results"][0]["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert client.send_websocket_message.await_count == 2


@pytest.mark.unit
async def test_home_assistant_rejection_is_reported_per_item() -> None:
    client = _client(
        _LISTED,
        {
            "success": False,
            "error": "Command failed: boom",
            "error_code": "unknown_error",
        },
    )

    result = await UpdateTools(client).ha_manage_updates(
        action="ignore_repair", repairs=[{"domain": "sun", "issue_id": "abc"}]
    )

    assert result["success"] is False
    assert result["results"][0]["error"]["code"] == "SERVICE_CALL_FAILED"
    assert "boom" in result["results"][0]["error"]["message"]


@pytest.mark.unit
async def test_unreadable_issue_list_leaves_the_verdict_to_home_assistant() -> None:
    client = _client({"success": False}, {"success": True, "result": None})

    result = await UpdateTools(client).ha_manage_updates(
        action="ignore_repair", repairs=[{"domain": "sun", "issue_id": "abc"}]
    )

    assert result["success"] is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "repairs", [None, [], [{"domain": "sun"}], [{"domain": "sun", "issue_id": 3}]]
)
async def test_malformed_repairs_are_rejected(repairs: Any) -> None:
    client = _client()

    with pytest.raises(ToolError) as exc_info:
        await UpdateTools(client).ha_manage_updates(
            action="ignore_repair", repairs=repairs
        )

    error = json.loads(str(exc_info.value))["error"]
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    client.send_websocket_message.assert_not_called()


@pytest.mark.unit
@pytest.mark.parametrize("action", ["ignore_repair", "unignore_repair"])
def test_repair_actions_are_writes_in_read_only_mode(action: str) -> None:
    exemption = READ_ONLY_EXEMPT_TOOLS["ha_manage_updates"]
    assert exemption.blocked_write({"action": action}) is not None


@pytest.mark.unit
async def test_unignore_reaches_issues_missing_from_the_active_list() -> None:
    """An inactive issue is absent from list_issues but still un-ignorable."""
    client = _client({"success": True, "result": None})

    result = await UpdateTools(client).ha_manage_updates(
        action="unignore_repair", repairs=[{"domain": "sun", "issue_id": "inactive"}]
    )

    assert result["success"] is True
    client.send_websocket_message.assert_awaited_once_with(
        {
            "type": "repairs/ignore_issue",
            "domain": "sun",
            "issue_id": "inactive",
            "ignore": False,
        }
    )
