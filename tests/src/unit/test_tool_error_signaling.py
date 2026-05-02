"""Unit tests for tool error signaling via MCP protocol.

This module tests that tool errors are properly signaled at the MCP protocol level
using FastMCP's ToolError exception, which sets isError=true in the response.

Issue #518: Tool errors were not being signaled via isError in MCP protocol responses.
"""

import json

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.errors import ErrorCode, create_error_response, create_validation_error
from ha_mcp.tools.helpers import exception_to_structured_error, raise_tool_error


class TestRaiseToolError:
    """Tests for the raise_tool_error helper function."""

    def test_raises_tool_error(self):
        """raise_tool_error should raise ToolError exception."""
        error_response = create_error_response(
            ErrorCode.ENTITY_NOT_FOUND,
            "Entity light.test not found"
        )

        with pytest.raises(ToolError):
            raise_tool_error(error_response)

    def test_tool_error_contains_structured_json(self):
        """ToolError message should contain the structured error as JSON."""
        error_response = create_error_response(
            ErrorCode.ENTITY_NOT_FOUND,
            "Entity light.test not found",
            suggestions=["Use ha_search_entities() to find valid entity IDs"]
        )

        with pytest.raises(ToolError) as exc_info:
            raise_tool_error(error_response)

        # Parse the error message as JSON
        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert error_data["error"]["code"] == "ENTITY_NOT_FOUND"
        assert error_data["error"]["message"] == "Entity light.test not found"
        # suggestion (singular) is always present, suggestions (plural) only when multiple
        assert "suggestion" in error_data["error"]

    def test_preserves_all_error_fields(self):
        """ToolError should preserve all fields from the error response."""
        error_response = {
            "success": False,
            "error": {
                "code": "TEST_ERROR",
                "message": "Test error message",
                "details": "Additional details",
                "suggestion": "Try this instead",
            },
            "custom_field": "custom_value",
            "entity_id": "light.test",
        }

        with pytest.raises(ToolError) as exc_info:
            raise_tool_error(error_response)

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert error_data["error"]["code"] == "TEST_ERROR"
        assert error_data["error"]["details"] == "Additional details"
        assert error_data["custom_field"] == "custom_value"
        assert error_data["entity_id"] == "light.test"


