"""ha_manage_pipeline checks its arguments and runs sentences through Core.

The tool runs on the server's own client connected to a real ``hass``.
Core's ``conversation`` integration answers ``/api/conversation/process``: a
sentence goes to Core's built-in agent or to a conversation agent this file
registers the way an integration does, so every answer the tool reads is
Core's serialization of a real ``ConversationResult``.

``assist_pipeline`` is not set up here: two of its requirements have no wheel
for this Python, so Core answers its commands with ``unknown_command``. What
needs its replies (cloning, merging, the preferred pipeline, the language a
pipeline resolves to) is covered in
``tests/src/e2e/workflows/core/test_assist_pipeline.py``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal

import pytest
from homeassistant.components import conversation
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_assist_pipeline import AssistPipelineTools

from ._client_spies import sent_messages
from ._tool_errors import rejected_call_body

_KITCHEN = intent.IntentResponseTarget(
    name="Kitchen light",
    type=intent.IntentResponseTargetType.ENTITY,
    id="light.kitchen",
)
_HALLWAY = intent.IntentResponseTarget(
    name="Hallway light",
    type=intent.IntentResponseTargetType.ENTITY,
    id="light.hallway",
)


def _turned_on_the_kitchen_light(response: intent.IntentResponse) -> None:
    response.async_set_speech("Turned on the light")
    response.async_set_results([_KITCHEN])


class RecordingAgent(conversation.AbstractConversationAgent):
    """A conversation agent as an integration registers one: it records each
    input Core hands it and answers through ``reply``."""

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        self.inputs: list[conversation.ConversationInput] = []
        self.reply: Callable[[intent.IntentResponse], None] = (
            _turned_on_the_kitchen_light
        )

    @property
    def supported_languages(self) -> Literal["*"]:
        return MATCH_ALL

    async def async_process(
        self, user_input: conversation.ConversationInput
    ) -> conversation.ConversationResult:
        self.inputs.append(user_input)
        response = intent.IntentResponse(language=user_input.language)
        self.reply(response)
        return conversation.ConversationResult(
            response=response,
            conversation_id=user_input.conversation_id or "01JABCDEF",
            # Not Core's own False default, so the result shows it was passed on.
            continue_conversation=True,
        )


@pytest.fixture
def tool(core_client: HomeAssistantClient) -> AssistPipelineTools:
    return AssistPipelineTools(core_client)


@pytest.fixture
async def agent(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> RecordingAgent:
    """Core's conversation API with a registered agent besides the built-in one."""
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "conversation", {})
    entry = MockConfigEntry(domain="recording_agent")
    entry.add_to_hass(hass)
    recording = RecordingAgent(entry.entry_id)
    conversation.async_set_agent(hass, entry, recording)
    return recording


Call = Callable[[AssistPipelineTools], Awaitable[Any]]


# --- what is refused before Home Assistant is asked ---------------------------


@pytest.mark.parametrize(
    ("call", "code", "names"),
    [
        pytest.param(
            lambda t: t.ha_manage_pipeline(action="get"),
            "VALIDATION_MISSING_PARAMETER",
            "pipeline_id",
            id="get-without-pipeline-id",
        ),
        pytest.param(
            lambda t: t.ha_manage_pipeline(
                action="create", conversation_engine="conversation.local_llm"
            ),
            "VALIDATION_MISSING_PARAMETER",
            "name and conversation_engine",
            id="create-without-name",
        ),
        pytest.param(
            lambda t: t.ha_manage_pipeline(
                action="update", conversation_engine="conversation.local_llm"
            ),
            "VALIDATION_MISSING_PARAMETER",
            "pipeline_id",
            id="update-without-pipeline-id",
        ),
        pytest.param(
            lambda t: t.ha_manage_pipeline(action="update", pipeline_id="voice"),
            "VALIDATION_INVALID_PARAMETER",
            "No Assist pipeline changes requested",
            id="update-without-changes",
        ),
        pytest.param(
            lambda t: t.ha_manage_pipeline(
                action="update", pipeline_id="voice", name=""
            ),
            "VALIDATION_INVALID_PARAMETER",
            "name cannot be an empty string",
            id="update-blanking-a-required-field",
        ),
    ],
)
async def test_an_incomplete_pipeline_request_is_refused_before_core_is_asked(
    tool: AssistPipelineTools,
    core_client: HomeAssistantClient,
    call: Call,
    code: str,
    names: str,
) -> None:
    """Each would otherwise reach Core and act on the wrong pipeline: a get or
    update without an id lands on the preferred one, a create without a name
    copies the preferred one's, and Core's schema stores an empty name."""
    with sent_messages(core_client) as spy:
        body = await rejected_call_body(call(tool))

    assert body["error"]["code"] == code
    assert names in body["error"]["message"]
    spy.assert_not_awaited()


@pytest.mark.parametrize("sentence", [None, "   "])
async def test_process_without_a_usable_sentence_sends_nothing(
    tool: AssistPipelineTools, agent: RecordingAgent, sentence: str | None
) -> None:
    body = await rejected_call_body(
        tool.ha_manage_pipeline(
            action="process", sentence=sentence, agent_id=agent.agent_id
        )
    )

    assert body["error"]["code"] == "VALIDATION_MISSING_PARAMETER"
    assert agent.inputs == []


# --- Home Assistant without Assist pipelines, or not answering ---------------


async def test_get_reports_core_refusing_the_command_with_the_pipeline_named(
    tool: AssistPipelineTools,
) -> None:
    body = await rejected_call_body(
        tool.ha_manage_pipeline(action="get", pipeline_id="voice")
    )

    assert body["success"] is False
    assert body["error"]["code"] == "SERVICE_CALL_FAILED"
    assert "Unknown command" in body["error"]["message"]
    assert body["pipeline_id"] == "voice"


