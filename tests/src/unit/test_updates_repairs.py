"""Repairs issues through ha_manage_updates(action='ignore_repair' / 'unignore_repair')."""

import json
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.read_only import READ_ONLY_EXEMPT_TOOLS
from ha_mcp.tools.tools_updates import UpdateTools


async def _repairs(hass: HomeAssistant, *issue_ids: str) -> ir.IssueRegistry:
    """Core's Repairs with an active ``sun`` issue for each id."""
    assert await async_setup_component(hass, "repairs", {})
    for issue_id in issue_ids:
        ir.async_create_issue(
            hass,
            "sun",
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="test",
        )
    return ir.async_get(hass)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("action", "ignore"), [("ignore_repair", True), ("unignore_repair", False)]
)
async def test_sets_ignored_flag_on_listed_issue(
    hass: HomeAssistant, core_client: HomeAssistantClient, action: str, ignore: bool
) -> None:
    registry = await _repairs(hass, "abc")
    registry.async_ignore("sun", "abc", not ignore)

    with patch.object(
        core_client, "send_websocket_message", wraps=core_client.send_websocket_message
    ) as spy:
        result = await UpdateTools(core_client).ha_manage_updates(
            action=action, repairs=[{"domain": "sun", "issue_id": "abc"}]
        )

    assert result["success"] is True
    assert result["succeeded"] == 1
    assert result["results"] == [
        {"success": True, "domain": "sun", "issue_id": "abc", "ignored": ignore}
    ]
    spy.assert_awaited_with(
        {
            "type": "repairs/ignore_issue",
            "domain": "sun",
            "issue_id": "abc",
            "ignore": ignore,
        }
    )
    issue = registry.async_get_issue("sun", "abc")
    assert issue is not None
    assert (issue.dismissed_version is not None) is ignore


@pytest.mark.unit
async def test_unknown_issue_is_reported_per_item(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await _repairs(hass, "abc")

    with patch.object(
        core_client, "send_websocket_message", wraps=core_client.send_websocket_message
    ) as spy:
        result = await UpdateTools(core_client).ha_manage_updates(
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
    assert spy.await_count == 2


@pytest.mark.unit
async def test_home_assistant_rejection_is_reported_per_item(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Core refuses an issue it does not hold; unignore skips the list check."""
    await _repairs(hass)

    result = await UpdateTools(core_client).ha_manage_updates(
        action="unignore_repair", repairs=[{"domain": "sun", "issue_id": "gone"}]
    )

    assert result["success"] is False
    assert result["results"][0]["error"]["code"] == "SERVICE_CALL_FAILED"
    assert "Issue 'gone' not found" in result["results"][0]["error"]["message"]


@pytest.mark.unit
async def test_unreadable_issue_list_leaves_the_verdict_to_home_assistant(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await _repairs(hass, "abc")

    # Core answers repairs/list_issues with an error when the listing fails;
    # repairs/ignore_issue does not read the hidden flag.
    with patch.object(
        ir.IssueRegistry,
        "async_is_hidden",
        side_effect=HomeAssistantError("listing failed"),
    ):
        listed = await core_client.send_websocket_message(
            {"type": "repairs/list_issues"}
        )
        assert listed["success"] is False

        result = await UpdateTools(core_client).ha_manage_updates(
            action="ignore_repair", repairs=[{"domain": "sun", "issue_id": "abc"}]
        )

    assert result["success"] is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "repairs", [None, [], [{"domain": "sun"}], [{"domain": "sun", "issue_id": 3}]]
)
async def test_malformed_repairs_are_rejected(
    core_client: HomeAssistantClient, repairs: Any
) -> None:
    with (
        patch.object(
            core_client,
            "send_websocket_message",
            wraps=core_client.send_websocket_message,
        ) as spy,
        pytest.raises(ToolError) as exc_info,
    ):
        await UpdateTools(core_client).ha_manage_updates(
            action="ignore_repair", repairs=repairs
        )

    error = json.loads(str(exc_info.value))["error"]
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    spy.assert_not_called()


@pytest.mark.unit
@pytest.mark.parametrize("action", ["ignore_repair", "unignore_repair"])
def test_repair_actions_are_writes_in_read_only_mode(action: str) -> None:
    exemption = READ_ONLY_EXEMPT_TOOLS["ha_manage_updates"]
    assert exemption.blocked_write({"action": action}) is not None


@pytest.mark.unit
async def test_unignore_reaches_issues_missing_from_the_active_list(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    core_client: HomeAssistantClient,
) -> None:
    """An inactive issue is absent from list_issues but still un-ignorable."""
    registry = await _repairs(hass, "inactive")
    ignored = registry.async_ignore("sun", "inactive", True)
    # A restart: Core reloads the stored issue as inactive until its
    # integration raises it again.
    hass_storage[ir.STORAGE_KEY] = {
        "version": ir.STORAGE_VERSION_MAJOR,
        "minor_version": ir.STORAGE_VERSION_MINOR,
        "key": ir.STORAGE_KEY,
        "data": {"issues": [ignored.to_json()]},
    }
    await registry.async_load()
    listed = await core_client.send_websocket_message({"type": "repairs/list_issues"})
    assert listed["result"]["issues"] == []

    with patch.object(
        core_client, "send_websocket_message", wraps=core_client.send_websocket_message
    ) as spy:
        result = await UpdateTools(core_client).ha_manage_updates(
            action="unignore_repair",
            repairs=[{"domain": "sun", "issue_id": "inactive"}],
        )

    assert result["success"] is True
    spy.assert_awaited_once_with(
        {
            "type": "repairs/ignore_issue",
            "domain": "sun",
            "issue_id": "inactive",
            "ignore": False,
        }
    )
    issue = registry.async_get_issue("sun", "inactive")
    assert issue is not None
    assert issue.dismissed_version is None
