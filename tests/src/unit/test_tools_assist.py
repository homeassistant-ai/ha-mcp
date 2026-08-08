"""Unit tests for the Assist conversation pipeline tool (ha_intent_process)."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_assist import AssistTools


@pytest.fixture
def mock_client():
    """Mock HA client whose _request is an AsyncMock."""
    client = MagicMock()
    client._request = AsyncMock()
    return client


@pytest.fixture
def tools(mock_client):
    return AssistTools(mock_client)


class TestHaIntentProcessRequestPayload:
    """Verify the outgoing payload to /conversation/process."""

    @pytest.mark.asyncio
    async def test_minimal_payload_only_includes_text(self, mock_client, tools):
        mock_client._request.return_value = {
            "response": {
                "response_type": "action_done",
                "language": "en",
                "speech": {"plain": {"speech": "Turned on the light"}},
                "data": {"targets": [], "success": [], "failed": []},
            },
            "conversation_id": None,
        }

        await tools.ha_intent_process(sentence="turn on the kitchen light")

        mock_client._request.assert_awaited_once()
        call_args = mock_client._request.await_args
        assert call_args.args == ("POST", "/conversation/process")
        assert call_args.kwargs["json"] == {"text": "turn on the kitchen light"}

    @pytest.mark.asyncio
    async def test_all_optional_fields_included_when_set(self, mock_client, tools):
        mock_client._request.return_value = {
            "response": {
                "response_type": "action_done",
                "speech": {"plain": {"speech": ""}},
                "data": {},
                "language": "he",
            },
            "conversation_id": "abc-123",
        }

        await tools.ha_intent_process(
            sentence="הדלק את האור בסלון",
            language="he",
            conversation_id="abc-123",
            agent_id="conversation.home_assistant",
        )

        payload = mock_client._request.await_args.kwargs["json"]
        assert payload == {
            "text": "הדלק את האור בסלון",
            "language": "he",
            "conversation_id": "abc-123",
            "agent_id": "conversation.home_assistant",
        }


class TestHaIntentProcessResponseShape:
    """Verify the normalized response we return to callers."""

    @pytest.mark.asyncio
    async def test_action_done_returns_success_true_and_speech(
        self, mock_client, tools
    ):
        mock_client._request.return_value = {
            "response": {
                "response_type": "action_done",
                "speech": {"plain": {"speech": "Turned on the kitchen light"}},
                "language": "en",
                "data": {
                    "targets": [
                        {
                            "name": "Kitchen Light",
                            "type": "entity",
                            "id": "light.kitchen",
                        }
                    ],
                    "success": [
                        {
                            "name": "Kitchen Light",
                            "type": "entity",
                            "id": "light.kitchen",
                        }
                    ],
                    "failed": [],
                },
            },
            "conversation_id": "conv-1",
        }

        result = await tools.ha_intent_process(sentence="turn on the kitchen light")

        assert result["success"] is True
        assert result["response_type"] == "action_done"
        assert result["speech"] == "Turned on the kitchen light"
        assert result["language"] == "en"
        assert result["conversation_id"] == "conv-1"
        assert result["targets"][0]["id"] == "light.kitchen"
        assert result["service_calls"]["success"][0]["id"] == "light.kitchen"
        assert result["service_calls"]["failed"] == []
        assert result["error_code"] is None
        assert "raw" in result

    @pytest.mark.asyncio
    async def test_error_response_type_returns_success_false_with_code(
        self, mock_client, tools
    ):
        mock_client._request.return_value = {
            "response": {
                "response_type": "error",
                "speech": {"plain": {"speech": "Sorry, I couldn't understand that"}},
                "language": "en",
                "data": {"code": "no_intent_match"},
            },
            "conversation_id": None,
        }

        result = await tools.ha_intent_process(sentence="do the thing")

        assert result["success"] is False
        assert result["response_type"] == "error"
        assert result["error_code"] == "no_intent_match"
        assert result["speech"] == "Sorry, I couldn't understand that"

    @pytest.mark.asyncio
    async def test_missing_speech_returns_none(self, mock_client, tools):
        mock_client._request.return_value = {
            "response": {
                "response_type": "action_done",
                "language": "en",
                "data": {},
            },
            "conversation_id": None,
        }

        result = await tools.ha_intent_process(sentence="hello")
        assert result["speech"] is None

    @pytest.mark.asyncio
    async def test_explicit_language_used_when_response_omits_it(
        self, mock_client, tools
    ):
        mock_client._request.return_value = {
            "response": {
                "response_type": "action_done",
                "speech": {"plain": {"speech": "ok"}},
                "data": {},
            },
            "conversation_id": None,
        }

        result = await tools.ha_intent_process(sentence="ok", language="he")
        assert result["language"] == "he"


class TestHaIntentProcessExceptionHandling:
    """Unexpected exceptions are converted to structured ToolError responses."""

    @pytest.mark.asyncio
    async def test_request_raises_runtime_error_surfaces_as_tool_error(
        self, mock_client, tools
    ):
        mock_client._request.side_effect = RuntimeError("boom")

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_intent_process(sentence="anything")

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert "error" in error_data
