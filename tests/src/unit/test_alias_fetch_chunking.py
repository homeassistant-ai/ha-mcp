"""Unit tests for chunked alias fetching (#1721).

``_fetch_entity_aliases`` used to send a single ``config/entity_registry/
get_entries`` WebSocket call with every survivor entity_id. ``get_entries``
returns the full ``extended_dict`` per entry (a superset of the list
projection, including aliases), so that one response frame scaled with the
whole instance — on a large (~6.4k-entity) instance it could exceed the
WebSocket message cap and kill the connection. These tests pin the chunked
replacement: bounded per-frame size, concurrent fetch, and per-chunk
best-effort failure handling.
"""

import logging
import math
from unittest.mock import patch

import pytest

from ha_mcp.tools.smart_search import SmartSearchTools
from ha_mcp.tools.smart_search._entities import _GET_ENTRIES_CHUNK_SIZE


def _make_tools(client):
    """Create SmartSearchTools with mocked global settings."""
    with patch("ha_mcp.tools.smart_search.get_global_settings") as mock_settings:
        mock_settings.return_value.fuzzy_threshold = 60
        return SmartSearchTools(client=client)


class RecordingClient:
    """Mock client recording each ``get_entries`` call, returning synthetic aliases.

    ``fail_on_call`` (1-indexed) makes that call raise instead of returning;
    ``fail_response_on_call`` makes that call return a non-success payload.
    Both are optional and mutually exclusive per test.
    """

    def __init__(
        self,
        fail_on_call: int | None = None,
        fail_response_on_call: int | None = None,
    ) -> None:
        self.calls: list[dict] = []
        self.fail_on_call = fail_on_call
        self.fail_response_on_call = fail_response_on_call

    async def send_websocket_message(self, message: dict) -> dict:
        self.calls.append(message)
        call_number = len(self.calls)
        if self.fail_on_call == call_number:
            raise RuntimeError(f"simulated failure on call {call_number}")
        if self.fail_response_on_call == call_number:
            return {"success": False, "error": "simulated failure"}
        entity_ids = message["entity_ids"]
        return {
            "success": True,
            "result": {eid: {"aliases": [f"alias-{eid}"]} for eid in entity_ids},
        }


def _entity_ids(count: int) -> list[str]:
    return [f"light.entity_{i}" for i in range(count)]


class TestAliasFetchChunking:
    @pytest.mark.asyncio
    async def test_large_survivor_set_is_chunked(self):
        """2501 ids split into 6 bounded chunks; every id is fetched exactly once."""
        client = RecordingClient()
        tools = _make_tools(client)
        survivor_ids = _entity_ids(2501)

        aliases_map = await tools._fetch_entity_aliases(survivor_ids)

        expected_calls = math.ceil(len(survivor_ids) / _GET_ENTRIES_CHUNK_SIZE)
        assert len(client.calls) == expected_calls == 6

        requested_ids: list[str] = []
        for call in client.calls:
            assert len(call["entity_ids"]) <= _GET_ENTRIES_CHUNK_SIZE
            requested_ids.extend(call["entity_ids"])
        assert sorted(requested_ids) == sorted(survivor_ids)

        assert len(aliases_map) == len(survivor_ids)
        for eid in survivor_ids:
            assert aliases_map[eid] == [f"alias-{eid}"]

    @pytest.mark.asyncio
    async def test_below_chunk_size_issues_single_call(self):
        """A small survivor set (below the chunk size) issues exactly one call."""
        client = RecordingClient()
        tools = _make_tools(client)
        survivor_ids = _entity_ids(3)

        aliases_map = await tools._fetch_entity_aliases(survivor_ids)

        assert len(client.calls) == 1
        assert client.calls[0]["entity_ids"] == survivor_ids
        assert len(aliases_map) == 3

    @pytest.mark.asyncio
    async def test_one_failing_chunk_does_not_fail_whole_fetch(self, caplog):
        """One chunk raising still returns the other chunks' aliases, plus a warning."""
        client = RecordingClient(fail_on_call=2)
        tools = _make_tools(client)
        # Two chunks: chunk 1 (ids 0..499) succeeds, chunk 2 (id 500) raises.
        survivor_ids = _entity_ids(_GET_ENTRIES_CHUNK_SIZE + 1)

        with caplog.at_level(
            logging.WARNING, logger="ha_mcp.tools.smart_search._entities"
        ):
            aliases_map = await tools._fetch_entity_aliases(survivor_ids)

        assert len(client.calls) == 2
        # First chunk's aliases survive the second chunk's failure.
        first_chunk_id = survivor_ids[0]
        assert aliases_map[first_chunk_id] == [f"alias-{first_chunk_id}"]
        # The failed chunk's id has no alias entry.
        failed_id = survivor_ids[-1]
        assert failed_id not in aliases_map

        assert "alias_enrichment_failed" in caplog.text

    @pytest.mark.asyncio
    async def test_one_non_success_chunk_does_not_fail_whole_fetch(self, caplog):
        """A non-success response for one chunk is also tolerated, with a warning."""
        client = RecordingClient(fail_response_on_call=1)
        tools = _make_tools(client)
        survivor_ids = _entity_ids(_GET_ENTRIES_CHUNK_SIZE + 1)

        with caplog.at_level(
            logging.WARNING, logger="ha_mcp.tools.smart_search._entities"
        ):
            aliases_map = await tools._fetch_entity_aliases(survivor_ids)

        assert len(client.calls) == 2
        # The failed first chunk contributed no aliases...
        first_chunk_id = survivor_ids[0]
        assert first_chunk_id not in aliases_map
        # ...but the successful second chunk still did.
        second_chunk_id = survivor_ids[-1]
        assert aliases_map[second_chunk_id] == [f"alias-{second_chunk_id}"]

        assert "alias_enrichment_failed" in caplog.text

    @pytest.mark.asyncio
    async def test_empty_survivor_ids_returns_empty_and_makes_no_calls(self):
        client = RecordingClient()
        tools = _make_tools(client)

        aliases_map = await tools._fetch_entity_aliases([])

        assert aliases_map == {}
        assert client.calls == []
