"""Offset pagination of ``ha_search`` entity results (issue #605).

Both the exact-match and the fuzzy search page their matches with the same
metadata (``entity_total_matches``, ``offset``, ``limit``, ``count``,
``has_more``, ``next_offset``). The search runs on an install without the
component, so the server pages Core's own states itself.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.smart_search import SmartSearchTools
from ha_mcp.tools.tools_search import SearchTools

_ROOMS = 10


@pytest.fixture
async def tools(hass: HomeAssistant, core_client: HomeAssistantClient) -> SearchTools:
    """The search tools on Core holding ten room lights."""
    # ``config`` serves the entity-registry reads.
    assert await async_setup_component(hass, "config", {})
    for i in range(_ROOMS):
        hass.states.async_set(
            f"light.room_{i}", "on", {"friendly_name": f"Room {i} Light"}
        )
    return SearchTools(core_client, SmartSearchTools(client=core_client))


# ``domain_filter`` keeps each search on the entity surface, so the flat
# pagination keys describe the entity pages alone.
@pytest.mark.parametrize("exact_match", [True, False], ids=["exact", "fuzzy"])
async def test_paging_by_next_offset_yields_every_match_once(
    tools: SearchTools, exact_match: bool
) -> None:
    seen: list[str] = []
    offset: int | None = 0
    pages = 0
    while offset is not None:
        page = await tools.ha_search(
            query="room",
            domain_filter="light",
            exact_match=exact_match,
            limit=3,
            offset=offset,
        )
        assert page["entity_total_matches"] == _ROOMS
        assert page["count"] == len(page["entities"]) <= 3
        seen.extend(row["entity_id"] for row in page["entities"])
        offset = page["next_offset"]
        assert page["has_more"] is (offset is not None)
        pages += 1

    assert pages == 4
    assert sorted(seen) == sorted(f"light.room_{i}" for i in range(_ROOMS))


@pytest.mark.parametrize("exact_match", [True, False], ids=["exact", "fuzzy"])
async def test_first_page_says_more_follow(
    tools: SearchTools, exact_match: bool
) -> None:
    page = await tools.ha_search(
        query="room", domain_filter="light", exact_match=exact_match, limit=3
    )

    assert page["offset"] == 0
    assert page["limit"] == 3
    assert page["count"] == 3
    assert page["has_more"] is True
    assert page["next_offset"] == 3


@pytest.mark.parametrize("exact_match", [True, False], ids=["exact", "fuzzy"])
async def test_last_partial_page_says_nothing_follows(
    tools: SearchTools, exact_match: bool
) -> None:
    page = await tools.ha_search(
        query="room", domain_filter="light", exact_match=exact_match, limit=3, offset=9
    )

    assert page["count"] == 1
    assert page["has_more"] is False
    assert page["next_offset"] is None


@pytest.mark.parametrize("exact_match", [True, False], ids=["exact", "fuzzy"])
async def test_offset_past_the_end_is_empty_but_keeps_the_total(
    tools: SearchTools, exact_match: bool
) -> None:
    page = await tools.ha_search(
        query="room",
        domain_filter="light",
        exact_match=exact_match,
        limit=3,
        offset=100,
    )

    assert page["entities"] == []
    assert page["count"] == 0
    assert page["has_more"] is False
    assert page["next_offset"] is None
    assert page["entity_total_matches"] == _ROOMS


async def test_fuzzy_search_without_a_match_offers_suggestions(
    tools: SearchTools,
) -> None:
    """A fuzzy search that finds nothing suggests what to search for instead."""
    result = await tools.ha_search(query="xyznonexistent999", exact_match=False)

    assert result["success"] is True
    assert result["entity_total_matches"] == 0
    assert result["suggestions"]
    assert all(isinstance(s, str) and s for s in result["suggestions"])
