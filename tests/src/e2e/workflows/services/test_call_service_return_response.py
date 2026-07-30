"""E2E coverage for ha_call_service's return_response placement (issue #2085).

``calendar.get_events`` is a ``SupportsResponse.ONLY`` service, so calling it with
``return_response=True`` against the test container exercises a real Home Assistant
``return_response`` envelope (``{"changed_states": [...], "service_response": {...}}``).

ha_call_service used to ship that response twice — once nested in ``result`` (the
whole envelope passed through projection) and once as a top-level
``service_response`` sibling — doubling its token cost. These assertions are
path-agnostic: they hold whether the component ``call_service`` capability or the
legacy REST POST serves the call, since both must surface the response exactly once
at the top level.
"""

import json
import logging
from datetime import UTC, datetime, timedelta

import pytest

from ...utilities.assertions import MCPAssertions

logger = logging.getLogger(__name__)


async def _find_calendar_entity(mcp_client) -> str:
    """Find the seeded local_calendar entity — its absence is a failure, not a skip."""
    async with MCPAssertions(mcp_client) as mcp:
        data = await mcp.call_tool_success(
            "ha_search",
            {"query": "calendar", "domain_filter": "calendar", "limit": 10},
        )
    entities = data.get("entities", [])
    assert entities, f"seeded local_calendar not found via ha_search: {data!r}"
    entity_id = entities[0].get("entity_id")
    assert entity_id, f"calendar search record lacks an entity_id: {entities[0]!r}"
    return entity_id


@pytest.mark.asyncio
@pytest.mark.services
class TestReturnResponsePlacement:
    """The service response is returned once, at the top level."""

    async def test_get_events_response_is_not_duplicated(self, mcp_client):
        calendar_entity = await _find_calendar_entity(mcp_client)

        start = datetime.now(UTC)
        end = start + timedelta(days=7)

        logger.info(
            f"Calling calendar.get_events on {calendar_entity} with return_response"
        )

        async with MCPAssertions(mcp_client) as mcp:
            data = await mcp.call_tool_success(
                "ha_call_service",
                {
                    "domain": "calendar",
                    "service": "get_events",
                    "entity_id": calendar_entity,
                    "data": {
                        "start_date_time": start.strftime("%Y-%m-%d %H:%M:%S"),
                        "end_date_time": end.strftime("%Y-%m-%d %H:%M:%S"),
                    },
                    "return_response": True,
                },
            )

        assert "service_response" in data, (
            f"return_response=True must surface a top-level service_response: {data!r}"
        )
        service_response = data["service_response"]

        result = data.get("result")
        assert not (isinstance(result, dict) and "service_response" in result), (
            f"result must not carry a nested service_response copy: {result!r}"
        )

        # The response must carry the payload exactly once — the token cost the
        # issue is about. Count the key first (cheap and always meaningful), then
        # the serialized payload itself when it is distinctive enough for a
        # substring match to mean something (a bare ``{}`` would match any empty
        # dict elsewhere in the response).
        serialized = json.dumps(data, sort_keys=True)
        assert serialized.count('"service_response"') == 1, (
            f"service_response key appears more than once: {data!r}"
        )
        payload = json.dumps(service_response, sort_keys=True)
        if len(payload) > 8:
            assert serialized.count(payload) == 1, (
                f"service_response payload appears {serialized.count(payload)} "
                f"times, expected exactly 1: {data!r}"
            )

        logger.info(
            f"calendar.get_events returned its response once: "
            f"{list(service_response) if isinstance(service_response, dict) else service_response!r}"
        )
