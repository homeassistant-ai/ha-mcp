"""E2E smoke tests for Assist pipeline tools."""

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest

from ...utilities.assertions import (
    MCPAssertions,
    assert_mcp_success,
    safe_call_tool,
)
from ...utilities.cleanup import CleanupSteps
from ...utilities.wait_helpers import wait_for_entity_state

logger = logging.getLogger(__name__)

# Budget for a freshly exposed entity to become matchable by the conversation
# agent.
_INTENT_MATCH_TIMEOUT = 20

# Unmatchable, so running it through Assist cannot change any state.
_UNMATCHED = "zzqx wibble frobnicate the quux"

# Every field of Core's pipeline schema, none at the default pipeline's value,
# so a clone or a merge shows where each value came from. Core checks only that
# an STT/TTS engine comes with its language, not that the engine exists.
_BASE: dict[str, Any] = {
    "conversation_engine": "conversation.home_assistant",
    "conversation_language": "*",
    "language": "en",
    "stt_engine": None,
    "stt_language": "en-US",
    "tts_engine": None,
    "tts_language": "en-GB",
    "tts_voice": "e2e-voice",
    "wake_word_entity": None,
    "wake_word_id": "e2e_wake",
    "prefer_local_intents": True,
}

Create = Callable[..., Awaitable[dict[str, Any]]]


async def _core_pipeline(ha_client: Any, pipeline_id: str) -> dict[str, Any]:
    reply = await ha_client.send_websocket_message(
        {"type": "assist_pipeline/pipeline/get", "pipeline_id": pipeline_id}
    )
    assert reply.get("success"), reply
    pipeline: dict[str, Any] = reply["result"]
    return pipeline


async def _core_preferred(ha_client: Any) -> str:
    reply = await ha_client.send_websocket_message(
        {"type": "assist_pipeline/pipeline/list"}
    )
    assert reply.get("success"), reply
    preferred: str = reply["result"]["preferred_pipeline"]
    return preferred


def _settings(pipeline: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in pipeline.items() if k not in {"id", "name"}}


