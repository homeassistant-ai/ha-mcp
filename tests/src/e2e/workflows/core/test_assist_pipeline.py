"""E2E smoke tests for Assist pipeline tools."""

import logging

import pytest

from ...utilities.assertions import assert_mcp_success, safe_call_tool

logger = logging.getLogger(__name__)


@pytest.mark.core
class TestAssistPipeline:
    """Test Assist pipeline read and write tools."""

    async def test_get_assist_pipeline_list(self, mcp_client):
        """Assist pipeline listing succeeds when HA exposes the pipeline API."""
        logger.info("Testing ha_manage_pipeline list path...")

        data = await safe_call_tool(
            mcp_client, "ha_manage_pipeline", {"action": "list"}
        )
        if data.get("success") is not True:
            pytest.skip(f"Assist pipeline websocket API unavailable: {data}")

        assert "count" in data
        assert isinstance(data["pipelines"], list)
        assert data["count"] == len(data["pipelines"])

    async def test_set_preferred_assist_pipeline_idempotent_roundtrip(self, mcp_client):
        """Setting the current preferred pipeline again is an idempotent write."""
        logger.info("Testing preferred Assist pipeline idempotent roundtrip...")

        list_data = await safe_call_tool(
            mcp_client, "ha_manage_pipeline", {"action": "list"}
        )
        if list_data.get("success") is not True:
            pytest.skip(f"Assist pipeline websocket API unavailable: {list_data}")

        preferred = list_data.get("preferred_pipeline")
        if not preferred:
            pytest.skip("No preferred Assist pipeline configured")

        set_result = await mcp_client.call_tool(
            "ha_manage_pipeline",
            {"action": "set_preferred", "pipeline_id": preferred},
        )
        set_data = assert_mcp_success(set_result, "set preferred Assist pipeline")

        assert set_data["pipeline_id"] == preferred

    async def test_set_assist_pipeline_idempotent_update(self, mcp_client):
        """Updating the preferred pipeline with its current name succeeds."""
        logger.info("Testing Assist pipeline idempotent update...")

        list_data = await safe_call_tool(
            mcp_client, "ha_manage_pipeline", {"action": "list"}
        )
        if list_data.get("success") is not True:
            pytest.skip(f"Assist pipeline websocket API unavailable: {list_data}")

        preferred = list_data.get("preferred_pipeline")
        if not preferred:
            pytest.skip("No preferred Assist pipeline configured")

        pipeline_data = await safe_call_tool(
            mcp_client,
            "ha_manage_pipeline",
            {"action": "get", "pipeline_id": preferred},
        )
        if pipeline_data.get("success") is not True:
            pytest.skip(f"Preferred Assist pipeline unavailable: {pipeline_data}")

        pipeline = pipeline_data["pipeline"]
        set_result = await mcp_client.call_tool(
            "ha_manage_pipeline",
            {"action": "update", "pipeline_id": preferred, "name": pipeline["name"]},
        )
        set_data = assert_mcp_success(set_result, "set Assist pipeline")

        assert set_data["operation"] == "updated"
        assert set_data["pipeline_id"] == preferred
        assert set_data["pipeline"]["name"] == pipeline["name"]
