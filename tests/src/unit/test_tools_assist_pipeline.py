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


@pytest.fixture
def mock_client():
    """Create a mock Home Assistant client."""
    client = MagicMock()
    client.send_websocket_message = AsyncMock()
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

    result = await tools.ha_manage_pipeline(action="set_preferred", pipeline_id="pipeline_1")

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
    assert tools._client.send_websocket_message.await_args_list[1].args[0][
        "stt_engine"
    ] is None
    assert tools._client.send_websocket_message.await_args_list[1].args[0][
        "tts_voice"
    ] is None


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
