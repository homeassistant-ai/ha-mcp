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

The public search now serves complete windows through the component. This
contract test invokes the legacy search implementation with the real HA
client, so it proves HA's reference-graph wire shape without relying on a
result limit to force a particular public route. The same fixture verifies
that public component search still discovers the automation.
"""

import logging
import uuid

import pytest

from ha_mcp.tools.smart_search import SmartSearchTools

from ..utilities.assertions import MCPAssertions, safe_call_tool
from ..utilities.wait_helpers import wait_for_tool_result

logger = logging.getLogger(__name__)

# Per-worker unique so parallel xdist workers never collide on entity ids.
_RUN_ID = uuid.uuid4().hex[:8]
_REFERENCED_ENTITY = f"input_boolean.refgraph_{_RUN_ID}"
_SLUG = f"reference_graph_probe_{_RUN_ID}"
_ALIAS = f"Reference Graph Probe {_RUN_ID}"
_PROBE_ENTITY_ID = f"automation.{_SLUG}"


@pytest.mark.asyncio
async def test_reference_graph_flags_an_automation_that_uses_the_entity(
    mcp_client, ha_client
):
    """Legacy deep search reports HA's reference-graph verdict for an entity_id.

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
        await wait_for_tool_result(
            mcp_client,
            tool_name="ha_search",
            arguments={
                "query": _REFERENCED_ENTITY,
                "limit": 501,
            },
            predicate=lambda d: any(
                a.get("friendly_name") == _ALIAS for a in d.get("automations", [])
            ),
            description="ha_search finds the probe automation",
        )

        data = await SmartSearchTools(client=ha_client).deep_search(
            _REFERENCED_ENTITY, search_types=["automation"], limit=501
        )
        record = next(
            a for a in data["automations"] if a.get("friendly_name") == _ALIAS
        )

        # The assertion only a live HA can make: this flag is True only if the
        # search/related frame went out, HA answered, and the answer parsed
        # into the automations bucket.
        assert record["match_in_references"] is True, (
            "Home Assistant's reference graph did not reach the automations "
            f"bucket for {_REFERENCED_ENTITY}; legacy deep search returned "
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