@pytest.fixture
async def scratch_pipelines(ha_client: Any) -> AsyncIterator[Create]:
    """Creates ``_BASE`` pipelines in Core; afterwards deletes every pipeline
    the test left and gives the preferred one back."""
    listed = await ha_client.send_websocket_message(
        {"type": "assist_pipeline/pipeline/list"}
    )
    if not listed.get("success"):
        pytest.skip(f"Assist pipeline websocket API unavailable: {listed}")
    preferred = listed["result"]["preferred_pipeline"]
    before = {p["id"] for p in listed["result"]["pipelines"]}

    async def create(**fields: Any) -> dict[str, Any]:
        reply = await ha_client.send_websocket_message(
            {
                "type": "assist_pipeline/pipeline/create",
                **_BASE,
                "name": f"E2E Pipeline {uuid.uuid4().hex[:8]}",
                **fields,
            }
        )
        assert reply.get("success"), reply
        pipeline: dict[str, Any] = reply["result"]
        return pipeline

    try:
        yield create
    finally:
        with CleanupSteps() as cleanup:
            with cleanup.step():
                restored = await ha_client.send_websocket_message(
                    {
                        "type": "assist_pipeline/pipeline/set_preferred",
                        "pipeline_id": preferred,
                    }
                )
                assert restored.get("success"), restored
            with cleanup.step():
                listed = await ha_client.send_websocket_message(
                    {"type": "assist_pipeline/pipeline/list"}
                )
                assert listed.get("success"), listed
                for pipeline in listed["result"]["pipelines"]:
                    if pipeline["id"] in before:
                        continue
                    removed = await ha_client.send_websocket_message(
                        {
                            "type": "assist_pipeline/pipeline/delete",
                            "pipeline_id": pipeline["id"],
                        }
                    )
                    assert removed.get("success"), removed


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
        assert data["preferred_pipeline"] in {p["id"] for p in data["pipelines"]}

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

    async def test_process_resolves_an_agent_id_home_assistant_accepts(
        self, mcp_client
    ):
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
                    "pipeline_id": preferred,
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
                "config": {"initial": "off"},
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
            await safe_call_tool(
                mcp_client,
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

    async def test_create_clones_the_base_pipeline_it_is_given(
        self, mcp_client, ha_client, scratch_pipelines: Create
    ):
        """The new pipeline keeps every setting of the base but the ones passed,
        and get reads back what Core stored."""
        base = await scratch_pipelines()

        async with MCPAssertions(mcp_client) as mcp:
            data = await mcp.call_tool_success(
                "ha_manage_pipeline",
                {
                    "action": "create",
                    "base_pipeline_id": base["id"],
                    "name": "E2E Clone",
                    "conversation_engine": "conversation.e2e_agent",
                },
            )
            read = await mcp.call_tool_success(
                "ha_manage_pipeline",
                {"action": "get", "pipeline_id": data["pipeline_id"]},
            )

        stored = await _core_pipeline(ha_client, data["pipeline_id"])
        assert data["pipeline"] == stored
        assert read["pipeline"] == stored
        assert stored["id"] != base["id"]
        assert stored["name"] == "E2E Clone"
        assert _settings(stored) == {
            **_settings(base),
            "conversation_engine": "conversation.e2e_agent",
        }

    async def test_create_without_a_base_clones_the_preferred_pipeline(
        self, mcp_client, ha_client, scratch_pipelines: Create
    ):
        preferred = await scratch_pipelines()
        reply = await ha_client.send_websocket_message(
            {
                "type": "assist_pipeline/pipeline/set_preferred",
                "pipeline_id": preferred["id"],
            }
        )
        assert reply.get("success"), reply

        async with MCPAssertions(mcp_client) as mcp:
            data = await mcp.call_tool_success(
                "ha_manage_pipeline",
                {
                    "action": "create",
                    "name": "E2E Clone",
                    "conversation_engine": "conversation.e2e_agent",
                },
            )

        assert data["preferred_changed"] is False
        assert _settings(await _core_pipeline(ha_client, data["pipeline_id"])) == {
            **_settings(preferred),
            "conversation_engine": "conversation.e2e_agent",
        }

    async def test_update_keeps_unnamed_fields_and_clears_an_emptied_nullable_one(
        self, mcp_client, ha_client, scratch_pipelines: Create
    ):
        """Core replaces the whole pipeline on update, so a field the caller
        did not name must be sent as stored; an empty string clears a
        nullable field instead of storing ""."""
        voice = await scratch_pipelines()

        async with MCPAssertions(mcp_client) as mcp:
            data = await mcp.call_tool_success(
                "ha_manage_pipeline",
                {
                    "action": "update",
                    "pipeline_id": voice["id"],
                    "conversation_engine": "conversation.e2e_agent",
                    "tts_voice": "",
                },
            )

        stored = await _core_pipeline(ha_client, voice["id"])
        assert data["pipeline"] == stored
        assert stored == {
            **voice,
            "conversation_engine": "conversation.e2e_agent",
            "tts_voice": None,
        }

    @pytest.mark.parametrize("action", ["create", "update", "set_preferred"])
    async def test_a_pipeline_made_preferred_is_what_core_prefers(
        self, mcp_client, ha_client, scratch_pipelines: Create, action: str
    ):
        """create/update with make_preferred, and set_preferred, each move
        Core's preference to the pipeline they name or write."""
        voice = await scratch_pipelines()
        arguments: dict[str, Any] = {
            "create": {
                "action": "create",
                "name": "E2E Preferred",
                "conversation_engine": "conversation.home_assistant",
                "base_pipeline_id": voice["id"],
                "make_preferred": True,
            },
            "update": {
                "action": "update",
                "pipeline_id": voice["id"],
                "name": "E2E Preferred",
                "make_preferred": True,
            },
            "set_preferred": {"action": "set_preferred", "pipeline_id": voice["id"]},
        }[action]

        async with MCPAssertions(mcp_client) as mcp:
            data = await mcp.call_tool_success("ha_manage_pipeline", arguments)

        assert await _core_preferred(ha_client) == data["pipeline_id"]
        if action != "set_preferred":
            assert data["preferred_changed"] is True

    @pytest.mark.parametrize(
        ("action", "core_says"),
        [("get", "Unable to find"), ("set_preferred", "unknown item")],
    )
    async def test_a_pipeline_core_does_not_have_is_reported(
        self, mcp_client, action: str, core_says: str
    ):
        missing = f"e2e_missing_{uuid.uuid4().hex[:8]}"

        data = await MCPAssertions(mcp_client).call_tool_failure(
            "ha_manage_pipeline",
            {"action": action, "pipeline_id": missing},
            expected_error=core_says,
        )

        assert data["error"]["code"] == "SERVICE_CALL_FAILED"
        assert data["pipeline_id"] == missing

    async def test_create_reports_core_rejecting_the_pipeline(
        self, mcp_client, scratch_pipelines: Create
    ):
        """Clearing the STT language while naming an STT engine is a pipeline
        Core refuses."""
        base = await scratch_pipelines()

        data = await MCPAssertions(mcp_client).call_tool_failure(
            "ha_manage_pipeline",
            {
                "action": "create",
                "base_pipeline_id": base["id"],
                "name": "E2E Rejected",
                "conversation_engine": "conversation.home_assistant",
                "stt_engine": "stt.e2e",
                "stt_language": "",
            },
            expected_error="stt_language",
        )

        assert data["error"]["code"] == "SERVICE_CALL_FAILED"
        assert data["operation"] == "create"

    @pytest.mark.parametrize(
        ("languages", "explicit", "expected"),
        [
            pytest.param(
                {"stt_language": "de-DE", "tts_language": "de-AT", "language": "de"},
                None,
                "de-DE",
                id="stt-beats-tts-and-language",
            ),
            pytest.param(
                {"stt_language": None, "tts_language": "de-AT", "language": "de"},
                None,
                "de-AT",
                id="tts-when-no-stt",
            ),
            pytest.param(
                {"stt_language": None, "tts_language": None, "language": "de"},
                None,
                "de",
                id="pipeline-language-last",
            ),
            pytest.param(
                {"conversation_language": "nl", "stt_language": "de-DE"},
                None,
                "nl",
                id="specific-conversation-language-wins",
            ),
            pytest.param(
                {"conversation_language": "", "stt_language": "de-DE"},
                None,
                "de-DE",
                id="empty-conversation-language-falls-through",
            ),
            pytest.param(
                {"stt_language": "de-DE"},
                "fr",
                "fr",
                id="explicit-language-beats-the-pipeline",
            ),
        ],
    )
    async def test_process_runs_in_the_language_the_pipeline_resolves(
        self,
        mcp_client,
        scratch_pipelines: Create,
        languages: dict[str, Any],
        explicit: str | None,
        expected: str,
    ):
        """Core resolves a "*" pipeline the same way and prefers the more
        specific STT value; an empty conversation_language is not a language
        and Core would pass it on verbatim. The built-in agent answers in the
        language it was asked in."""
        voice = await scratch_pipelines(**languages)
        arguments: dict[str, Any] = {
            "action": "process",
            "sentence": _UNMATCHED,
            "pipeline_id": voice["id"],
        }
        if explicit is not None:
            arguments["language"] = explicit

        async with MCPAssertions(mcp_client) as mcp:
            data = await mcp.call_tool_success("ha_manage_pipeline", arguments)

        assert data["language"] == expected

    async def test_process_leaves_an_unresolvable_language_to_home_assistant(
        self, mcp_client, ha_client, scratch_pipelines: Create
    ):
        """Sending "*" or "" would make the agent recognise in no language;
        left out, Core uses its configured one."""
        voice = await scratch_pipelines(
            language="", stt_language=None, tts_language=None
        )
        config = await ha_client.get_config()

        async with MCPAssertions(mcp_client) as mcp:
            data = await mcp.call_tool_success(
                "ha_manage_pipeline",
                {
                    "action": "process",
                    "sentence": _UNMATCHED,
                    "pipeline_id": voice["id"],
                },
            )

        assert data["language"] == config["language"]

    async def test_process_explicit_agent_beats_the_pipeline_agent(
        self, mcp_client, scratch_pipelines: Create
    ):
        """The pipeline names an agent Home Assistant does not have, so the
        call succeeds only if the explicit agent_id is the one sent."""
        voice = await scratch_pipelines(conversation_engine="conversation.e2e_missing")

        async with MCPAssertions(mcp_client) as mcp:
            data = await mcp.call_tool_success(
                "ha_manage_pipeline",
                {
                    "action": "process",
                    "sentence": _UNMATCHED,
                    "pipeline_id": voice["id"],
                    "agent_id": "conversation.home_assistant",
                },
            )

        assert data["agent_id"] == "conversation.home_assistant"
