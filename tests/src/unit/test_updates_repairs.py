"""Repairs issues through ha_manage_updates(action='ignore_repair' / 'unignore_repair').

The issues are raised and dismissed through Core's issue registry, and every
reply comes from Core over the server's own client.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.read_only import READ_ONLY_EXEMPT_TOOLS
from ha_mcp.tools.tools_updates import UpdateTools

from ._client_spies import sent_messages


@pytest.fixture
async def repairs_loaded(hass: HomeAssistant) -> None:
    """Load the repairs integration, which answers the Repairs WebSocket commands."""
    assert await async_setup_component(hass, "repairs", {})


def _raise(hass: HomeAssistant, domain: str, issue_id: str) -> None:
    ir.async_create_issue(
        hass,
        domain,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="demo_issue",
    )


def _dismissed(hass: HomeAssistant, domain: str, issue_id: str) -> bool:
    issue = ir.async_get(hass).async_get_issue(domain, issue_id)
    assert issue is not None, f"{domain}/{issue_id} is not in the registry"
    return issue.dismissed_version is not None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("action", "start_dismissed", "end_dismissed"),
    [("ignore_repair", False, True), ("unignore_repair", True, False)],
)
async def test_sets_ignored_flag_on_listed_issue(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    repairs_loaded: None,
    action: str,
    start_dismissed: bool,
    end_dismissed: bool,
) -> None:
    _raise(hass, "sun", "abc")
    if start_dismissed:
        ir.async_ignore_issue(hass, "sun", "abc", True)

    result = await UpdateTools(core_client).ha_manage_updates(
        action=action, repairs=[{"domain": "sun", "issue_id": "abc"}]
    )

    assert result["success"] is True
    assert result["succeeded"] == 1
    assert result["results"] == [
        {
            "success": True,
            "domain": "sun",
            "issue_id": "abc",
            "ignored": end_dismissed,
        }
    ]
    assert _dismissed(hass, "sun", "abc") is end_dismissed


@pytest.mark.unit
async def test_unknown_issue_is_reported_per_item(
    hass: HomeAssistant, core_client: HomeAssistantClient, repairs_loaded: None
) -> None:
    _raise(hass, "sun", "abc")

    with sent_messages(core_client) as sent:
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
    assert ir.async_get(hass).async_get_issue("sun", "missing") is None
    assert _dismissed(hass, "sun", "abc") is True
    # The Repairs list plus the one known issue; the unknown one is never sent.
    assert sent.await_count == 2


@pytest.mark.unit
async def test_home_assistant_rejection_is_reported_per_item(
    hass: HomeAssistant, core_client: HomeAssistantClient, repairs_loaded: None
) -> None:
    """Core refuses to un-ignore an issue its registry no longer holds."""
    result = await UpdateTools(core_client).ha_manage_updates(
        action="unignore_repair", repairs=[{"domain": "sun", "issue_id": "gone"}]
    )

    assert result["success"] is False
    assert result["results"][0]["error"]["code"] == "SERVICE_CALL_FAILED"
    assert "'gone' not found" in result["results"][0]["error"]["message"]
    assert ir.async_get(hass).async_get_issue("sun", "gone") is None


@pytest.mark.unit
async def test_ignore_without_repairs_integration_is_rejected_by_core(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """With no repairs integration Core has no Repairs commands, so the
    issue list and the ignore are both refused and the refusal is reported."""
    _raise(hass, "sun", "abc")

    result = await UpdateTools(core_client).ha_manage_updates(
        action="ignore_repair", repairs=[{"domain": "sun", "issue_id": "abc"}]
    )

    assert result["success"] is False
    assert result["results"][0]["error"]["code"] == "SERVICE_CALL_FAILED"
    assert "Unknown command" in result["results"][0]["error"]["message"]
    assert _dismissed(hass, "sun", "abc") is False


@pytest.mark.unit
@pytest.mark.parametrize(
    "repairs", [None, [], [{"domain": "sun"}], [{"domain": "sun", "issue_id": 3}]]
)
async def test_malformed_repairs_are_rejected(
    core_client: HomeAssistantClient, repairs: Any
) -> None:
    with sent_messages(core_client) as sent, pytest.raises(ToolError) as exc_info:
        await UpdateTools(core_client).ha_manage_updates(
            action="ignore_repair", repairs=repairs
        )

    error = json.loads(str(exc_info.value))["error"]
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    sent.assert_not_called()


@pytest.mark.unit
@pytest.mark.parametrize("action", ["ignore_repair", "unignore_repair"])
def test_repair_actions_are_writes_in_read_only_mode(action: str) -> None:
    exemption = READ_ONLY_EXEMPT_TOOLS["ha_manage_updates"]
    assert exemption.blocked_write({"action": action}) is not None


@pytest.mark.unit
async def test_unignore_reaches_issues_missing_from_the_active_list(
    hass: HomeAssistant, core_client: HomeAssistantClient, repairs_loaded: None
) -> None:
    """An inactive issue is absent from list_issues but still un-ignorable."""
    _raise(hass, "sun", "inactive")
    ir.async_ignore_issue(hass, "sun", "inactive", True)
    # A restart keeps each non-persistent issue as an inactive stub.
    hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
    await hass.async_block_till_done()
    await ir.async_load(hass)
    stub = ir.async_get(hass).async_get_issue("sun", "inactive")
    assert stub is not None
    assert stub.active is False

    result = await UpdateTools(core_client).ha_manage_updates(
        action="unignore_repair", repairs=[{"domain": "sun", "issue_id": "inactive"}]
    )

    assert result["success"] is True
    assert _dismissed(hass, "sun", "inactive") is False