class TestExceptionToStructuredError:
    """Tests for the exception_to_structured_error function."""

    def test_raises_tool_error_by_default(self):
        """exception_to_structured_error should raise ToolError by default."""
        with pytest.raises(ToolError):
            exception_to_structured_error(ValueError("test error"))

    def test_returns_dict_when_raise_error_false(self):
        """exception_to_structured_error should return dict when raise_error=False."""
        result = exception_to_structured_error(
            ValueError("test error"),
            raise_error=False
        )

        assert isinstance(result, dict)
        assert result["success"] is False
        assert "error" in result

    def test_error_contains_correct_code(self):
        """Structured error should contain appropriate error code."""
        result = exception_to_structured_error(
            ValueError("test validation error"),
            raise_error=False
        )

        assert result["error"]["code"] == "VALIDATION_FAILED"

    def test_context_is_preserved(self):
        """Context should be preserved in the error response for relevant error types."""
        # Use an error type that includes context (API errors with 400 status)
        from ha_mcp.client.rest_client import HomeAssistantAPIError
        error = HomeAssistantAPIError("Bad request", status_code=400)
        result = exception_to_structured_error(
            error,
            context={"entity_id": "light.test", "action": "get"},
            raise_error=False
        )

        # Context is added to the response at top level
        assert result.get("entity_id") == "light.test"
        assert result.get("action") == "get"

    def test_suggestions_embedded_when_raising(self):
        """Suggestions should be embedded in the error and raised as ToolError."""
        suggestions = ["Check connection", "Retry later"]
        with pytest.raises(ToolError) as exc_info:
            exception_to_structured_error(
                ValueError("test error"),
                suggestions=suggestions,
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["suggestions"] == suggestions

    def test_suggestions_embedded_when_returning(self):
        """Suggestions should be embedded in the returned error dict."""
        suggestions = ["Try a different query", "Check spelling"]
        result = exception_to_structured_error(
            ValueError("test error"),
            raise_error=False,
            suggestions=suggestions,
        )

        assert result["error"]["suggestions"] == suggestions

    def test_no_suggestions_when_none(self):
        """No suggestions key should be added when suggestions is None."""
        result = exception_to_structured_error(
            ValueError("test error"),
            raise_error=False,
        )

        assert "suggestions" not in result["error"]

    def test_tool_error_message_is_valid_json(self):
        """ToolError message should be valid JSON."""
        with pytest.raises(ToolError) as exc_info:
            exception_to_structured_error(
                Exception("Connection failed"),
                context={"operation": "connect"},
                raise_error=True,
            )

        # Should not raise JSONDecodeError
        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False


class TestErrorCodeMapping:
    """Tests for exception type to error code mapping."""

    def test_value_error_maps_to_validation_failed(self):
        """ValueError should map to VALIDATION_FAILED error code."""
        result = exception_to_structured_error(
            ValueError("Invalid parameter"),
            raise_error=False
        )
        assert result["error"]["code"] == "VALIDATION_FAILED"

    def test_timeout_error_maps_to_timeout_operation(self):
        """TimeoutError should map to TIMEOUT_OPERATION error code."""
        result = exception_to_structured_error(
            TimeoutError("Request timed out"),
            raise_error=False
        )
        assert result["error"]["code"] == "TIMEOUT_OPERATION"

    def test_connection_error_in_message_maps_correctly(self):
        """Error messages containing 'connection' should be connection errors."""
        result = exception_to_structured_error(
            Exception("Connection refused"),
            raise_error=False
        )
        assert result["error"]["code"] == "CONNECTION_FAILED"


class TestIntegrationWithMCPProtocol:
    """Integration tests simulating MCP protocol behavior."""

    def test_tool_error_enables_client_error_detection(self):
        """ToolError exception should enable MCP clients to detect errors.

        MCP clients can detect tool failures by catching ToolError exceptions,
        which FastMCP converts to isError=true in the protocol response.
        """
        def simulated_tool_call():
            """Simulates a tool call that returns an error."""
            error = create_validation_error("Invalid parameter value")
            raise_tool_error(error)

        # MCP clients can catch ToolError to detect tool failures
        tool_failed = False
        error_message = None

        try:
            simulated_tool_call()
        except ToolError as e:
            tool_failed = True
            error_message = str(e)

        assert tool_failed is True
        assert error_message is not None

        # Error message contains actionable information as JSON
        error_data = json.loads(error_message)
        assert error_data["success"] is False
        assert "VALIDATION" in error_data["error"]["code"]


class TestSchemaAndAuthClassification:
    """Tests for _classify_by_message schema and auth branches (issue #993).

    Pins three behaviours at the classifier boundary:
    1. Supervisor vol.Invalid messages prefixed with "Command failed:" and
       carrying any of the schema markers route to VALIDATION_FAILED.
    2. Messages that merely contain the substring "auth" (e.g.
       "authorized_keys") are NOT misclassified as AUTH_INVALID_TOKEN.
       Only the phrase list (unauthorized, authentication, invalid token,
       access denied) plus the 401 numeric signal match the auth branch.
    3. Typed HA exceptions (HomeAssistantAuthError,
       HomeAssistantConnectionError, HomeAssistantCommandError) route via
       type dispatch in _classify_exception, skipping string
       classification entirely.
    """

    # --- Schema branch: all 5 vol.Invalid markers + the "expected" regex ---

    SCHEMA_MARKER_MESSAGES: tuple[tuple[str, str], ...] = (
        # marker id, full message
        ("missing_option", "Command failed: Missing option 'authorized_keys' in ssh"),
        ("extra_keys", "Command failed: extra keys not allowed @ data['foo']"),
        ("unknown_secret", "Command failed: Unknown secret 'api_key'"),
        ("unknown_type", "Command failed: Unknown type 'timedelta'"),
        ("expected_a", "Command failed: expected a string for dictionary value @ data['host']"),
        ("expected_str", "Command failed: expected str for 'name'"),
        ("expected_int", "Command failed: expected int for 'port'"),
        ("expected_bool", "Command failed: expected bool"),
        ("expected_dict", "Command failed: expected dict"),
        ("expected_list", "Command failed: expected list of strings"),
        ("expected_float", "Command failed: expected float value"),
        ("expected_type", "Command failed: expected type 'str'"),
        ("expected_one_of", "Command failed: expected one of ['a', 'b', 'c']"),
    )

    @pytest.mark.parametrize(
        "marker_id,message",
        SCHEMA_MARKER_MESSAGES,
        ids=[m[0] for m in SCHEMA_MARKER_MESSAGES],
    )
    def test_schema_marker_classified_as_validation_failed(self, marker_id, message):
        """Each vol.Invalid marker under "Command failed:" routes to VALIDATION_FAILED.

        Mutation-testing-style coverage: drop any marker from the source
        tuple in helpers.py and the corresponding parametrized case fails.
        """
        from ha_mcp.client.rest_client import HomeAssistantCommandError

        exc = HomeAssistantCommandError(message)
        result = exception_to_structured_error(exc, raise_error=False)
        assert result["error"]["code"] == "VALIDATION_FAILED", (
            f"marker {marker_id!r} did not route to VALIDATION_FAILED"
        )

    def test_schema_phrase_without_command_prefix_not_validation(self):
        """Negative test for the "command failed:" outer gate.

        A plain Exception containing a schema phrase but without the
        "Command failed:" prefix must not route to VALIDATION_FAILED.
        Drop the gate and this test catches it.
        """
        exc = Exception("Missing option 'foo' in bar")
        result = exception_to_structured_error(exc, raise_error=False)
        assert result["error"]["code"] != "VALIDATION_FAILED"

    # --- Auth branch: all 4 phrases + 401 numeric signal ---

    AUTH_PHRASE_MESSAGES: tuple[tuple[str, str], ...] = (
        ("unauthorized", "unauthorized: invalid bearer token"),
        ("authentication", "authentication required"),
        ("invalid_token", "token rejected: invalid token format"),
        ("access_denied", "access denied for user"),
    )

    @pytest.mark.parametrize(
        "phrase_id,message",
        AUTH_PHRASE_MESSAGES,
        ids=[m[0] for m in AUTH_PHRASE_MESSAGES],
    )
    def test_auth_phrase_classified(self, phrase_id, message):
        """Each auth phrase routes to AUTH_INVALID_TOKEN.

        Mutation-testing coverage: drop any phrase from the source tuple
        in helpers.py and the corresponding case fails.
        """
        exc = Exception(message)
        result = exception_to_structured_error(exc, raise_error=False)
        assert result["error"]["code"] == "AUTH_INVALID_TOKEN", (
            f"phrase {phrase_id!r} did not route to AUTH_INVALID_TOKEN"
        )

    def test_401_status_still_classified_as_auth(self):
        """401 numeric signal in error text remains an auth error."""
        exc = Exception("Server returned 401")
        result = exception_to_structured_error(exc, raise_error=False)
        assert result["error"]["code"] == "AUTH_INVALID_TOKEN"

    # --- Regression tests: substrings that must NOT trigger auth ---

    def test_authorized_keys_substring_not_auth_error(self):
        """Plain Exception mentioning 'authorized_keys' must not be AUTH_INVALID_TOKEN.

        Covers the root cause of #993: the old ``"auth" in error_str``
        greedy match caught this as an auth failure purely because the
        word "authorized_keys" contains "auth".
        """
        exc = Exception("Command failed: Missing option 'authorized_keys' in ssh")
        result = exception_to_structured_error(exc, raise_error=False)
        assert result["error"]["code"] != "AUTH_INVALID_TOKEN"

    # --- Command-failed fallback: known failure, not INTERNAL_ERROR ---

    def test_command_error_unknown_message_is_service_call_failed(self):
        """HomeAssistantCommandError without a specific marker => SERVICE_CALL_FAILED.

        A WS ``success=False`` is a known failure mode, not "unexpected".
        Classification falls through to the terminal ``command failed:``
        branch rather than INTERNAL_ERROR.
        """
        from ha_mcp.client.rest_client import HomeAssistantCommandError

        exc = HomeAssistantCommandError("Command failed: light unreachable")
        result = exception_to_structured_error(exc, raise_error=False)
        assert result["error"]["code"] == "SERVICE_CALL_FAILED"

    # --- Typed exceptions: type dispatch skips string classification ---

    def test_auth_required_handshake_is_connection_error(self):
        """Handshake failure carrying 'auth_required' classifies as CONNECTION_FAILED.

        websocket_client.py raises ``HomeAssistantConnectionError("Did not
        receive auth_required message")`` during the connect handshake —
        this is a transport problem, not an auth failure. Type-dispatch in
        ``_classify_exception`` routes it to CONNECTION_FAILED directly,
        skipping string classification.
        """
        from ha_mcp.client.rest_client import HomeAssistantConnectionError

        exc = HomeAssistantConnectionError("Did not receive auth_required message")
        result = exception_to_structured_error(exc, raise_error=False)
        assert result["error"]["code"] == "CONNECTION_FAILED"

    def test_typed_auth_error_classified_as_auth(self):
        """HomeAssistantAuthError routes to AUTH_INVALID_TOKEN via type dispatch.

        Covers __main__.py raise sites (OAuth token missing, HA credentials
        missing in claims). Message text doesn't match the auth phrase
        list, but the type wins before string classification runs.
        """
        from ha_mcp.client.rest_client import HomeAssistantAuthError

        exc = HomeAssistantAuthError("No OAuth token in request context")
        result = exception_to_structured_error(exc, raise_error=False)
        assert result["error"]["code"] == "AUTH_INVALID_TOKEN"

    def test_typed_connection_error_classified_as_connection(self):
        """HomeAssistantConnectionError routes to CONNECTION_FAILED via type dispatch.

        Covers websocket_client.py raise sites (WebSocket state guards).
        Message "WebSocket not authenticated" does not match any phrase in
        the auth list — the type dispatch determines the classification.
        """
        from ha_mcp.client.rest_client import HomeAssistantConnectionError

        exc = HomeAssistantConnectionError("WebSocket not authenticated")
        result = exception_to_structured_error(exc, raise_error=False)
        assert result["error"]["code"] == "CONNECTION_FAILED"
