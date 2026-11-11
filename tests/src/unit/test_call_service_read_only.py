"""Call Service is unavailable in read-only mode, including direct dispatch."""

import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import async_capture_events

from ha_mcp._vendor.fastmcp import Client, FastMCP
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.config import get_global_settings
from ha_mcp.read_only import ReadOnlyMiddleware, ReadOnlyToolsTransform
from ha_mcp.tools.tools_service import ServiceTools, register_service_tools
from ha_mcp.transforms.categorized_search import CategorizedSearchTransform

ISSUE = ("sun", "test")


@pytest.fixture
async def service_tools(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> ServiceTools:
    """Service tools on the real client, with a Core repairs issue to act on."""
    assert await async_setup_component(hass, "repairs", {})
    assert await async_setup_component(hass, "persistent_notification", {})
    ir.async_create_issue(
        hass,
        *ISSUE,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="test",
    )
    return ServiceTools(core_client, MagicMock())


def issue_ignored(hass: HomeAssistant) -> bool:
    issue = ir.async_get(hass).async_get_issue(*ISSUE)
    assert issue is not None
    return issue.dismissed_version is not None


def assert_core_untouched(hass: HomeAssistant, calls: list[Event]) -> None:
    assert calls == []
    assert not issue_ignored(hass)


@pytest.mark.parametrize(
    "arguments",
    [
        {
            "domain": "persistent_notification",
            "service": "create",
            "data": {"message": "read-only regression"},
        },
        {"domain": "weather", "service": "get_forecasts", "return_response": True},
        {
            "ws_command": "repairs/ignore_issue",
            "data": {"domain": "sun", "issue_id": "test", "ignore": True},
        },
        {"ws_command": "repairs/list_issues"},
        {},
    ],
)
async def test_direct_dispatch_blocks_every_mode(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    service_tools: ServiceTools,
    arguments: dict[str, Any],
):
    """Removing the tool-body guard must fail even without MCP middleware."""
    calls = async_capture_events(hass, EVENT_CALL_SERVICE)
    monkeypatch.setattr(get_global_settings(), "read_only_mode", True)
    with pytest.raises(ToolError) as exc:
        await service_tools.ha_call_service(**arguments)
    body = json.loads(str(exc.value))
    assert body["error"]["code"] == "READ_ONLY_MODE"
    assert body["tool_name"] == "ha_call_service"
    assert_core_untouched(hass, calls)


async def test_direct_dispatch_observes_live_toggle(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, service_tools: ServiceTools
):
    settings = get_global_settings()
    monkeypatch.setattr(settings, "read_only_mode", False)
    first = await service_tools.ha_call_service(ws_command="repairs/list_issues")
    assert first["success"]
    assert [i["issue_id"] for i in first["result"]["issues"]] == [ISSUE[1]]
    settings.read_only_mode = True
    with pytest.raises(ToolError, match="READ_ONLY_MODE"):
        await service_tools.ha_call_service(ws_command="repairs/list_issues")
    settings.read_only_mode = False
    again = await service_tools.ha_call_service(ws_command="repairs/list_issues")
    assert again["success"]
    assert again["result"] == first["result"]


@pytest.mark.parametrize("tool_search", [False, True])
async def test_registered_tool_hidden_and_calls_rejected(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    core_client: HomeAssistantClient,
    service_tools: ServiceTools,
    tool_search: bool,
):
    """Exercise real tool metadata, filtering, and middleware with an MCP client."""
    settings = get_global_settings()
    monkeypatch.setattr(settings, "read_only_mode", False)
    calls = async_capture_events(hass, EVENT_CALL_SERVICE)
    mcp = FastMCP("call-service-read-only-test")
    register_service_tools(mcp, core_client, device_tools=MagicMock())
    mcp.add_transform(ReadOnlyToolsTransform())
    mcp.add_middleware(ReadOnlyMiddleware(list_tools=mcp.local_provider.list_tools))
    if tool_search:
        mcp.add_transform(
            CategorizedSearchTransform(always_visible=["ha_call_service"])
        )
    async with Client(mcp) as client:
        assert "ha_call_service" in {t.name for t in await client.list_tools()}
        settings.read_only_mode = True
        assert "ha_call_service" not in {t.name for t in await client.list_tools()}
        with pytest.raises(ToolError, match="READ_ONLY_MODE"):
            await client.call_tool(
                "ha_call_service", {"ws_command": "repairs/list_issues"}
            )
        if tool_search:
            for proxy in (
                "ha_call_read_tool",
                "ha_call_write_tool",
                "ha_call_delete_tool",
            ):
                with pytest.raises(ToolError, match="READ_ONLY_MODE"):
                    await client.call_tool(
                        proxy,
                        {
                            "name": "ha_call_service",
                            "arguments": {
                                "domain": "persistent_notification",
                                "service": "create",
                                "data": {"message": "read-only regression"},
                            },
                        },
                    )
        settings.read_only_mode = False
        assert "ha_call_service" in {t.name for t in await client.list_tools()}
    assert_core_untouched(hass, calls)
