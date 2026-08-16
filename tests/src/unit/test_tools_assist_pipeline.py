"""Unit tests for Assist pipeline MCP tools."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_voice_assistant import VoiceAssistantTools


def _pipeline(**overrides):
    """Build a complete Assist pipeline fixture."""
    pipeline = {
        "id": "pipeline_1",
        "conversation_engine": "conversation.openai_conversation",
        "conversation_language": "*",
        "language": "en",
        "name": "Voice",
        "stt_engine": "stt.home_assistant_cloud",
        "stt_language": "en-US",
        "tts_engine": "tts.home_assistant_cloud",
        "tts_language": "en-US",
        "tts_voice": "MonicaNeural",
        "wake_word_entity": "wake_word.openwakeword",
        "wake_word_id": "hey_mycroft_v0.1",
        "prefer_local_intents": True,
    }
    pipeline.update(overrides)
    return pipeline


def _conversation_result(**overrides):
    """Build an HA ``/conversation/process`` response body."""
    result = {
        "response": {
            "speech": {"plain": {"speech": "Turned on the light", "extra_data": None}},
            "card": {},
            "language": "en",
            "response_type": "action_done",
            "data": {
                # Cores before 2026.4 send targets alongside success/failed;
                # a distinct entity id here keeps the key pinned to what HA
                # sent rather than to the extraction's own default.
                "targets": [
                    {"name": "Kitchen light", "type": "entity", "id": "light.kitchen"}
                ],
                "success": [
                    {"name": "Kitchen light", "type": "entity", "id": "light.kitchen"}
                ],
                "failed": [],
            },
        },
        "conversation_id": "01JABCDEF",
        # True, not the extraction's own False default, so the key is pinned to
        # what HA sent. ConversationResult.as_dict always carries it.
        "continue_conversation": True,
    }
    result.update(overrides)
    return result


@pytest.fixture
def mock_client():
    """Create a mock Home Assistant client."""
    client = MagicMock()
    client.send_websocket_message = AsyncMock()
    client._request = AsyncMock()
    return client


@pytest.fixture
def tools(mock_client):
    """Create VoiceAssistantTools instance."""
    return VoiceAssistantTools(mock_client)


async def test_get_assist_pipeline_lists_pipelines_when_id_omitted(tools):
    """Omitting pipeline_id should list pipelines and preferred_pipeline."""
    tools._client.send_websocket_message.return_value = {
        "success": True,
        "result": {
            "pipelines": [
                {"id": "pipeline_1", "name": "Home"},
                {"id": "conversation.home_assistant", "name": "Home Assistant"},
            ],
            "preferred_pipeline": "pipeline_1",
        },
    }

    result = await tools.ha_manage_pipeline(action="list")

    assert result == {
        "success": True,
        "operation": "list",
        "count": 2,
        "pipelines": [
            {"id": "pipeline_1", "name": "Home"},
            {"id": "conversation.home_assistant", "name": "Home Assistant"},
        ],
        "preferred_pipeline": "pipeline_1",
        "message": "Found 2 Assist pipeline(s)",
    }
    tools._client.send_websocket_message.assert_awaited_once_with(
        {"type": "assist_pipeline/pipeline/list"}
    )


async def test_get_assist_pipeline_fetches_specific_pipeline(tools):
    """Providing pipeline_id should fetch that pipeline."""
    tools._client.send_websocket_message.return_value = {
        "success": True,
        "result": {"id": "pipeline_1", "name": "Home"},
    }

    result = await tools.ha_manage_pipeline(action="get", pipeline_id="pipeline_1")

    assert result == {
        "success": True,
        "operation": "get",
        "pipeline_id": "pipeline_1",
        "pipeline": {"id": "pipeline_1", "name": "Home"},
        "message": "Found Assist pipeline: Home",
    }
    tools._client.send_websocket_message.assert_awaited_once_with(
        {"type": "assist_pipeline/pipeline/get", "pipeline_id": "pipeline_1"}
    )


async def test_set_preferred_assist_pipeline_sends_preferred_message(tools):
    """Setting the preferred pipeline should use the set_preferred websocket command."""
    tools._client.send_websocket_message.return_value = {
        "success": True,
        "result": None,
    }

    result = await tools.ha_manage_pipeline(
        action="set_preferred", pipeline_id="pipeline_1"
    )

    assert result == {
        "success": True,
        "operation": "set_preferred",
        "pipeline_id": "pipeline_1",
        "message": "Successfully set preferred Assist pipeline: pipeline_1",
    }
    tools._client.send_websocket_message.assert_awaited_once_with(
        {"type": "assist_pipeline/pipeline/set_preferred", "pipeline_id": "pipeline_1"}
    )


async def test_set_assist_pipeline_creates_from_preferred_pipeline(tools):
    """Creating a pipeline should clone the preferred pipeline and override fields."""
    preferred_pipeline = {
        "id": "preferred",
        "conversation_engine": "conversation.openai_conversation",
        "conversation_language": "*",
        "language": "en",
        "name": "Extended GPT4o",
        "stt_engine": "stt.home_assistant_cloud",
        "stt_language": "en-US",
        "tts_engine": "tts.home_assistant_cloud",
        "tts_language": "en-US",
        "tts_voice": "MonicaNeural",
        "wake_word_entity": "wake_word.openwakeword",
        "wake_word_id": "hey_mycroft_v0.1",
        "prefer_local_intents": False,
    }
    created_pipeline = {
        **preferred_pipeline,
        "id": "new_pipeline",
        "conversation_engine": "conversation.local_llm",
        "name": "Local Conversation",
    }
    tools._client.send_websocket_message.side_effect = [
        {"success": True, "result": preferred_pipeline},
        {"success": True, "result": created_pipeline},
    ]

    result = await tools.ha_manage_pipeline(
        action="create",
        name="Local Conversation",
        conversation_engine="conversation.local_llm",
    )

    assert result == {
        "success": True,
        "operation": "created",
        "pipeline_id": "new_pipeline",
        "pipeline": created_pipeline,
        "preferred_changed": False,
        "message": "Assist pipeline created: Local Conversation",
    }
    assert tools._client.send_websocket_message.await_args_list[0].args[0] == {
        "type": "assist_pipeline/pipeline/get"
    }
    assert tools._client.send_websocket_message.await_args_list[1].args[0] == {
        "type": "assist_pipeline/pipeline/create",
        "conversation_engine": "conversation.local_llm",
        "conversation_language": "*",
        "language": "en",
        "name": "Local Conversation",
        "stt_engine": "stt.home_assistant_cloud",
        "stt_language": "en-US",
        "tts_engine": "tts.home_assistant_cloud",
        "tts_language": "en-US",
        "tts_voice": "MonicaNeural",
        "wake_word_entity": "wake_word.openwakeword",
        "wake_word_id": "hey_mycroft_v0.1",
        "prefer_local_intents": False,
    }


async def test_set_assist_pipeline_updates_existing_pipeline_by_merging(tools):
    """Updating a pipeline should fetch existing values and send a full payload."""
    existing_pipeline = {
        "id": "pipeline_1",
        "conversation_engine": "conversation.openai_conversation",
        "conversation_language": "*",
        "language": "en",
        "name": "Voice",
        "stt_engine": "stt.home_assistant_cloud",
        "stt_language": "en-US",
        "tts_engine": "tts.home_assistant_cloud",
        "tts_language": "en-US",
        "tts_voice": "MonicaNeural",
        "wake_word_entity": "wake_word.openwakeword",
        "wake_word_id": "hey_mycroft_v0.1",
        "prefer_local_intents": True,
    }
    updated_pipeline = {
        **existing_pipeline,
        "conversation_engine": "conversation.local_llm",
    }
    tools._client.send_websocket_message.side_effect = [
        {"success": True, "result": existing_pipeline},
        {"success": True, "result": updated_pipeline},
    ]

    result = await tools.ha_manage_pipeline(
        action="update",
        pipeline_id="pipeline_1",
        conversation_engine="conversation.local_llm",
    )

    assert result["success"] is True
    assert result["operation"] == "updated"
    assert result["pipeline_id"] == "pipeline_1"
    assert result["pipeline"] == updated_pipeline
    assert tools._client.send_websocket_message.await_args_list[0].args[0] == {
        "type": "assist_pipeline/pipeline/get",
        "pipeline_id": "pipeline_1",
    }
    assert tools._client.send_websocket_message.await_args_list[1].args[0] == {
        "type": "assist_pipeline/pipeline/update",
        "pipeline_id": "pipeline_1",
        "conversation_engine": "conversation.local_llm",
        "conversation_language": "*",
        "language": "en",
        "name": "Voice",
        "stt_engine": "stt.home_assistant_cloud",
        "stt_language": "en-US",
        "tts_engine": "tts.home_assistant_cloud",
        "tts_language": "en-US",
        "tts_voice": "MonicaNeural",
        "wake_word_entity": "wake_word.openwakeword",
        "wake_word_id": "hey_mycroft_v0.1",
        "prefer_local_intents": True,
    }


async def test_set_assist_pipeline_normalizes_nullable_empty_strings(tools):
    """Empty string should clear nullable STT/TTS/wake-word fields."""
    existing_pipeline = _pipeline()
    updated_pipeline = {
        **existing_pipeline,
        "stt_engine": None,
        "tts_voice": None,
    }
    tools._client.send_websocket_message.side_effect = [
        {"success": True, "result": existing_pipeline},
        {"success": True, "result": updated_pipeline},
    ]

    result = await tools.ha_manage_pipeline(
        action="update",
        pipeline_id="pipeline_1",
        stt_engine="",
        tts_voice="",
    )

    assert result["success"] is True
    assert (
        tools._client.send_websocket_message.await_args_list[1].args[0]["stt_engine"]
        is None
    )
    assert (
        tools._client.send_websocket_message.await_args_list[1].args[0]["tts_voice"]
        is None
    )


async def test_set_assist_pipeline_rejects_required_empty_strings(tools):
    """Empty strings on required/non-nullable fields should fail before HA."""
    with pytest.raises(ToolError) as exc_info:
        await tools.ha_manage_pipeline(
            action="update",
            pipeline_id="pipeline_1",
            name="",
        )

    error_data = json.loads(str(exc_info.value))
    assert error_data["success"] is False
    assert error_data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "name" in error_data["error"]["message"]
    tools._client.send_websocket_message.assert_not_awaited()


async def test_set_assist_pipeline_creates_from_explicit_base_pipeline(tools):
    """base_pipeline_id should drive the create clone source when supplied."""
    base_pipeline = _pipeline(id="base_pipeline", name="Base Voice")
    created_pipeline = {
        **base_pipeline,
        "id": "new_pipeline",
        "conversation_engine": "conversation.local_llm",
        "name": "Local Conversation",
    }
    tools._client.send_websocket_message.side_effect = [
        {"success": True, "result": base_pipeline},
        {"success": True, "result": created_pipeline},
    ]

    result = await tools.ha_manage_pipeline(
        action="create",
        base_pipeline_id="base_pipeline",
        name="Local Conversation",
        conversation_engine="conversation.local_llm",
    )

    assert result["pipeline_id"] == "new_pipeline"
    assert tools._client.send_websocket_message.await_args_list[0].args[0] == {
        "type": "assist_pipeline/pipeline/get",
        "pipeline_id": "base_pipeline",
    }


async def test_set_assist_pipeline_can_make_created_pipeline_preferred(tools):
    """make_preferred should set the resulting pipeline as preferred."""
    preferred_pipeline = {
        "id": "preferred",
        "conversation_engine": "conversation.openai_conversation",
        "conversation_language": "*",
        "language": "en",
        "name": "Extended GPT4o",
        "stt_engine": None,
        "stt_language": None,
        "tts_engine": None,
        "tts_language": None,
        "tts_voice": None,
        "wake_word_entity": None,
        "wake_word_id": None,
        "prefer_local_intents": False,
    }
    created_pipeline = {
        **preferred_pipeline,
        "id": "new_pipeline",
        "conversation_engine": "conversation.local_llm",
        "name": "Local Conversation",
    }
    tools._client.send_websocket_message.side_effect = [
        {"success": True, "result": preferred_pipeline},
        {"success": True, "result": created_pipeline},
        {"success": True, "result": None},
    ]

    result = await tools.ha_manage_pipeline(
        action="create",
        name="Local Conversation",
        conversation_engine="conversation.local_llm",
        make_preferred=True,
    )

    assert result["success"] is True
    assert result["pipeline_id"] == "new_pipeline"
    assert result["preferred_changed"] is True
    assert tools._client.send_websocket_message.await_args_list[2].args[0] == {
        "type": "assist_pipeline/pipeline/set_preferred",
        "pipeline_id": "new_pipeline",
    }


async def test_set_assist_pipeline_can_make_updated_pipeline_preferred(tools):
    """make_preferred should set an updated pipeline as preferred."""
    existing_pipeline = _pipeline()
    updated_pipeline = {
        **existing_pipeline,
        "conversation_engine": "conversation.local_llm",
    }
    tools._client.send_websocket_message.side_effect = [
        {"success": True, "result": existing_pipeline},
        {"success": True, "result": updated_pipeline},
        {"success": True, "result": None},
    ]

    result = await tools.ha_manage_pipeline(
        action="update",
        pipeline_id="pipeline_1",
        conversation_engine="conversation.local_llm",
        make_preferred=True,
    )

    assert result["success"] is True
    assert result["preferred_changed"] is True
    assert tools._client.send_websocket_message.await_args_list[2].args[0] == {
        "type": "assist_pipeline/pipeline/set_preferred",
        "pipeline_id": "pipeline_1",
    }


async def test_manage_pipeline_create_requires_name_and_conversation_engine(tools):
    """Create should fail locally when required fields are missing."""
    with pytest.raises(ToolError) as exc_info:
        await tools.ha_manage_pipeline(
            action="create",
            conversation_engine="conversation.local_llm",
        )

    error_data = json.loads(str(exc_info.value))
    assert error_data["error"]["code"] == "VALIDATION_MISSING_PARAMETER"
    assert "name and conversation_engine" in error_data["error"]["message"]
    tools._client.send_websocket_message.assert_not_awaited()


async def test_manage_pipeline_update_requires_pipeline_id(tools):
    """Update should fail locally when pipeline_id is missing."""
    with pytest.raises(ToolError) as exc_info:
        await tools.ha_manage_pipeline(
            action="update",
            conversation_engine="conversation.local_llm",
        )

    error_data = json.loads(str(exc_info.value))
    assert error_data["error"]["code"] == "VALIDATION_MISSING_PARAMETER"
    assert "pipeline_id" in error_data["error"]["message"]
    tools._client.send_websocket_message.assert_not_awaited()


async def test_manage_pipeline_update_requires_changes(tools):
    """Update with no fields and no make_preferred should fail locally."""
    with pytest.raises(ToolError) as exc_info:
        await tools.ha_manage_pipeline(
            action="update",
            pipeline_id="pipeline_1",
        )

    error_data = json.loads(str(exc_info.value))
    assert error_data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "No Assist pipeline changes requested" in error_data["error"]["message"]
    tools._client.send_websocket_message.assert_not_awaited()


async def test_manage_pipeline_get_requires_pipeline_id(tools):
    """Get should fail locally when pipeline_id is omitted."""
    with pytest.raises(ToolError) as exc_info:
        await tools.ha_manage_pipeline(action="get")

    error_data = json.loads(str(exc_info.value))
    assert error_data["error"]["code"] == "VALIDATION_MISSING_PARAMETER"
    assert "pipeline_id" in error_data["error"]["message"]
    tools._client.send_websocket_message.assert_not_awaited()


async def test_manage_pipeline_create_rejects_unexpected_write_response(tools):
    """Create should reject non-dict write responses."""
    tools._client.send_websocket_message.side_effect = [
        {"success": True, "result": _pipeline(id="preferred")},
        {"success": True, "result": None},
    ]

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_manage_pipeline(
            action="create",
            name="Local Conversation",
            conversation_engine="conversation.local_llm",
        )

    error_data = json.loads(str(exc_info.value))
    assert error_data["error"]["code"] == "SERVICE_CALL_FAILED"
    assert "Unexpected Assist pipeline write response" in error_data["error"]["message"]


async def test_manage_pipeline_update_rejects_unexpected_write_response(tools):
    """Update should reject non-dict write responses."""
    tools._client.send_websocket_message.side_effect = [
        {"success": True, "result": _pipeline()},
        {"success": True, "result": None},
    ]

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_manage_pipeline(
            action="update",
            pipeline_id="pipeline_1",
            conversation_engine="conversation.local_llm",
        )

    error_data = json.loads(str(exc_info.value))
    assert error_data["error"]["code"] == "SERVICE_CALL_FAILED"
    assert "Unexpected Assist pipeline write response" in error_data["error"]["message"]


async def test_manage_pipeline_create_without_preferred_has_targeted_error(tools):
    """Create without base should explain the missing preferred pipeline."""
    tools._client.send_websocket_message.return_value = {
        "success": True,
        "result": None,
    }

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_manage_pipeline(
            action="create",
            name="Local Conversation",
            conversation_engine="conversation.local_llm",
        )

    error_data = json.loads(str(exc_info.value))
    assert error_data["error"]["code"] == "SERVICE_CALL_FAILED"
    assert "No preferred Assist pipeline" in error_data["error"]["message"]


async def test_set_assist_pipeline_raises_tool_error_on_create_failure(tools):
    """HA create failures should become structured ToolError responses."""
    preferred_pipeline = {
        "id": "preferred",
        "conversation_engine": "conversation.openai_conversation",
        "conversation_language": "*",
        "language": "en",
        "name": "Extended GPT4o",
        "stt_engine": None,
        "stt_language": None,
        "tts_engine": None,
        "tts_language": None,
        "tts_voice": None,
        "wake_word_entity": None,
        "wake_word_id": None,
    }
    tools._client.send_websocket_message.side_effect = [
        {"success": True, "result": preferred_pipeline},
        {"success": False, "error": {"code": "invalid_format", "message": "bad"}},
    ]

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_manage_pipeline(
            action="create",
            name="Local Conversation",
            conversation_engine="conversation.local_llm",
        )

    error_data = json.loads(str(exc_info.value))
    assert error_data["success"] is False
    assert error_data["error"]["code"] == "SERVICE_CALL_FAILED"
    assert "bad" in error_data["error"]["message"]
    assert error_data["operation"] == "create"


async def test_get_assist_pipeline_raises_tool_error_on_ha_failure(tools):
    """HA websocket failure responses should become structured ToolError."""
    tools._client.send_websocket_message.return_value = {
        "success": False,
        "error": {"code": "not_found", "message": "unknown item"},
    }

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_manage_pipeline(action="get", pipeline_id="missing")

    error_data = json.loads(str(exc_info.value))
    assert error_data["success"] is False
    assert error_data["error"]["code"] == "SERVICE_CALL_FAILED"
    assert "unknown item" in error_data["error"]["message"]
    assert error_data["pipeline_id"] == "missing"


async def test_set_preferred_assist_pipeline_raises_tool_error_on_ha_failure(tools):
    """set_preferred HA failures should become structured ToolError."""
    tools._client.send_websocket_message.return_value = {
        "success": False,
        "error": {"code": "not_found", "message": "unknown item"},
    }

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_manage_pipeline(action="set_preferred", pipeline_id="missing")

    error_data = json.loads(str(exc_info.value))
    assert error_data["success"] is False
    assert error_data["error"]["code"] == "SERVICE_CALL_FAILED"
    assert "unknown item" in error_data["error"]["message"]
    assert error_data["pipeline_id"] == "missing"


async def test_get_assist_pipeline_maps_unexpected_exception(tools):
    """Unexpected client exceptions should be mapped through structured errors."""
    tools._client.send_websocket_message.side_effect = RuntimeError("network down")

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_manage_pipeline(action="list")

    error_data = json.loads(str(exc_info.value))
    assert error_data["success"] is False
    assert error_data["error"]["code"] == "INTERNAL_ERROR"
    assert "network down" in error_data["error"]["details"]


async def test_process_sends_sentence_and_normalizes_response(tools):
    """action='process' posts the sentence and flattens HA's answer."""
    tools._client._request.return_value = _conversation_result()

    result = await tools.ha_manage_pipeline(
        action="process", sentence="turn on the kitchen light"
    )

    tools._client._request.assert_awaited_once_with(
        "POST",
        "/conversation/process",
        json={"text": "turn on the kitchen light"},
    )
    # No pipeline_id was passed, so nothing may fetch a pipeline first.
    tools._client.send_websocket_message.assert_not_awaited()
    assert result == {
        "success": True,
        "operation": "process",
        "pipeline_id": None,
        "agent_id": None,
        "response_type": "action_done",
        "speech": "Turned on the light",
        "language": "en",
        "conversation_id": "01JABCDEF",
        "continue_conversation": True,
        "targets": [{"name": "Kitchen light", "type": "entity", "id": "light.kitchen"}],
        "service_calls": {
            "success": [
                {"name": "Kitchen light", "type": "entity", "id": "light.kitchen"}
            ],
            "failed": [],
        },
        "error_code": None,
        "message": "Turned on the light",
    }
    # A fully successful intent carries no warnings key at all.
    assert "warnings" not in result


