"""E2E contract test for the ``search/related`` reference-graph merge.

Discussion #2258. The unit tests around this feature drive a mocked client, so
every one of them asserts against a response shape this repo *assumed* Home
Assistant produces. That assumption is load-bearing and non-obvious: HA's
``Searcher.async_search`` returns ``dict[ItemType, set[str]]``, and what
reaches the wire depends on its JSON encoder coercing those sets to arrays.

If the real shape differs, every unit test still passes and ``ha_search``
silently reports nothing from the graph. Silent is the operative word: the
feature exists to answer "what breaks if I rename this entity", so a false
negative reads as "nothing uses it" and makes an unsafe rename look safe.

One test, against a real Home Assistant, closes exactly that gap. It pins the
command name, its parameters, the envelope parsing, and the item-type-to-bucket
mapping in a single pass. Everything else about the merge is pure logic already
covered by ``tests/src/unit/test_search_related_graph.py``.

**Why this test asks for a dashboard surface and an oversized limit.** The
reference-graph merge runs on the legacy config-body path only. It is
deliberately absent from the ``ha_mcp_tools`` component route, which reads HA's
loaded config in-process and so has neither of the blind spots above (no per-id
budget, and YAML-defined configs are visible to it). This suite installs that
component into every test container, so a plain ``search_types=["automation"]``
call is served by the component and never reaches the code under test.

Naming ``dashboard`` alone no longer sends the call to legacy: the component
serves the surfaces its search command has while the dashboards leg serves that
bucket, merged server-side (issue #2289). What still routes the WHOLE call to
legacy is a page the merge cannot fetch -- ``offset + limit`` past the
component's 500-record ``limit`` ceiling, which its schema rejects outright --
so the request pairs the dashboard surface with a limit one past that ceiling.
Should that lever stop working, this test fails on ``match_in_references``
rather than passing vacuously against a feature that never ran.
"""

import logging
import uuid

import pytest

from ..utilities.assertions import MCPAssertions, safe_call_tool
from ..utilities.wait_helpers import wait_for_tool_result

logger = logging.getLogger(__name__)

# Per-worker unique so parallel xdist workers never collide on entity ids.
_RUN_ID = uuid.uuid4().hex[:8]
_REFERENCED_ENTITY = f"input_boolean.refgraph_{_RUN_ID}"
_SLUG = f"reference_graph_probe_{_RUN_ID}"
_ALIAS = f"Reference Graph Probe {_RUN_ID}"
_PROBE_ENTITY_ID = f"automation.{_SLUG}"
# One past the component's 500-record ``limit`` ceiling, which is what sends a
# ``dashboard``-including request to the legacy path whole — see the module
# docstring. Wide enough that the probe automation is on the page regardless of
# how many configs the container holds.
_LEGACY_ROUTE_LIMIT = 501


@pytest.mark.asyncio
async def test_reference_graph_flags_an_automation_that_uses_the_entity(mcp_client):
    """ha_search reports HA's own reference-graph verdict for an entity_id query.

    Fails if HA does not serve ``search/related``, if it names the command or
    its parameters differently, if the result envelope is not what the parser
    expects, or if ``automation`` stops mapping onto the automations bucket.
    In every one of those cases the graph contributes nothing and the user's
    dependency check quietly under-reports.
    """
    automation_config = {
        "alias": _ALIAS,
        "trigger": [{"platform": "state", "entity_id": _REFERENCED_ENTITY, "to": "on"}],
        "action": [
            {
                "service": "homeassistant.turn_on",
                "target": {"entity_id": _REFERENCED_ENTITY},
            }
        ],
    }

    async with MCPAssertions(mcp_client) as mcp:
        await mcp.call_tool_success(
            "ha_config_set_automation", {"config": automation_config}
        )

    try:
        data = await wait_for_tool_result(
            mcp_client,
            tool_name="ha_search",
            arguments={
                "query": _REFERENCED_ENTITY,
                # This pair forces the legacy path -- see the module docstring.
                "search_types": ["automation", "dashboard"],
                "limit": _LEGACY_ROUTE_LIMIT,
            },
            predicate=lambda d: any(
                a.get("friendly_name") == _ALIAS for a in d.get("automations", [])
            ),
            description="ha_search finds the probe automation",
        )

        record = next(
            a for a in data["automations"] if a.get("friendly_name") == _ALIAS
        )

        # The assertion only a live HA can make: this flag is True only if the
        # search/related frame went out, HA answered, and the answer parsed
        # into the automations bucket.
        assert record["match_in_references"] is True, (
            "Home Assistant's reference graph did not reach the automations "
            f"bucket for {_REFERENCED_ENTITY}; ha_search fell back to "
            f"config-body search alone. Record: {record}"
        )
        logger.info("✅ reference graph flagged %s", _ALIAS)
    finally:
        # ``safe_call_tool`` so a cleanup failure cannot mask the real
        # assertion above (tests/AGENTS.md "Test Patterns").
        await safe_call_tool(
            mcp_client,
            "ha_config_remove_automation",
            {"identifier": _PROBE_ENTITY_ID},
        )
        logger.info("🧹 Cleaned up probe automation")
