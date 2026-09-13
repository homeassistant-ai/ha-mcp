"""The readonly URL applies existing enforcement without changing other clients."""

import json

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
        for endpoint, expected in ((readonly_path, {"read"}), (path, {"read", "write"})):
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
