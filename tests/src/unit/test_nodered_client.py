"""Unit tests for NodeRedClient (httpx wrapper, auth + error classification).

Uses respx to intercept httpx requests at the transport layer, so we exercise
the real client code (Basic-auth header construction, JSON vs text branching,
status-code → exception mapping) without needing a live Node-RED instance.
"""

import httpx
import pytest
import respx

from ha_mcp.client.nodered_client import (
    NodeRedAPIError,
    NodeRedAuthError,
    NodeRedClient,
    NodeRedConnectionError,
)

BASE = "https://nodered.example.test"


def _make_client() -> NodeRedClient:
    return NodeRedClient(base_url=BASE, username="user", password="secret", timeout=5)


@pytest.mark.asyncio
@respx.mock
async def test_get_flows_returns_parsed_json_array():
    respx.get(f"{BASE}/flows").mock(
        return_value=httpx.Response(
            200,
            json=[{"id": "tab1", "type": "tab"}],
            headers={"content-type": "application/json"},
        )
    )
    client = _make_client()
    flows = await client.get_flows()
    assert flows == [{"id": "tab1", "type": "tab"}]
    await client.close()


@pytest.mark.asyncio
@respx.mock
async def test_post_flows_sets_deployment_header_and_basic_auth():
    route = respx.post(f"{BASE}/flows").mock(
        return_value=httpx.Response(
            200, text="rev-1", headers={"content-type": "text/plain"}
        )
    )
    client = _make_client()
    revision = await client.post_flows([{"id": "tab1", "type": "tab"}])
    assert revision == "rev-1"

    sent = route.calls.last.request
    assert sent.headers["Node-RED-Deployment-Type"] == "full"
    # httpx stores Basic auth as a base64-encoded Authorization header
    assert sent.headers["Authorization"].startswith("Basic ")
    await client.close()


@pytest.mark.asyncio
@respx.mock
async def test_get_settings_rejects_non_dict_response():
    respx.get(f"{BASE}/settings").mock(
        return_value=httpx.Response(
            200, json=["not", "a", "dict"], headers={"content-type": "application/json"}
        )
    )
    client = _make_client()
    with pytest.raises(NodeRedAPIError):
        await client.get_settings()
    await client.close()


@pytest.mark.asyncio
@respx.mock
async def test_401_raises_auth_error():
    respx.get(f"{BASE}/flows").mock(return_value=httpx.Response(401, text="nope"))
    client = _make_client()
    with pytest.raises(NodeRedAuthError):
        await client.get_flows()
    await client.close()


@pytest.mark.asyncio
@respx.mock
async def test_500_raises_api_error_with_status_and_body():
    respx.get(f"{BASE}/flows").mock(
        return_value=httpx.Response(500, text="server exploded")
    )
    client = _make_client()
    with pytest.raises(NodeRedAPIError) as exc:
        await client.get_flows()
    assert exc.value.status_code == 500
    assert "server exploded" in (exc.value.response_text or "")
    await client.close()


@pytest.mark.asyncio
@respx.mock
async def test_transport_error_raises_connection_error():
    respx.get(f"{BASE}/flows").mock(side_effect=httpx.ConnectError("boom"))
    client = _make_client()
    with pytest.raises(NodeRedConnectionError):
        await client.get_flows()
    await client.close()


@pytest.mark.asyncio
@respx.mock
async def test_inject_uses_post_with_node_id_in_path():
    route = respx.post(f"{BASE}/inject/abc123").mock(
        return_value=httpx.Response(200, text="OK")
    )
    client = _make_client()
    result = await client.inject("abc123")
    assert result == "OK"
    assert route.called
    await client.close()
