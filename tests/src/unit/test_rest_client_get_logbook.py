"""get_logbook keeps a malformed REST reply visible to its callers."""

from unittest.mock import AsyncMock, patch

import pytest

from ha_mcp.client.rest_client import HomeAssistantClient


def _client() -> HomeAssistantClient:
    with patch("ha_mcp.client.rest_client.httpx.AsyncClient"):
        return HomeAssistantClient(base_url="http://ha.local:8123", token="t")


@pytest.mark.asyncio
async def test_get_logbook_returns_none_for_a_malformed_reply():
    """A JSON object instead of a row list must not read as an empty logbook."""
    client = _client()

    with patch.object(client, "_request", AsyncMock(return_value={"rows": []})):
        rows = await client.get_logbook(start_time="2026-11-08T16:00:00+00:00")

    assert rows is None


@pytest.mark.asyncio
async def test_get_logbook_returns_the_row_list():
    """A row-list reply is passed through unchanged."""
    client = _client()
    rows = [{"when": "2026-11-08T16:00:00+00:00", "name": "Light"}]

    with patch.object(client, "_request", AsyncMock(return_value=rows)):
        assert await client.get_logbook(start_time="2026-11-08T16:00:00+00:00") == rows