async def test_process_passes_optional_conversation_fields(tools):
    """language, conversation_id and agent_id reach HA when supplied."""
    tools._client._request.return_value = _conversation_result()

    await tools.ha_manage_pipeline(
        action="process",
        sentence="and the hallway?",
        language="de",
        conversation_id="01JPREV",
        agent_id="conversation.home_assistant",
    )

    tools._client._request.assert_awaited_once_with(
        "POST",
        "/conversation/process",
        json={
            "text": "and the hallway?",
            "language": "de",
            "conversation_id": "01JPREV",
            "agent_id": "conversation.home_assistant",
        },
    )


async def test_process_takes_agent_and_language_from_pipeline(tools):
    """pipeline_id resolves the agent, and '*' falls back to the STT language."""
    tools._client.send_websocket_message.return_value = {
        "success": True,
        "result": _pipeline(),
    }
    tools._client._request.return_value = _conversation_result()

    result = await tools.ha_manage_pipeline(
        action="process",
        sentence="turn on the kitchen light",
        pipeline_id="pipeline_1",
    )

    tools._client.send_websocket_message.assert_awaited_once_with(
        {"type": "assist_pipeline/pipeline/get", "pipeline_id": "pipeline_1"}
    )
    tools._client._request.assert_awaited_once_with(
        "POST",
        "/conversation/process",
        json={
            "text": "turn on the kitchen light",
            "language": "en-US",
            "agent_id": "conversation.openai_conversation",
        },
    )
    assert result["pipeline_id"] == "pipeline_1"
    assert result["agent_id"] == "conversation.openai_conversation"


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        pytest.param(
            {"stt_language": "zh-CN", "tts_language": "zh-TW", "language": "zh"},
            "zh-CN",
            id="stt-beats-tts-and-language",
        ),
        pytest.param(
            {"stt_language": None, "tts_language": "zh-TW", "language": "zh"},
            "zh-TW",
            id="tts-when-no-stt",
        ),
        pytest.param(
            {"stt_language": None, "tts_language": None, "language": "zh"},
            "zh",
            id="pipeline-language-last",
        ),
        pytest.param(
            {"stt_language": None, "tts_language": None, "language": None},
            None,
            id="nothing-resolvable-omits-the-key",
        ),
        pytest.param(
            {
                "conversation_language": "",
                "stt_language": "zh-CN",
                "tts_language": None,
                "language": "zh",
            },
            "zh-CN",
            id="empty-conversation-language-falls-through",
        ),
    ],
)
async def test_process_resolves_the_pipeline_language_ladder(
    tools, overrides, expected
):
    """Each rung of the STT/TTS/language fallback, and its precedence.

    Core resolves MATCH_ALL the same way and prefers the more specific STT
    value, so swapping two rungs is a real behaviour change. An empty
    conversation_language is not a language either: HA's async_converse
    substitutes its default for a missing language but not for an empty
    string, so "" would go on the wire verbatim.
    """
    tools._client.send_websocket_message.return_value = {
        "success": True,
        "result": _pipeline(**{"conversation_language": "*", **overrides}),
    }
    tools._client._request.return_value = _conversation_result()

    await tools.ha_manage_pipeline(
        action="process", sentence="turn on the light", pipeline_id="pipeline_1"
    )

    payload = tools._client._request.await_args.kwargs["json"]
    if expected is None:
        assert "language" not in payload
    else:
        assert payload["language"] == expected


