"""The readonly URL applies existing enforcement without changing other clients."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import fastmcp
import pytest
from starlette.testclient import TestClient

from ha_mcp.config import get_global_settings, reset_global_settings
from ha_mcp.http_transport import HttpTransportFastMCP
from ha_mcp.read_only import ReadOnlyMiddleware, ReadOnlyToolsTransform
from ha_mcp.utils.data_paths import get_data_dir


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("HOMEASSISTANT_URL", "http://localhost:8123")
    monkeypatch.setenv("HOMEASSISTANT_TOKEN", "test-token")
    monkeypatch.setenv("READ_ONLY_MODE", "false")
    monkeypatch.setattr(fastmcp.settings, "http_host_origin_protection", False)
    get_data_dir.cache_clear()
    reset_global_settings()
    yield
    reset_global_settings()
    get_data_dir.cache_clear()


def _server():
    mcp = HttpTransportFastMCP("readonly endpoint")
    writes = []

    @mcp.tool(annotations={"readOnlyHint": True})
    def read() -> str:
        return "read succeeded"

    @mcp.tool(annotations={"readOnlyHint": False})
    def write() -> str:
        writes.append("write")
        return "write succeeded"

    mcp.add_transform(ReadOnlyToolsTransform())
    mcp.add_middleware(ReadOnlyMiddleware(list_tools=mcp.local_provider._list_tools))
    return mcp, writes


def _rpc(client, path, method, params=None):
    response = client.post(
        path,
        headers={"Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )
    assert response.status_code == 200, response.text
    if response.headers["content-type"].startswith("application/json"):
        return response.json()
    return next(
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    )


@pytest.mark.parametrize("path", ["/private-secret", "/private-secret/"])
@pytest.mark.parametrize("json_response", [False, True])
def test_readonly_endpoint_blocks_writes_without_changing_normal_endpoint(
    path, json_response
):
    mcp, writes = _server()
    app = mcp.http_app(path=path, stateless_http=True, json_response=json_response)
    readonly_path = "/private-secret/readonly"
    with TestClient(app) as client:
        for endpoint, expected in (
            (readonly_path, {"read"}),
            (path, {"read", "write"}),
        ):
            catalog = _rpc(client, endpoint, "tools/list")
            assert {tool["name"] for tool in catalog["result"]["tools"]} == expected
        read = _rpc(client, readonly_path, "tools/call", {"name": "read"})
        assert read["result"]["content"][0]["text"] == "read succeeded"
        blocked = _rpc(client, readonly_path, "tools/call", {"name": "write"})
        assert blocked["result"]["isError"] is True
        assert "READ_ONLY_MODE" in str(blocked)
        assert writes == []
        allowed = _rpc(client, path, "tools/call", {"name": "write"})
        assert allowed["result"]["content"][0]["text"] == "write succeeded"
        assert writes == ["write"]
        assert get_global_settings().read_only_mode is False


def test_global_readonly_still_restricts_both_endpoints():
    mcp, writes = _server()
    get_global_settings().read_only_mode = True
    with TestClient(mcp.http_app(path="/mcp", stateless_http=True)) as client:
        for path in ("/mcp", "/mcp/readonly"):
            blocked = _rpc(client, path, "tools/call", {"name": "write"})
            assert blocked["result"]["isError"] is True
            assert "READ_ONLY_MODE" in str(blocked)
    assert writes == []


def test_readonly_alias_keeps_oauth_authentication_and_discovery():
    from ha_mcp.auth import HomeAssistantOAuthProvider

    mcp, writes = _server()
    provider = HomeAssistantOAuthProvider(base_url="https://testserver")
    mcp.auth = provider
    token = provider._encode_token("test-ha-token")
    with TestClient(
        mcp.http_app(path="/mcp", stateless_http=True), base_url="https://testserver"
    ) as client:
        for path in ("/mcp", "/mcp/readonly"):
            for authorization in (None, "Bearer invalid-token"):
                headers = {"Authorization": authorization} if authorization else {}
                response = client.post(path, headers=headers, json={})
                assert response.status_code == 401, response.text
                challenge = response.headers["www-authenticate"]
                metadata_url = challenge.split('resource_metadata="')[1].split('"')[0]
                metadata = client.get(metadata_url)
                assert metadata.status_code == 200, metadata.text
                assert metadata.json()["resource"] == "https://testserver/mcp"
        client.headers["Authorization"] = f"Bearer {token}"
        blocked = _rpc(client, "/mcp/readonly", "tools/call", {"name": "write"})
        assert blocked["result"]["isError"] is True
        assert "READ_ONLY_MODE" in str(blocked)
        allowed = _rpc(client, "/mcp", "tools/call", {"name": "write"})
        assert allowed["result"]["content"][0]["text"] == "write succeeded"
    assert writes == ["write"]


def test_overlapping_requests_keep_their_own_readonly_mode():
    from ha_mcp.read_only import is_read_only

    mcp, _writes = _server()
    arrived = 0
    both_arrived = asyncio.Event()

    @mcp.tool(annotations={"readOnlyHint": True})
    async def inspect_mode() -> list[bool]:
        nonlocal arrived
        before = is_read_only()
        arrived += 1
        if arrived == 2:
            both_arrived.set()
        await asyncio.wait_for(both_arrived.wait(), timeout=5)
        return [before, is_read_only()]

    with (
        TestClient(mcp.http_app(path="/mcp", stateless_http=True)) as client,
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        calls = [
            executor.submit(_rpc, client, path, "tools/call", {"name": "inspect_mode"})
            for path in ("/mcp/readonly", "/mcp")
        ]
        for call, expected in zip(calls, ([True, True], [False, False]), strict=True):
            result = call.result(timeout=10)
            assert json.loads(result["result"]["content"][0]["text"]) == expected


def test_readonly_proxy_catalog_and_nested_calls():
    from ha_mcp.transforms.categorized_search import CategorizedSearchTransform

    mcp, writes = _server()
    mcp.add_transform(CategorizedSearchTransform())
    with TestClient(mcp.http_app(path="/mcp", stateless_http=True)) as client:
        for path, expected in (
            (
                "/mcp",
                {
                    "ha_search_tools",
                    "ha_call_read_tool",
                    "ha_call_write_tool",
                    "ha_call_delete_tool",
                },
            ),
            ("/mcp/readonly", {"ha_search_tools", "ha_call_read_tool"}),
            (
                "/mcp",
                {
                    "ha_search_tools",
                    "ha_call_read_tool",
                    "ha_call_write_tool",
                    "ha_call_delete_tool",
                },
            ),
        ):
            catalog = _rpc(client, path, "tools/list")
            assert {tool["name"] for tool in catalog["result"]["tools"]} == expected
        read = _rpc(
            client,
            "/mcp/readonly",
            "tools/call",
            {
                "name": "ha_call_read_tool",
                "arguments": {"name": "read"},
            },
        )
        assert read["result"]["content"][0]["text"] == "read succeeded"
        for proxy in ("ha_call_write_tool", "ha_call_read_tool", "ha_call_delete_tool"):
            blocked = _rpc(
                client,
                "/mcp/readonly",
                "tools/call",
                {
                    "name": proxy,
                    "arguments": {"name": "write"},
                },
            )
            assert "READ_ONLY_MODE" in str(blocked), blocked
        allowed = _rpc(
            client,
            "/mcp",
            "tools/call",
            {
                "name": "ha_call_write_tool",
                "arguments": {"name": "write"},
            },
        )
        assert allowed["result"]["content"][0]["text"] == "write succeeded"
    assert writes == ["write"]


def test_alias_does_not_capture_unrelated_routes():
    mcp, _writes = _server()
    with TestClient(mcp.http_app(path="/mcp", stateless_http=True)) as client:
        for path in (
            "/wrong/readonly",
            "/mcp/readonly/settings",
            "/mcp/readonly/extra",
        ):
            assert client.post(path, json={}).status_code == 404
        catalog = _rpc(client, "/mcp/readonly/", "tools/list")
        assert {tool["name"] for tool in catalog["result"]["tools"]} == {"read"}


@pytest.mark.parametrize("prefix_in_path", [False, True])
def test_readonly_endpoint_under_root_path(prefix_in_path):
    mcp, writes = _server()
    app = mcp.http_app(path="/mcp", stateless_http=True, json_response=True)
    prefix = "/prefix" if prefix_in_path else ""
    with TestClient(app, root_path="/prefix") as client:
        result = _rpc(client, prefix + "/mcp/readonly", "tools/call", {"name": "write"})
        assert result["result"]["isError"] is True
        assert "READ_ONLY_MODE" in str(result)
        assert writes == []


def test_theme_guidance_observes_readonly_connection():
    from ha_mcp.dashboard_screenshot.theme_guard import _read_only_mode
    from ha_mcp.read_only import read_only_request

    assert _read_only_mode() is False
    with read_only_request():
        assert _read_only_mode() is True
    assert _read_only_mode() is False
