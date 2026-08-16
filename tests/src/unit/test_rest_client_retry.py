"""Unit tests for ``_raw_request`` transient-gateway retry.

A reverse proxy / Supervisor ingress returns 502/503/504 when HA Core is
restarting or briefly overloaded behind it, and a 502 storm once failed ~190
unrelated tests at once. Reads retry with bounded backoff.

Writes never do. None of those statuses proves the request failed to execute
(RFC 9110 has 502 as "received an invalid response from an inbound server"), so
a replay can double-apply — see ``test_no_write_is_ever_gateway_retried``.
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


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [502, 503, 504])
@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
async def test_reads_still_retry_every_gateway_status(client, status, method):
    """Replaying a read costs nothing, so the storm protection stays."""
    ok = _response(200, json_body={"ok": True})
    client.httpx_client.request = AsyncMock(side_effect=[_response(status), ok])
    with patch("ha_mcp.client.rest_client.asyncio.sleep", new=AsyncMock()):
        result = await client._raw_request(method, "/api/states")
    assert result is ok
    assert client.httpx_client.request.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [502, 503, 504])
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
async def test_no_write_is_ever_gateway_retried(client, status, method):
    """None of these statuses proves Home Assistant did not execute the write.

    #1623 retried them on the premise that a gateway 5xx means the request
    never reached the backend. It does not: a replay can fire an event twice,
    run a script twice, or turn a completed DELETE into a misleading 404.
    """
    client.httpx_client.request = AsyncMock(return_value=_response(status))
    with (
        patch("ha_mcp.client.rest_client.asyncio.sleep", new=AsyncMock()) as sleep,
        pytest.raises(HomeAssistantAPIError) as exc,
    ):
        await client._raw_request(method, "/api/services/light/toggle")
    assert exc.value.status_code == status
    assert client.httpx_client.request.await_count == 1
    sleep.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [502, 503, 504])
async def test_the_config_flow_submit_is_not_replayed(client, status):
    """The call that motivated the rule.

    HA consumes the flow_id on success, so a replay returns 404 "Invalid flow
    specified" — a definitive-looking 4xx that hides a first attempt which may
    already have committed.
    """
    client.httpx_client.request = AsyncMock(return_value=_response(status))
    with (
        patch("ha_mcp.client.rest_client.asyncio.sleep", new=AsyncMock()),
        pytest.raises(HomeAssistantAPIError),
    ):
        await client.submit_config_flow_step("flow-1", {"host": "192.0.2.1"})
    assert client.httpx_client.request.await_count == 1