async def test_process_uses_pipeline_conversation_language_when_specific(tools):
    """A pipeline naming a real conversation language wins over STT/TTS."""
    tools._client.send_websocket_message.return_value = {
        "success": True,
        "result": _pipeline(conversation_language="nl"),
    }
    tools._client._request.return_value = _conversation_result()

    await tools.ha_manage_pipeline(
        action="process", sentence="doe het licht aan", pipeline_id="pipeline_1"
    )

    assert tools._client._request.await_args.kwargs["json"]["language"] == "nl"


async def test_process_explicit_arguments_override_the_pipeline(tools):
    """Explicit agent_id and language are not overwritten by pipeline_id."""
    tools._client.send_websocket_message.return_value = {
        "success": True,
        "result": _pipeline(),
    }
    tools._client._request.return_value = _conversation_result()

    await tools.ha_manage_pipeline(
        action="process",
        sentence="turn on the kitchen light",
        pipeline_id="pipeline_1",
        agent_id="conversation.home_assistant",
        language="fr",
    )

    payload = tools._client._request.await_args.kwargs["json"]
    assert payload["agent_id"] == "conversation.home_assistant"
    assert payload["language"] == "fr"


@pytest.mark.parametrize("sentence", [None, "", "   "])
async def test_process_requires_a_non_empty_sentence(tools, sentence):
    """process without a usable sentence fails before any HA call."""
    with pytest.raises(ToolError) as exc_info:
        await tools.ha_manage_pipeline(action="process", sentence=sentence)

    error_data = json.loads(str(exc_info.value))
    assert error_data["success"] is False
    assert error_data["error"]["code"] == "VALIDATION_MISSING_PARAMETER"
    tools._client._request.assert_not_awaited()


