"""Unit tests for ``_raw_request`` transient-gateway retry (502/503/504).

A reverse proxy / Supervisor ingress returns 502/503/504 when HA Core is
restarting or briefly overloaded behind it; the request never reached the
backend, so ``_raw_request`` retries with bounded backoff instead of hard-
failing the call (which previously surfaced as SERVICE_CALL_FAILED on every
HA-restart window and flaked the in-addon E2E suite).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from ha_mcp.client.rest_client import (
    _MAX_REQUEST_ATTEMPTS,
    HomeAssistantAPIError,
    HomeAssistantClient,
)


@pytest.fixture
def client():
    """``HomeAssistantClient`` with stubbed internals — no real network."""
    with patch.object(HomeAssistantClient, "__init__", lambda self, **kwargs: None):
        c = HomeAssistantClient()
        c.base_url = "http://test.local:8123"
        c.token = "test-token"
        c.timeout = 30
        c.verify_ssl = True
        c.httpx_client = MagicMock()
        c._supervised_detected = None
        return c


def _response(status_code, *, json_body=None, text=""):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.reason_phrase = "Bad Gateway"
    resp.text = text
    if json_body is None:
        resp.json = MagicMock(side_effect=ValueError("no json"))
    else:
        resp.json = MagicMock(return_value=json_body)
    return resp


@pytest.mark.asyncio
async def test_raw_request_retries_transient_502_then_succeeds(client):
    ok = _response(200, json_body={"ok": True})
    client.httpx_client.request = AsyncMock(side_effect=[_response(502), ok])
    with patch("ha_mcp.client.rest_client.asyncio.sleep", new=AsyncMock()) as sleep:
        result = await client._raw_request("GET", "/api/states")
    assert result is ok
    assert client.httpx_client.request.await_count == 2
    sleep.assert_awaited_once()


@pytest.mark.asyncio
async def test_raw_request_exhausts_retries_then_raises(client):
    client.httpx_client.request = AsyncMock(return_value=_response(503))
    with (
        patch("ha_mcp.client.rest_client.asyncio.sleep", new=AsyncMock()),
        pytest.raises(HomeAssistantAPIError) as exc,
    ):
        await client._raw_request("GET", "/api/states")
    assert exc.value.status_code == 503
    assert client.httpx_client.request.await_count == _MAX_REQUEST_ATTEMPTS


@pytest.mark.asyncio
async def test_raw_request_does_not_retry_non_gateway_4xx(client):
    client.httpx_client.request = AsyncMock(
        return_value=_response(400, json_body={"message": "bad request"})
    )
    with (
        patch("ha_mcp.client.rest_client.asyncio.sleep", new=AsyncMock()) as sleep,
        pytest.raises(HomeAssistantAPIError) as exc,
    ):
        await client._raw_request("GET", "/api/states")
    assert exc.value.status_code == 400
    assert client.httpx_client.request.await_count == 1
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_raw_request_success_first_try_no_retry(client):
    ok = _response(200, json_body={})
    client.httpx_client.request = AsyncMock(return_value=ok)
    with patch("ha_mcp.client.rest_client.asyncio.sleep", new=AsyncMock()) as sleep:
        result = await client._raw_request("GET", "/api/")
    assert result is ok
    assert client.httpx_client.request.await_count == 1
    sleep.assert_not_awaited()
