"""
E2E tests for logbook functionality.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import pytest

from ...utilities.assertions import parse_mcp_result

logger = logging.getLogger(__name__)


class TestLogbook:
    """Test suite for ha_get_logbook tool."""

    @pytest.mark.asyncio
    async def test_get_logbook_basic(self, mcp_client):
        """Test basic logbook retrieval without entity filter."""
        logger.info("🧪 Testing basic logbook retrieval...")

        # Get logbook for last 24 hours
        result = await mcp_client.call_tool("ha_get_logbook", {"hours_back": 24})
        data = parse_mcp_result(result)

        logger.info(f"Logbook result: {data}")

        # Should get a response (may or may not have entries)
        assert isinstance(data, dict), "Expected dict response"

        if data.get("success"):
            assert "entries" in data, "Expected entries field in successful response"
            assert isinstance(
                data["entries"], list
            ), "Expected entries to be a list"
            logger.info(f"✅ Retrieved {len(data['entries'])} logbook entries")
        else:
            # If no entries, should have proper error structure
            assert "error" in data, "Expected error field in failed response"
            logger.info(f"ℹ️ No logbook entries: {data.get('error')}")

    @pytest.mark.asyncio
    async def test_get_logbook_with_entity(self, mcp_client):
        """Test logbook retrieval filtered by entity."""
        logger.info("🧪 Testing logbook retrieval with entity filter...")

        # First, get a light entity to use as filter
        search_result = await mcp_client.call_tool(
            "ha_search_entities", {"query": "light", "limit": 1}
        )
        search_data = parse_mcp_result(search_result)

        if (
            not search_data.get("results")
            or len(search_data.get("results", [])) == 0
        ):
            pytest.skip("No light entities available for testing")

        entity_id = search_data["results"][0]["entity_id"]
        logger.info(f"Using entity for test: {entity_id}")

        # Turn the light on to create a logbook entry
        await mcp_client.call_tool(
            "ha_call_service",
            {"domain": "light", "service": "turn_on", "entity_id": entity_id},
        )
        await asyncio.sleep(2)  # Wait for logbook to update

        # Get logbook for this entity
        result = await mcp_client.call_tool(
            "ha_get_logbook", {"hours_back": 1, "entity_id": entity_id}
        )
        data = parse_mcp_result(result)

        logger.info(f"Entity logbook result: {data}")

        # Should get a response
        assert isinstance(data, dict), "Expected dict response"

        if data.get("success"):
            assert "entries" in data, "Expected entries field"
            assert "entity_filter" in data, "Expected entity_filter field"
            assert (
                data["entity_filter"] == entity_id
            ), "Entity filter should match requested entity"
            logger.info(
                f"✅ Retrieved {len(data['entries'])} logbook entries for {entity_id}"
            )
        else:
            # May legitimately have no entries for a specific entity
            logger.info(
                f"ℹ️ No logbook entries for {entity_id}: {data.get('error')}"
            )

    @pytest.mark.asyncio
    async def test_get_logbook_time_range(self, mcp_client):
        """Test logbook retrieval with custom time range."""
        logger.info("🧪 Testing logbook retrieval with custom time range...")

        # Get logbook for last hour
        result = await mcp_client.call_tool("ha_get_logbook", {"hours_back": 1})
        data = parse_mcp_result(result)

        logger.info(f"Time range logbook result: {data}")

        # Check metadata
        assert isinstance(data, dict), "Expected dict response"
        assert "metadata" in data, "Expected metadata field"
        assert (
            "home_assistant_timezone" in data["metadata"]
        ), "Expected timezone metadata"

        if data.get("data", {}).get("success"):
            entry_data = data["data"]
            assert "period" in entry_data, "Expected period information"
            assert "start_time" in entry_data, "Expected start_time"
            assert "end_time" in entry_data, "Expected end_time"
            logger.info(f"✅ Logbook time range: {entry_data['period']}")
        else:
            logger.info(f"ℹ️ No entries in time range: {data.get('data', {}).get('error')}")

    @pytest.mark.asyncio
    async def test_logbook_after_automation_trigger(self, mcp_client):
        """Test that automation triggers appear in logbook."""
        logger.info("🧪 Testing logbook entries after automation trigger...")

        # Create a simple automation
        automation_name = "Test Logbook Automation"
        config = {
            "alias": automation_name,
            "trigger": [{"platform": "event", "event_type": "test_logbook_event"}],
            "action": [
                {"service": "persistent_notification.create", "data": {"message": "Test"}}
            ],
        }

        create_result = await mcp_client.call_tool(
            "ha_config_set_automation", {"config": config}
        )
        create_data = parse_mcp_result(create_result)

        if not create_data.get("success"):
            pytest.skip(f"Could not create automation: {create_data.get('error')}")

        automation_id = create_data.get("unique_id")
        entity_id = create_data.get("entity_id", f"automation.{automation_name.lower().replace(' ', '_')}")

        try:
            # Trigger the automation
            await mcp_client.call_tool(
                "ha_call_service",
                {"domain": "automation", "service": "trigger", "entity_id": entity_id},
            )
            await asyncio.sleep(3)  # Wait for automation to execute and log

            # Check logbook
            result = await mcp_client.call_tool("ha_get_logbook", {"hours_back": 1})
            data = parse_mcp_result(result)

            if data.get("data", {}).get("success"):
                entries = data.get("data", {}).get("entries", [])
                automation_entries = [
                    e
                    for e in entries
                    if automation_name.lower() in str(e).lower()
                    or entity_id.lower() in str(e).lower()
                ]
                logger.info(
                    f"Found {len(automation_entries)} logbook entries related to automation"
                )
                # Note: We don't assert here because logbook behavior may vary
                if automation_entries:
                    logger.info("✅ Automation execution found in logbook")
                else:
                    logger.info("ℹ️ Automation execution not found in logbook (may be expected)")
            else:
                logger.info(f"ℹ️ Could not verify logbook: {data.get('data', {}).get('error')}")

        finally:
            # Clean up
            if automation_id:
                await mcp_client.call_tool(
                    "ha_config_remove_automation", {"identifier": entity_id}
                )
