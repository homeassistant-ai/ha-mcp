"""E2E tests for ha_remove_entity tool."""

import logging

import pytest

from tests.src.e2e.utilities.assertions import safe_call_tool

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
@pytest.mark.registry
class TestEntityRemove:
    """Test ha_remove_entity tool."""

    async def test_remove_entity_nonexistent(self, mcp_client):
        """Removing a non-existent entity should fail gracefully."""
        data = await safe_call_tool(
            mcp_client,
            "ha_remove_entity",
            {"entity_id": "sensor.definitely_not_real_12345"},
        )
        assert not data.get("success"), (
            f"Expected failure for non-existent entity, got: {data}"
        )
        logger.info("Non-existent entity removal error handling verified")
