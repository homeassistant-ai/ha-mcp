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
