"""
Home Assistant Assist Pipeline Tools.

Routes a natural-language sentence through Home Assistant's Assist
conversation pipeline via POST /api/conversation/process. Same code path
used by HA voice commands. Useful for:
- Checking whether HA can already handle a command natively before
  building an automation
- Validating custom sentence templates during development
- Debugging Assist pipeline issues
- Programmatically testing intent / area / entity name coverage

API reference: https://developers.home-assistant.io/docs/api/rest/#post-apiconversationprocess
"""

import logging
from typing import Annotated, Any

from fastmcp.exceptions import ToolError
from fastmcp.tools import tool
from pydantic import Field

from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    register_tool_methods,
)

logger = logging.getLogger(__name__)


class AssistTools:
    """Assist conversation pipeline tools."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @staticmethod
    def _extract_speech(response: dict[str, Any]) -> str | None:
        """Extract the plain-text speech string from an HA conversation response."""
        speech = response.get("speech")
        if not isinstance(speech, dict):
            return None
        plain = speech.get("plain")
        if not isinstance(plain, dict):
            return None
        text = plain.get("speech")
        return text if isinstance(text, str) else None

    @tool(
        name="ha_intent_process",
        tags={"Assist"},
        annotations={
            "title": "Process Assist Intent",
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
        },
    )
    @log_tool_usage
    async def ha_intent_process(
        self,
        sentence: Annotated[
            str,
            Field(
                description=(
                    "Natural-language command to send through HA's Assist "
                    "conversation pipeline (e.g. 'turn on the kitchen light'). "
                    "Same path a voice command would take. Commands that "
                    "match an intent will execute (turn on lights, set "
                    "temperature, etc.)."
                ),
                min_length=1,
            ),
        ],
        language: Annotated[
            str | None,
            Field(
                description=(
                    "Optional BCP47 language code (e.g. 'en', 'he', 'en-US'). "
                    "If omitted, HA uses its configured default language."
                ),
                default=None,
            ),
        ] = None,
        conversation_id: Annotated[
            str | None,
            Field(
                description=(
                    "Optional conversation ID to maintain context across "
                    "multiple calls (for follow-up questions). If omitted, "
                    "HA starts a new conversation. Returned in the response."
                ),
                default=None,
            ),
        ] = None,
        agent_id: Annotated[
            str | None,
            Field(
                description=(
                    "Optional conversation agent entity_id "
                    "(e.g. 'conversation.home_assistant'). If omitted, "
                    "HA uses the default Assist agent."
                ),
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Run a natural-language sentence through Home Assistant's Assist intent system.

        Calls POST /api/conversation/process — the same endpoint that voice
        clients (Assist devices, mobile app voice input) use. Will EXECUTE
        matched intents (e.g. turn devices on/off, set temperatures), not just
        analyze them. Treat as a write operation.

        USE CASES:
        - Test whether HA can already handle a command natively before building an automation
        - Validate custom sentence templates added under intent_script / conversation
        - Debug why an Assist sentence isn't matching (response_type='error', code='no_intent_match')
        - Reviver agent: check intent coverage for documented use cases

        EXAMPLES:
        - ha_intent_process(sentence="turn on the kitchen light")
        - ha_intent_process(sentence="הדלק את האור בסלון", language="he")
        - ha_intent_process(sentence="what's the bedroom temperature")
        - ha_intent_process(sentence="and the kitchen?", conversation_id="<prev_id>")
        - ha_intent_process(sentence="run good morning", agent_id="conversation.home_assistant")

        RETURNS:
        - success: True unless response_type is 'error'
        - response_type: 'action_done' | 'query_answer' | 'error'
        - speech: Plain-text reply from HA (e.g. "Turned on the light")
        - language: Language code HA used
        - conversation_id: Pass back to chain follow-up calls
        - targets: List of resolved targets (entity / area / domain)
        - service_calls: { success: [...], failed: [...] } — entities affected
        - error_code: When response_type='error' (e.g. 'no_intent_match',
          'no_valid_targets', 'failed_to_handle')
        - raw: Full unmodified HA response for advanced inspection
        """
        payload: dict[str, Any] = {"text": sentence}
        if language is not None:
            payload["language"] = language
        if conversation_id is not None:
            payload["conversation_id"] = conversation_id
        if agent_id is not None:
            payload["agent_id"] = agent_id

        try:
            result = await self._client._request(
                "POST", "/conversation/process", json=payload
            )

            response = result.get("response", {}) if isinstance(result, dict) else {}
            if not isinstance(response, dict):
                response = {}
            data = response.get("data", {}) if isinstance(response, dict) else {}
            if not isinstance(data, dict):
                data = {}

            response_type = response.get("response_type")
            is_error = response_type == "error"

            return {
                "success": not is_error,
                "sentence": sentence,
                "response_type": response_type,
                "speech": self._extract_speech(response),
                "language": response.get("language") or language,
                "conversation_id": result.get("conversation_id")
                if isinstance(result, dict)
                else None,
                "targets": data.get("targets", []),
                "service_calls": {
                    "success": data.get("success", []),
                    "failed": data.get("failed", []),
                },
                "error_code": data.get("code") if is_error else None,
                "raw": result,
            }

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error processing intent for sentence={sentence!r}: {e}")
            exception_to_structured_error(
                e,
                context={
                    "sentence": sentence,
                    "language": language,
                    "conversation_id": conversation_id,
                    "agent_id": agent_id,
                },
            )


def register_assist_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Assist conversation pipeline tools."""
    register_tool_methods(mcp, AssistTools(client))
