"""Unit tests for ``HomeAssistantClient.get_states()``'s response-shape contract.

Every ``/states`` reply Home Assistant itself produces on success is a JSON
array — even a fresh install has built-in entities (``sun.sun``,
``persistent_notification``, ...), so a genuinely empty instance is not a
real-world possibility. A non-list reply (including ``_request``'s own
``{}`` fallback for an unparseable body) can therefore only mean the fetch
itself failed, not "this instance has zero entities" — callers that verify
something against the live entity set (e.g. ``bulk_selector.py``'s topology
load, or ``tools_service.py``'s operations-mode group-safety check) need
that distinction to fail closed instead of silently treating an
unverifiable batch as a verified-safe one.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError


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
    resp.reason_phrase = "OK"
    resp.text = text
    if json_body is None:
        resp.json = MagicMock(side_effect=json.JSONDecodeError("no json", text, 0))
    else:
        resp.json = MagicMock(return_value=json_body)
    return resp


@pytest.mark.asyncio
async def test_get_states_passes_through_a_real_list(client):
    """The overwhelmingly common, correct case: unchanged."""
    states = [{"entity_id": "light.one", "state": "on"}]
    client.httpx_client.request = AsyncMock(
        return_value=_response(200, json_body=states)
    )

    result = await client.get_states()

    assert result == states


@pytest.mark.asyncio
async def test_get_states_passes_through_a_genuinely_empty_list(client):
    """An empty list IS a list -- isinstance(result, list) accepts it as-is.

    Distinct from the non-list cases below: this is what Home Assistant
    would send back if the check ever needed to allow for zero entities;
    the raise below is specifically about the shape being wrong, not the
    count being zero.
    """
    client.httpx_client.request = AsyncMock(return_value=_response(200, json_body=[]))

    result = await client.get_states()

    assert result == []


@pytest.mark.asyncio
async def test_get_states_raises_on_a_dict_response(client):
    """A dict-shaped body (an error envelope Home Assistant might send
    instead of the expected array) must raise, not silently become []."""
    client.httpx_client.request = AsyncMock(
        return_value=_response(200, json_body={"error": "something went wrong"})
    )

    with pytest.raises(HomeAssistantConnectionError):
        await client.get_states()


@pytest.mark.asyncio
async def test_get_states_raises_on_unparseable_body(client):
    """``_request``'s own ``{}`` fallback for a JSONDecodeError must not
    read as "zero entities" either -- it is the same non-list shape,
    reached through a different failure inside ``_request``."""
    client.httpx_client.request = AsyncMock(
        return_value=_response(200, json_body=None, text="not json")
    )

    with pytest.raises(HomeAssistantConnectionError):
        await client.get_states()
