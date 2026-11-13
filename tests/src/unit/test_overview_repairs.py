"""``ha_get_overview`` repairs: dismissed ones filtered, dismissal state shown.

Regression coverage for issue #1307: dismissed repairs are filtered by default
(matching the Home Assistant Repairs UI), and each repair carries its dismissal
state so a caller that opts in can tell active from ignored ones. The issues
are raised and ignored through Core's issue registry and read back by the tool
over the server's own client.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE, __version__
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.smart_search import SmartSearchTools
from ha_mcp.tools.tools_search import SearchTools


def _tools(client: HomeAssistantClient) -> SearchTools:
    return SearchTools(client, SmartSearchTools(client=client))


@pytest.fixture
async def repairs(hass: HomeAssistant, core_client: HomeAssistantClient) -> None:
    """Load the integrations that answer the overview's reads."""
    assert await async_setup_component(hass, "config", {})
    assert await async_setup_component(hass, "repairs", {})
    assert await async_setup_component(hass, "persistent_notification", {})


def _raise(
    hass: HomeAssistant,
    issue_id: str,
    *,
    ignored: bool = False,
    fixable: bool = True,
    persistent: bool = False,
) -> None:
    ir.async_create_issue(
        hass,
        "demo",
        issue_id,
        is_fixable=fixable,
        is_persistent=persistent,
        severity=ir.IssueSeverity.WARNING,
        translation_key="demo_issue",
    )
    if ignored:
        ir.async_ignore_issue(hass, "demo", issue_id, True)


async def _overview(client: HomeAssistantClient, **kwargs: Any) -> dict[str, Any]:
    return await _tools(client).ha_get_overview(detail_level="minimal", **kwargs)


def _ids(result: dict[str, Any]) -> set[str]:
    return {repair["issue_id"] for repair in result["repairs"]}


async def test_dismissed_repairs_are_left_out_and_counted_apart(
    hass: HomeAssistant, core_client: HomeAssistantClient, repairs: None
) -> None:
    """By default only active repairs are listed and counted; the dismissed
    ones are reported as a separate count so an agent can opt in."""
    _raise(hass, "active_one")
    _raise(hass, "active_unfixable", fixable=False)
    _raise(hass, "dismissed_one", ignored=True)
    _raise(hass, "dismissed_two", ignored=True, fixable=False)

    result = await _overview(core_client)

    assert result["repair_count"] == 2
    assert _ids(result) == {"active_one", "active_unfixable"}
    assert result["dismissed_repair_count"] == 2


async def test_include_dismissed_lists_every_repair_without_separate_count(
    hass: HomeAssistant, core_client: HomeAssistantClient, repairs: None
) -> None:
    _raise(hass, "active_one")
    _raise(hass, "dismissed_one", ignored=True)

    result = await _overview(core_client, include_dismissed_repairs=True)

    assert result["repair_count"] == 2
    assert "dismissed_repair_count" not in result
    assert _ids(result) == {"active_one", "dismissed_one"}


async def test_each_repair_shows_its_dismissal_state(
    hass: HomeAssistant, core_client: HomeAssistantClient, repairs: None
) -> None:
    """A caller that opts in can tell an ignored repair from an active one."""
    _raise(hass, "active_one")
    _raise(hass, "dismissed_one", ignored=True)

    result = await _overview(core_client, include_dismissed_repairs=True)

    for entry in result["repairs"]:
        for field in (
            "issue_id",
            "domain",
            "severity",
            "translation_key",
            "ignored",
            "dismissed_version",
            "is_fixable",
        ):
            assert field in entry, f"Missing field {field} in repair entry"
    by_id = {r["issue_id"]: r for r in result["repairs"]}
    assert by_id["active_one"]["ignored"] is False
    assert by_id["active_one"]["dismissed_version"] is None
    assert by_id["dismissed_one"]["ignored"] is True
    # Core records the running version as the one the repair was dismissed in.
    assert by_id["dismissed_one"]["dismissed_version"] == __version__


async def test_repairs_left_inactive_by_a_restart_are_not_counted(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    repairs: None,
) -> None:
    """After a restart Core keeps each non-persistent issue as an inactive
    stub until its integration raises it again. Served by the component, the
    overview neither lists nor counts them, dismissed or not."""
    _raise(hass, "active_one", persistent=True)
    _raise(hass, "dismissed_one", ignored=True, persistent=True)
    _raise(hass, "ghost_restart_required")
    _raise(hass, "ghost_dismissed", ignored=True)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
    await hass.async_block_till_done()
    await ir.async_load(hass)
    registry = ir.async_get(hass)
    assert {key[1] for key, issue in registry.issues.items() if not issue.active} == {
        "ghost_restart_required",
        "ghost_dismissed",
    }

    result = await _overview(component_core_client)

    assert not any("legacy path" in w for w in result.get("warnings", []))
    assert result["repair_count"] == 1
    assert _ids(result) == {"active_one"}
    assert result["dismissed_repair_count"] == 1

    result = await _overview(component_core_client, include_dismissed_repairs=True)

    assert result["repair_count"] == 2
    assert _ids(result) == {"active_one", "dismissed_one"}


async def test_no_repairs_yields_zero_count_and_empty_list(
    core_client: HomeAssistantClient, repairs: None
) -> None:
    """``repairs`` is always present, so ``fields=["repairs"]`` never trips
    the projection's unknown-key warning on a clean instance."""
    result = await _overview(core_client)

    assert result["repair_count"] == 0
    assert result["repairs"] == []
    assert "dismissed_repair_count" not in result


async def test_only_dismissed_repairs_report_none_active(
    hass: HomeAssistant, core_client: HomeAssistantClient, repairs: None
) -> None:
    for i in range(3):
        _raise(hass, f"dismissed_{i}", ignored=True)

    result = await _overview(core_client)

    assert result["repair_count"] == 0
    assert result["repairs"] == []
    assert result["dismissed_repair_count"] == 3


async def test_unanswered_repairs_read_keeps_overview_with_error(
    hass: HomeAssistant, core_client: HomeAssistantClient, repairs: None
) -> None:
    """With no WebSocket answer the overview still returns, with
    ``repairs_error`` naming the failure and an empty ``repairs`` list."""
    _raise(hass, "active_one")

    with patch.object(
        core_client,
        "send_websocket_message",
        side_effect=HomeAssistantConnectionError("ws disconnect"),
    ):
        result = await _overview(core_client)

    assert result["repair_count"] == 0
    assert result["repairs"] == []
    assert "ws disconnect" in result["repairs_error"]


async def test_rejected_repairs_read_surfaces_core_error(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Without the repairs integration Core rejects ``repairs/list_issues``;
    the overview says so instead of reporting zero repairs as if clean."""
    assert await async_setup_component(hass, "config", {})

    result = await _overview(core_client)

    assert result["repair_count"] == 0
    assert result["repairs"] == []
    assert "Unknown command" in result["repairs_error"]
