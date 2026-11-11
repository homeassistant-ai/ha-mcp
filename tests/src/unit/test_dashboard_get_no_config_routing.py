"""ha_config_get_dashboard on a dashboard Home Assistant holds no config for (#2632).

Core answers "No config found." (the component: ``ConfigNotFound``) and the
frontend shows its generated default view, so the get answers with
``stored_config`` false instead of failing, through the same envelope as any
other get; an identifier Core does not know is RESOURCE_NOT_FOUND.

The tool runs on the server's own client against a real ``hass`` with a
storage dashboard that has never been saved: ``component_core_client`` also
answers ``ha_mcp_tools/*``, ``core_client`` is an install without the
component, so the tool reads through Core's own ``lovelace/config``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import Mock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_config_dashboards import DashboardConfigTools

# Core requires a hyphen in a dashboard's url_path and derives its internal
# id by slugifying it.
_URL_PATH = "fresh-dash"
_DASHBOARD_ID = "fresh_dash"


async def _fresh_dashboard(hass: HomeAssistant, client: HomeAssistantClient) -> None:
    """A storage dashboard Core knows but holds no saved config for."""
    assert await async_setup_component(hass, "lovelace", {})
    created = await client.send_websocket_message(
        {
            "type": "lovelace/dashboards/create",
            "url_path": _URL_PATH,
            "title": "Fresh",
            "mode": "storage",
        }
    )
    assert created["success"], created
    assert created["result"]["id"] == _DASHBOARD_ID


@contextmanager
def _legacy_reads(client: HomeAssistantClient) -> Iterator[Mock]:
    with patch.object(
        client, "send_websocket_message", wraps=client.send_websocket_message
    ) as spy:
        yield spy


def _config_reads(spy: Mock) -> list[Any]:
    return [
        call.args[0].get("url_path")
        for call in spy.call_args_list
        if call.args[0].get("type") == "lovelace/config"
    ]


def _error(exc: pytest.ExceptionInfo[ToolError]) -> dict[str, Any]:
    error: dict[str, Any] = json.loads(str(exc.value))["error"]
    return error


async def test_component_no_config_answers_without_a_legacy_read(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    await _fresh_dashboard(hass, component_core_client)
    get_dashboard = DashboardConfigTools(component_core_client).ha_config_get_dashboard

    with _legacy_reads(component_core_client) as legacy:
        resp = await get_dashboard(url_path=_URL_PATH)

    assert resp["success"] is True
    assert resp["stored_config"] is False
    assert resp["config"] is None
    assert _config_reads(legacy) == []


async def test_legacy_read_answers_no_config_the_same_way(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await _fresh_dashboard(hass, core_client)

    resp = await DashboardConfigTools(core_client).ha_config_get_dashboard(
        url_path=_URL_PATH
    )

    assert resp["success"] is True
    assert resp["stored_config"] is False


async def test_lazily_resolved_id_keeps_resolved_from(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """An internal id resolved to a dashboard with no config still reports the
    identifier the caller passed."""
    await _fresh_dashboard(hass, core_client)

    resp = await DashboardConfigTools(core_client).ha_config_get_dashboard(
        url_path=_DASHBOARD_ID
    )

    assert resp["url_path"] == _URL_PATH
    assert resp["resolved_from"] == _DASHBOARD_ID
    assert resp["stored_config"] is False


async def test_view_path_on_dashboard_without_config_is_refused(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """There are no static views to scope to, so view_path is not dropped silently."""
    await _fresh_dashboard(hass, component_core_client)
    get_dashboard = DashboardConfigTools(component_core_client).ha_config_get_dashboard

    with pytest.raises(ToolError) as exc:
        await get_dashboard(url_path=_URL_PATH, view_path="kitchen")

    assert _error(exc)["code"] == "VALIDATION_INVALID_PARAMETER"


async def test_screenshot_request_on_dashboard_without_config_is_answered(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """A screenshot request is handled like any get's (here: the feature is
    off), not dropped without a word."""
    await _fresh_dashboard(hass, component_core_client)
    get_dashboard = DashboardConfigTools(component_core_client).ha_config_get_dashboard

    resp = await get_dashboard(url_path=_URL_PATH, include_screenshot=True)

    assert any("Screenshot" in w for w in resp.get("warnings", []))


async def test_card_search_on_dashboard_without_config_says_why(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await _fresh_dashboard(hass, core_client)

    with pytest.raises(ToolError) as exc:
        await DashboardConfigTools(core_client).ha_config_get_dashboard(
            url_path=_URL_PATH, card_type="tile"
        )

    error = _error(exc)
    assert error["code"] == "VALIDATION_FAILED"
    assert "no config" in error["message"]


async def test_card_search_on_unknown_dashboard_is_not_found(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await _fresh_dashboard(hass, core_client)

    with pytest.raises(ToolError) as exc:
        await DashboardConfigTools(core_client).ha_config_get_dashboard(
            url_path="ghost", card_type="tile"
        )

    assert _error(exc)["code"] == "RESOURCE_NOT_FOUND"