async def test_process_reports_an_unmatched_sentence_without_raising(tools):
    """Assist declining a sentence is an answer, not a tool failure."""
    tools._client._request.return_value = _conversation_result(
        response={
            "speech": {"plain": {"speech": "Sorry, I couldn't understand that"}},
            "card": {},
            "language": "en",
            "response_type": "error",
            "data": {"code": "no_intent_match"},
        }
    )

    result = await tools.ha_manage_pipeline(
        action="process", sentence="make me a sandwich"
    )

    assert result["success"] is True
    assert result["response_type"] == "error"
    assert result["error_code"] == "no_intent_match"
    assert result["service_calls"] == {"success": [], "failed": []}


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({}, id="no-envelope"),
        pytest.param({"response": "not a mapping"}, id="envelope-not-a-mapping"),
        pytest.param([], id="body-not-a-mapping"),
        pytest.param({"response": {}}, id="empty-envelope"),
        pytest.param({"response": {"data": {}, "speech": {}}}, id="no-response-type"),
        pytest.param(
            {"response": {"response_type": "action_done", "data": "nope"}},
            id="data-not-a-mapping",
        ),
        pytest.param(
            {"response": {"response_type": "action_done", "speech": {}}},
            id="no-data",
        ),
    ],
)
async def test_process_rejects_a_malformed_conversation_response(tools, body):
    """A malformed envelope fails instead of reporting success.

    ``IntentResponse.as_dict`` always writes both ``response_type`` and
    ``data``, so every body here is malformed rather than a variant: no
    envelope at all (``{}``, which is what ``_request`` normalizes an empty or
    undecodable 2xx body to), a non-mapping envelope, a non-mapping body, an
    empty envelope, and either required key missing or of the wrong type.
    Accepting any of them would return success with a null response_type or
    with service_calls indistinguishable from an intent that touched nothing.
    """
    tools._client._request.return_value = body

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_manage_pipeline(
            action="process", sentence="unlock the front door for Alex"
        )

    raw = str(exc_info.value)
    # Same guarantee as the transport-failure path: this raise site must not
    # echo the utterance back into an error payload either.
    assert "Alex" not in raw
    error_data = json.loads(raw)
    assert error_data["success"] is False
    assert error_data["error"]["code"] == "SERVICE_CALL_FAILED"


