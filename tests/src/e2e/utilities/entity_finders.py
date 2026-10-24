"""Shared entity-discovery helpers for e2e tests.

Several suites need "any light entity" as an automation/scene target; this
is the single implementation (the per-file copies it replaces had drifted
between assert-vs-skip and preference behavior — this keeps the most robust
variant: prefer demo/test entities, skip when the instance has none).
"""

from __future__ import annotations

import pytest

from .assertions import parse_mcp_result


async def find_test_light_entity(mcp_client) -> str:
    """Return the entity_id of a light in the test HA instance.

    Prefers demo/test entities, falls back to the first available light,
    and skips the running test when the instance exposes no lights.
    """
    search_result = await mcp_client.call_tool(
        "ha_search",
        {"query": "light", "domain_filter": "light", "limit": 20},
    )
    search_data = parse_mcp_result(search_result)
    results = search_data.get("entities", [])
    if not results:
        pytest.skip("No light entities available for testing")
    for entity in results:
        entity_id = entity.get("entity_id", "")
        if "demo" in entity_id.lower() or "test" in entity_id.lower():
            return entity_id
    entity_id = results[0].get("entity_id", "")
    if not entity_id:
        pytest.skip("No valid light entity found for testing")
    return entity_id
