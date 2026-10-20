"""E2E smoke tests for Assist pipeline tools."""

import asyncio
import logging
import time
import uuid

import pytest

from ha_mcp._vendor.fastmcp import Client
from ha_mcp.client import HomeAssistantClient
from ha_mcp.client.rest_client import HomeAssistantError

from ...utilities.assertions import (
    MCPAssertions,
    assert_mcp_success,
    safe_call_tool,
)
from ...utilities.wait_helpers import wait_for_entity_state

logger = logging.getLogger(__name__)

# Budget for a freshly exposed entity to become matchable by the conversation
# agent.
_INTENT_MATCH_TIMEOUT = 20


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

    @pytest.mark.parametrize("use_preferred_alias", [False, True])
    async def test_set_preferred_assist_pipeline_idempotent_roundtrip(
        self, mcp_client: Client, use_preferred_alias: bool
    ) -> None:
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
            {
                "action": "set_preferred",
                "pipeline_id": "preferred" if use_preferred_alias else preferred,
            },
        )
        set_data = assert_mcp_success(set_result, "set preferred Assist pipeline")

        assert set_data["pipeline_id"] == preferred

    @pytest.mark.parametrize("use_preferred_alias", [False, True])
    async def test_set_assist_pipeline_idempotent_update(
        self, mcp_client: Client, use_preferred_alias: bool
    ) -> None:
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

        reference = "preferred" if use_preferred_alias else preferred
        async with MCPAssertions(mcp_client) as mcp:
            pipeline_data = await mcp.call_tool_success(
                "ha_manage_pipeline", {"action": "get", "pipeline_id": reference}
            )
        assert pipeline_data["pipeline_id"] == preferred

        pipeline = pipeline_data["pipeline"]
        set_result = await mcp_client.call_tool(
            "ha_manage_pipeline",
            {"action": "update", "pipeline_id": reference, "name": pipeline["name"]},
        )
        set_data = assert_mcp_success(set_result, "set Assist pipeline")

        assert set_data["operation"] == "updated"
        assert set_data["pipeline_id"] == preferred
        assert set_data["pipeline"]["name"] == pipeline["name"]

    async def test_create_clones_the_preferred_pipeline_alias(
        self, mcp_client: Client, ha_client: HomeAssistantClient
    ) -> None:
        """The documented clone alias reads the preferred pipeline's real settings."""
        async with MCPAssertions(mcp_client) as mcp:
            source = await mcp.call_tool_success(
                "ha_manage_pipeline", {"action": "get", "pipeline_id": "preferred"}
            )
            pipeline = source["pipeline"]
            created = await mcp.call_tool_success(
                "ha_manage_pipeline",
                {
                    "action": "create",
                    "base_pipeline_id": "preferred",
                    "name": f"Preferred clone {uuid.uuid4().hex[:8]}",
                    "conversation_engine": pipeline["conversation_engine"],
                },
            )
            try:
                clone = await mcp.call_tool_success(
                    "ha_manage_pipeline",
                    {"action": "get", "pipeline_id": created["pipeline_id"]},
                )
                assert clone["pipeline_id"] != source["pipeline_id"]
                assert clone["pipeline"]["language"] == pipeline["language"]
                assert clone["pipeline"]["tts_voice"] == pipeline["tts_voice"]
            finally:
                try:
                    await ha_client.send_websocket_message(
                        {
                            "type": "assist_pipeline/pipeline/delete",
                            "pipeline_id": created["pipeline_id"],
                        }
                    )
                except (HomeAssistantError, OSError, TimeoutError) as exc:
                    logger.warning("Could not remove isolated pipeline clone: %s", exc)

    async def test_process_reports_an_unmatched_sentence(self, mcp_client):
        """A sentence Assist cannot match answers with an error response type.

        Deliberately unmatchable: the process action executes matched intents,
        so a sentence that reaches an intent would change device state.
        """
        logger.info("Testing ha_manage_pipeline process path...")

        # No availability guard here: `conversation` ships with default_config,
        # which tests/initial_test_state/configuration.yaml loads, so a failure
        # is the endpoint or this tool regressing, not an absent component.
        async with MCPAssertions(mcp_client) as mcp:
            data = await mcp.call_tool_success(
                "ha_manage_pipeline",
                {
                    "action": "process",
                    "sentence": "zzqx wibble frobnicate the quux",
                },
            )

        assert data["operation"] == "process"
        assert data["response_type"] == "error"
        assert data["error_code"] == "no_intent_match"
        assert data["service_calls"] == {"success": [], "failed": []}

    @pytest.mark.parametrize("use_preferred_alias", [False, True])
    async def test_process_resolves_an_agent_id_home_assistant_accepts(
        self, mcp_client: Client, use_preferred_alias: bool
    ) -> None:
        """pipeline_id resolves an agent id the conversation endpoint accepts.

        The resolution reads conversation_engine off the pipeline and sends it
        as agent_id. Whether HA's own agent_id validator accepts that value is
        only observable against a real instance: a rejected id is a 400 here,
        and the mocked unit tests cannot see it. Still an unmatched sentence,
        so nothing can change state.
        """
        logger.info("Testing ha_manage_pipeline process with pipeline_id...")

        list_data = await safe_call_tool(
            mcp_client, "ha_manage_pipeline", {"action": "list"}
        )
        if list_data.get("success") is not True:
            pytest.skip(f"Assist pipeline websocket API unavailable: {list_data}")

        preferred = list_data.get("preferred_pipeline")
        if not preferred:
            pytest.skip("No preferred Assist pipeline configured")

        async with MCPAssertions(mcp_client) as mcp:
            data = await mcp.call_tool_success(
                "ha_manage_pipeline",
                {
                    "action": "process",
                    "sentence": "zzqx wibble frobnicate the quux",
                    "pipeline_id": "preferred" if use_preferred_alias else preferred,
                },
            )

        assert data["pipeline_id"] == preferred
        assert data["agent_id"], f"pipeline_id resolved no agent: {data}"
        # Which one depends on the configured agent; that HA answered at all is
        # what proves it accepted the resolved id.
        assert data["response_type"] in {"error", "action_done", "query_answer"}

    async def test_process_executes_a_matched_intent(self, mcp_client, cleanup_tracker):
        """A matched sentence runs the intent against a throwaway helper.

        The negative cases above cannot show that the payload HA accepts on the
        happy path is right, nor that data.success is extracted from a real
        response — on an error response both keys are absent and the defaults
        make the assertion pass either way. Blast radius is one input_boolean
        created here, in a per-worker container.
        """
        logger.info("Testing ha_manage_pipeline process executing an intent...")

        friendly_name = "Assist Process Probe"
        create_data = await safe_call_tool(
            mcp_client,
            "ha_config_set_helper",
            {
                "helper_type": "input_boolean",
                "name": friendly_name,
                "initial": "off",
            },
        )
        if not create_data.get("success"):
            pytest.skip(f"Could not create the test helper: {create_data}")

        entity_id = create_data.get("entity_id")
        if not entity_id:
            helper_id = create_data.get("data", {}).get("id")
            entity_id = f"input_boolean.{helper_id}" if helper_id else None
        if not entity_id:
            pytest.skip(f"Could not resolve the helper entity_id: {create_data}")

        try:
            cleanup_tracker.track("input_boolean", entity_id)

            assert await wait_for_entity_state(mcp_client, entity_id, "off"), (
                f"{entity_id} never became available"
            )

            async with MCPAssertions(mcp_client) as mcp:
                await mcp.call_tool_success(
                    "ha_set_entity",
                    {"entity_id": entity_id, "expose_to": {"conversation": True}},
                )

            # The new exposure reaches the conversation agent asynchronously, so
            # a sentence sent right away can still come back unmatched. Retry
            # within a budget instead of racing it; a real regression still
            # fails once the budget is spent.
            deadline = time.monotonic() + _INTENT_MATCH_TIMEOUT
            while True:
                async with MCPAssertions(mcp_client) as mcp:
                    data = await mcp.call_tool_success(
                        "ha_manage_pipeline",
                        {"action": "process", "sentence": f"turn on {friendly_name}"},
                    )
                if (
                    data.get("error_code") != "no_intent_match"
                    or time.monotonic() >= deadline
                ):
                    break
                await asyncio.sleep(1)

            assert data["response_type"] == "action_done", f"Intent not run: {data}"
            assert data["service_calls"]["success"], f"No target reported: {data}"
            assert entity_id in {
                target.get("id") for target in data["service_calls"]["success"]
            }, f"{entity_id} missing from the reported targets: {data}"
            assert data["service_calls"]["failed"] == []
            assert "warnings" not in data
            assert await wait_for_entity_state(mcp_client, entity_id, "on"), (
                f"{entity_id} did not turn on"
            )
        finally:
            # cleanup_tracker only logs; the helper has to be removed by hand,
            # as the other suites that create one do.
            await mcp_client.call_tool(
                "ha_remove_helpers_integrations",
                {
                    "helper_type": "input_boolean",
                    "target": entity_id,
                    "confirm": True,
                },
            )

    async def test_process_requires_a_sentence(self, mcp_client):
        """process without a sentence is rejected before HA is contacted."""
        logger.info("Testing ha_manage_pipeline process validation...")

        data = await safe_call_tool(
            mcp_client, "ha_manage_pipeline", {"action": "process"}
        )

        assert data["success"] is False
        assert data["error"]["code"] == "VALIDATION_MISSING_PARAMETER"