async def test_process_keeps_the_sentence_out_of_error_payloads(tools):
    """A transport failure must not echo the utterance back."""
    tools._client._request.side_effect = RuntimeError("upstream said no")

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_manage_pipeline(
            action="process", sentence="unlock the front door for Alex"
        )

    raw = str(exc_info.value)
    assert "Alex" not in raw
    error_data = json.loads(raw)
    assert error_data["error"]["code"] == "INTERNAL_ERROR"


async def test_process_warns_when_some_targets_failed(tools):
    """A partial intent failure is reported, not hidden in a nested list.

    HA raises only when no target succeeded, so one failing integration still
    comes back as action_done with the intent's ordinary speech.
    """
    tools._client._request.return_value = _conversation_result(
        response={
            "speech": {"plain": {"speech": "Turned on the lights"}},
            "card": {},
            "language": "en",
            "response_type": "action_done",
            "data": {
                "success": [
                    {"name": "Kitchen light", "type": "entity", "id": "light.kitchen"}
                ],
                "failed": [
                    {"name": "Hallway light", "type": "entity", "id": "light.hallway"}
                ],
            },
        }
    )

    result = await tools.ha_manage_pipeline(
        action="process", sentence="turn on the lights"
    )

    assert result["success"] is True
    assert result["message"] == "Turned on the lights"
    assert result["service_calls"]["failed"] == [
        {"name": "Hallway light", "type": "entity", "id": "light.hallway"}
    ]
    assert result["warnings"] == [
        "1 target(s) the intent resolved failed: light.hallway. "
        "See service_calls.failed."
    ]