async def test_process_with_a_pipeline_core_cannot_read_runs_no_sentence(
    tool: AssistPipelineTools, agent: RecordingAgent
) -> None:
    """Running it anyway would answer with another agent and language than
    the pipeline the caller named."""
    body = await rejected_call_body(
        tool.ha_manage_pipeline(
            action="process", sentence="turn on the light", pipeline_id="voice"
        )
    )

    assert body["error"]["code"] == "SERVICE_CALL_FAILED"
    assert agent.inputs == []


async def test_list_reports_an_unreachable_home_assistant(
    hass: HomeAssistant, unreachable_client: HomeAssistantClient
) -> None:
    body = await rejected_call_body(
        AssistPipelineTools(unreachable_client).ha_manage_pipeline(action="list")
    )

    assert body["success"] is False
    assert body["error"]["code"] == "CONNECTION_FAILED"


async def test_process_keeps_the_sentence_out_of_a_transport_error(
    hass: HomeAssistant, unreachable_client: HomeAssistantClient
) -> None:
    body = await rejected_call_body(
        AssistPipelineTools(unreachable_client).ha_manage_pipeline(
            action="process", sentence="unlock the front door for Alex"
        )
    )

    assert body["success"] is False
    assert "Alex" not in str(body)


# --- process -------------------------------------------------------------------


async def test_process_runs_the_sentence_and_flattens_the_answer(
    tool: AssistPipelineTools, agent: RecordingAgent
) -> None:
    result = await tool.ha_manage_pipeline(
        action="process", sentence="turn on the kitchen light", agent_id=agent.agent_id
    )

    (seen,) = agent.inputs
    assert seen.text == "turn on the kitchen light"
    kitchen = {"name": "Kitchen light", "type": "entity", "id": "light.kitchen"}
    assert result == {
        "success": True,
        "operation": "process",
        "pipeline_id": None,
        "agent_id": agent.agent_id,
        "response_type": "action_done",
        "speech": "Turned on the light",
        "language": seen.language,
        "conversation_id": "01JABCDEF",
        "continue_conversation": True,
        # Core dropped "targets" in 2026.4; the split lists carry the targets.
        "targets": [],
        "service_calls": {"success": [kitchen], "failed": []},
        "error_code": None,
        "message": "Turned on the light",
    }


async def test_process_passes_language_and_conversation_on_to_the_agent(
    tool: AssistPipelineTools, agent: RecordingAgent
) -> None:
    await tool.ha_manage_pipeline(
        action="process",
        sentence="and the hallway?",
        language="de",
        conversation_id="01JPREV",
        agent_id=agent.agent_id,
    )

    (seen,) = agent.inputs
    assert (seen.language, seen.conversation_id) == ("de", "01JPREV")


async def test_process_without_an_agent_reaches_the_built_in_one_unwarned(
    tool: AssistPipelineTools, core_client: HomeAssistantClient, agent: RecordingAgent
) -> None:
    """No pipeline is read first, and Assist declining a sentence is an
    answer: neither a tool failure nor a warning."""
    with sent_messages(core_client) as spy:
        result = await tool.ha_manage_pipeline(
            action="process", sentence="make me a sandwich"
        )

    spy.assert_not_awaited()
    assert agent.inputs == []
    assert result["success"] is True
    assert result["response_type"] == "error"
    assert result["error_code"] == "no_intent_match"
    assert result["service_calls"] == {"success": [], "failed": []}
    assert "warnings" not in result


async def test_process_warns_when_some_targets_failed(
    tool: AssistPipelineTools, agent: RecordingAgent
) -> None:
    """Core raises only when no target succeeded, so one failing integration
    still comes back as action_done with the intent's ordinary speech."""

    def partly_failed(response: intent.IntentResponse) -> None:
        response.async_set_speech("Turned on the lights")
        response.async_set_results([_KITCHEN], [_HALLWAY])

    agent.reply = partly_failed

    result = await tool.ha_manage_pipeline(
        action="process", sentence="turn on the lights", agent_id=agent.agent_id
    )

    assert result["message"] == "Turned on the lights"
    assert result["service_calls"]["failed"] == [
        {"name": "Hallway light", "type": "entity", "id": "light.hallway"}
    ]
    assert result["warnings"] == [
        "1 target(s) the intent resolved failed: light.hallway. "
        "See service_calls.failed."
    ]


async def test_process_warns_when_handling_the_intent_failed(
    tool: AssistPipelineTools, agent: RecordingAgent
) -> None:
    def failed_to_handle(response: intent.IntentResponse) -> None:
        response.async_set_error(
            intent.IntentResponseErrorCode.FAILED_TO_HANDLE,
            "Sorry, something went wrong",
        )

    agent.reply = failed_to_handle

    result = await tool.ha_manage_pipeline(
        action="process", sentence="turn on the kitchen light", agent_id=agent.agent_id
    )

    assert result["success"] is True
    assert result["error_code"] == "failed_to_handle"
    assert result["warnings"] == [
        "Assist matched the intent but handling it failed. The command was "
        "understood and not carried out."
    ]


async def test_process_survives_an_agent_that_acts_without_speaking(
    tool: AssistPipelineTools, agent: RecordingAgent
) -> None:
    agent.reply = lambda response: response.async_set_results([_KITCHEN])

    result = await tool.ha_manage_pipeline(
        action="process", sentence="turn on the kitchen light", agent_id=agent.agent_id
    )

    assert result["success"] is True
    assert result["speech"] is None
    assert result["message"] == "Assist responded with action_done"
