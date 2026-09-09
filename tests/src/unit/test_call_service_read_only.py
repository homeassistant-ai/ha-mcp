"""Call Service is unavailable in read-only mode, including direct dispatch."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from ha_mcp.config import get_global_settings
from ha_mcp.read_only import ReadOnlyMiddleware, ReadOnlyToolsTransform
from ha_mcp.tools.tools_service import ServiceTools, register_service_tools
from ha_mcp.transforms.categorized_search import CategorizedSearchTransform


@pytest.fixture
def service_tools():
    client = MagicMock()
    client.call_service = AsyncMock(return_value=[])
    client.send_websocket_message = AsyncMock(return_value={"success": True})
    return ServiceTools(client, MagicMock())


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
async def test_direct_dispatch_blocks_every_mode(monkeypatch, service_tools, arguments):
    """Removing the tool-body guard must fail even without MCP middleware."""
    monkeypatch.setattr(get_global_settings(), "read_only_mode", True)
    with pytest.raises(ToolError) as exc:
        await service_tools.ha_call_service(**arguments)
    body = json.loads(str(exc.value))
    assert body["error"]["code"] == "READ_ONLY_MODE"
    assert body["tool_name"] == "ha_call_service"
    service_tools._client.call_service.assert_not_awaited()
    service_tools._client.send_websocket_message.assert_not_awaited()


async def test_direct_dispatch_observes_live_toggle(monkeypatch, service_tools):
    settings = get_global_settings()
    monkeypatch.setattr(settings, "read_only_mode", False)
    assert (await service_tools.ha_call_service(ws_command="repairs/list_issues"))[
        "success"
    ]
    settings.read_only_mode = True
    with pytest.raises(ToolError, match="READ_ONLY_MODE"):
        await service_tools.ha_call_service(ws_command="repairs/list_issues")
    settings.read_only_mode = False
    assert (await service_tools.ha_call_service(ws_command="repairs/list_issues"))[
        "success"
    ]
    assert service_tools._client.send_websocket_message.await_count == 2


@pytest.mark.parametrize("tool_search", [False, True])
async def test_registered_tool_hidden_and_calls_rejected(
    monkeypatch, service_tools, tool_search
):
    """Exercise real tool metadata, filtering, and middleware with an MCP client."""
    settings = get_global_settings()
    monkeypatch.setattr(settings, "read_only_mode", False)
    mcp = FastMCP("call-service-read-only-test")
    register_service_tools(mcp, service_tools._client, device_tools=MagicMock())
    mcp.add_transform(ReadOnlyToolsTransform())
    mcp.add_middleware(ReadOnlyMiddleware(list_tools=mcp.local_provider._list_tools))
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
    service_tools._client.call_service.assert_not_awaited()
    service_tools._client.send_websocket_message.assert_not_awaited()