async def test_process_warns_when_handling_the_intent_failed(tools):
    """failed_to_handle is HA reporting a matched intent that did not run."""
    tools._client._request.return_value = _conversation_result(
        response={
            "speech": {"plain": {"speech": "Sorry, something went wrong"}},
            "card": {},
            "language": "en",
            "response_type": "error",
            "data": {"code": "failed_to_handle"},
        }
    )

    result = await tools.ha_manage_pipeline(
        action="process", sentence="turn on the kitchen light"
    )

    assert result["success"] is True
    assert result["error_code"] == "failed_to_handle"
    assert result["warnings"] == [
        "Assist matched the intent but handling it failed. The command was "
        "understood and not carried out."
    ]


async def test_process_does_not_warn_about_an_unmatched_sentence(tools):
    """no_intent_match is Assist answering, so it carries no warning."""
    tools._client._request.return_value = _conversation_result(
        response={
            "speech": {"plain": {"speech": "Sorry, I couldn't understand that"}},
            "card": {},
            "language": "en",
            "response_type": "error",
            "data": {"code": "no_intent_match"},
        }
    )

    result = await tools.ha_manage_pipeline(
        action="process", sentence="make me a sandwich"
    )

    assert result["error_code"] == "no_intent_match"
    assert "warnings" not in result


@pytest.mark.parametrize(
    "speech",
    [
        pytest.param(None, id="no-speech-key"),
        pytest.param("just a string", id="speech-not-a-mapping"),
        pytest.param({}, id="no-plain-key"),
        pytest.param({"plain": "just a string"}, id="plain-not-a-mapping"),
        pytest.param({"plain": {}}, id="no-speech-text"),
        pytest.param({"plain": {"speech": 42}}, id="speech-text-not-a-string"),
    ],
)
async def test_process_survives_a_response_without_usable_speech(tools, speech):
    """An agent that acts without speaking must not turn into INTERNAL_ERROR.

    Every guard in the speech extraction is a shape HA's own response can
    take; reaching into the nested keys unguarded would raise here and report
    a call that ran the intent as a tool failure.
    """
    response = {
        "card": {},
        "language": "en",
        "response_type": "action_done",
        "data": {"success": [], "failed": []},
    }
    if speech is not None:
        response["speech"] = speech
    tools._client._request.return_value = _conversation_result(response=response)

    result = await tools.ha_manage_pipeline(
        action="process", sentence="turn on the kitchen light"
    )

    assert result["success"] is True
    assert result["speech"] is None
    assert result["message"] == "Assist responded with action_done"
