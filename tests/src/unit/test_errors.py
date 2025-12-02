"""Unit tests for the structured error handling module.

These tests verify that error codes, error responses, and helper functions
work correctly to provide informative, structured error messages.
"""

import pytest

from ha_mcp.errors import (
    DEFAULT_SUGGESTIONS,
    ErrorCode,
    create_auth_error,
    create_config_error,
    create_connection_error,
    create_entity_not_found_error,
    create_error_response,
    create_resource_not_found_error,
    create_service_error,
    create_timeout_error,
    create_validation_error,
    get_error_code,
    get_error_message,
    is_error_response,
)


class TestErrorCode:
    """Tests for ErrorCode enum."""

    def test_error_codes_are_strings(self):
        """Error codes should be string values."""
        assert ErrorCode.CONNECTION_FAILED.value == "CONNECTION_FAILED"
        assert ErrorCode.ENTITY_NOT_FOUND.value == "ENTITY_NOT_FOUND"
        assert ErrorCode.AUTH_INVALID_TOKEN.value == "AUTH_INVALID_TOKEN"

    def test_error_codes_can_be_compared_to_strings(self):
        """Error codes should be comparable to their string values."""
        assert ErrorCode.CONNECTION_FAILED == "CONNECTION_FAILED"
        assert ErrorCode.ENTITY_NOT_FOUND == "ENTITY_NOT_FOUND"

    def test_all_error_codes_have_suggestions(self):
        """All error codes should have default suggestions defined."""
        # Check that important error codes have suggestions
        important_codes = [
            ErrorCode.CONNECTION_FAILED,
            ErrorCode.CONNECTION_TIMEOUT,
            ErrorCode.AUTH_INVALID_TOKEN,
            ErrorCode.ENTITY_NOT_FOUND,
            ErrorCode.SERVICE_NOT_FOUND,
            ErrorCode.VALIDATION_INVALID_JSON,
        ]
        for code in important_codes:
            assert code in DEFAULT_SUGGESTIONS, f"Missing suggestions for {code}"
            assert len(DEFAULT_SUGGESTIONS[code]) > 0, f"Empty suggestions for {code}"


class TestCreateErrorResponse:
    """Tests for create_error_response function."""

    def test_basic_error_response_structure(self):
        """Error response should have correct structure."""
        response = create_error_response(
            ErrorCode.ENTITY_NOT_FOUND,
            "Entity not found",
        )

        assert response["success"] is False
        assert "error" in response
        assert response["error"]["code"] == "ENTITY_NOT_FOUND"
        assert response["error"]["message"] == "Entity not found"

    def test_error_response_with_details(self):
        """Error response should include details when provided."""
        response = create_error_response(
            ErrorCode.CONNECTION_FAILED,
            "Connection failed",
            details="Network unreachable",
        )

        assert response["error"]["details"] == "Network unreachable"

    def test_error_response_with_custom_suggestions(self):
        """Error response should use custom suggestions when provided."""
        custom_suggestions = ["Try this", "Or that"]
        response = create_error_response(
            ErrorCode.INTERNAL_ERROR,
            "Internal error",
            suggestions=custom_suggestions,
        )

        assert response["error"]["suggestion"] == "Try this"
        assert response["error"]["suggestions"] == custom_suggestions

    def test_error_response_with_default_suggestions(self):
        """Error response should use default suggestions when not provided."""
        response = create_error_response(
            ErrorCode.ENTITY_NOT_FOUND,
            "Entity not found",
        )

        assert "suggestion" in response["error"]
        # Should use default suggestions from DEFAULT_SUGGESTIONS
        assert "ha_search_entities()" in response["error"]["suggestion"]

    def test_error_response_with_context(self):
        """Error response should include context at top level."""
        response = create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            "Service call failed",
            context={"domain": "light", "service": "turn_on", "entity_id": "light.test"},
        )

        assert response["domain"] == "light"
        assert response["service"] == "turn_on"
        assert response["entity_id"] == "light.test"


class TestCreateConnectionError:
    """Tests for create_connection_error function."""

    def test_connection_failed_error(self):
        """Connection error should have CONNECTION_FAILED code."""
        response = create_connection_error("Failed to connect")

        assert response["success"] is False
        assert response["error"]["code"] == "CONNECTION_FAILED"
        assert "Failed to connect" in response["error"]["message"]

    def test_connection_timeout_error(self):
        """Timeout connection error should have CONNECTION_TIMEOUT code."""
        response = create_connection_error("Request timed out", timeout=True)

        assert response["error"]["code"] == "CONNECTION_TIMEOUT"


class TestCreateAuthError:
    """Tests for create_auth_error function."""

    def test_invalid_token_error(self):
        """Auth error should have AUTH_INVALID_TOKEN code."""
        response = create_auth_error("Invalid token")

        assert response["success"] is False
        assert response["error"]["code"] == "AUTH_INVALID_TOKEN"

    def test_expired_token_error(self):
        """Expired auth error should have AUTH_EXPIRED code."""
        response = create_auth_error("Token expired", expired=True)

        assert response["error"]["code"] == "AUTH_EXPIRED"


class TestCreateEntityNotFoundError:
    """Tests for create_entity_not_found_error function."""

    def test_entity_not_found_error_structure(self):
        """Entity not found error should include entity_id in context."""
        response = create_entity_not_found_error("light.nonexistent")

        assert response["success"] is False
        assert response["error"]["code"] == "ENTITY_NOT_FOUND"
        assert "light.nonexistent" in response["error"]["message"]
        assert response["entity_id"] == "light.nonexistent"

    def test_entity_not_found_with_details(self):
        """Entity not found error should include custom details."""
        response = create_entity_not_found_error(
            "sensor.test",
            details="Sensor was deleted",
        )

        assert response["error"]["details"] == "Sensor was deleted"


class TestCreateServiceError:
    """Tests for create_service_error function."""

    def test_service_error_structure(self):
        """Service error should include domain, service, and entity_id."""
        response = create_service_error(
            domain="light",
            service="turn_on",
            message="Service failed",
            entity_id="light.test",
        )

        assert response["success"] is False
        assert response["error"]["code"] == "SERVICE_CALL_FAILED"
        assert response["domain"] == "light"
        assert response["service"] == "turn_on"
        assert response["entity_id"] == "light.test"


class TestCreateValidationError:
    """Tests for create_validation_error function."""

    def test_validation_error_structure(self):
        """Validation error should have VALIDATION_FAILED code."""
        response = create_validation_error("Invalid input")

        assert response["success"] is False
        assert response["error"]["code"] == "VALIDATION_FAILED"

    def test_invalid_json_error(self):
        """Invalid JSON error should have VALIDATION_INVALID_JSON code."""
        response = create_validation_error(
            "Invalid JSON syntax",
            parameter="data",
            invalid_json=True,
        )

        assert response["error"]["code"] == "VALIDATION_INVALID_JSON"
        assert response["parameter"] == "data"


class TestCreateConfigError:
    """Tests for create_config_error function."""

    def test_config_error_structure(self):
        """Config error should have CONFIG_INVALID code."""
        response = create_config_error("Invalid configuration")

        assert response["success"] is False
        assert response["error"]["code"] == "CONFIG_INVALID"

    def test_missing_fields_error(self):
        """Missing fields error should have CONFIG_MISSING_REQUIRED_FIELDS code."""
        response = create_config_error(
            "Missing required fields",
            missing_fields=["alias", "trigger"],
        )

        assert response["error"]["code"] == "CONFIG_MISSING_REQUIRED_FIELDS"
        assert response["missing_fields"] == ["alias", "trigger"]
        assert "alias, trigger" in response["error"]["details"]


class TestCreateTimeoutError:
    """Tests for create_timeout_error function."""

    def test_timeout_error_structure(self):
        """Timeout error should include operation and timeout in context."""
        response = create_timeout_error("service_call", 30.0)

        assert response["success"] is False
        assert response["error"]["code"] == "TIMEOUT_OPERATION"
        assert "service_call" in response["error"]["message"]
        assert "30" in response["error"]["message"]
        assert response["operation"] == "service_call"
        assert response["timeout_seconds"] == 30.0


class TestCreateResourceNotFoundError:
    """Tests for create_resource_not_found_error function."""

    def test_resource_not_found_structure(self):
        """Resource not found error should include resource_type and identifier."""
        response = create_resource_not_found_error("Automation", "automation.test")

        assert response["success"] is False
        assert response["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert "Automation" in response["error"]["message"]
        assert "automation.test" in response["error"]["message"]
        assert response["resource_type"] == "Automation"
        assert response["identifier"] == "automation.test"


class TestIsErrorResponse:
    """Tests for is_error_response function."""

    def test_error_response_detected(self):
        """Error responses should be detected."""
        error_response = create_error_response(ErrorCode.INTERNAL_ERROR, "Error")
        assert is_error_response(error_response) is True

    def test_success_response_not_error(self):
        """Success responses should not be detected as errors."""
        success_response = {"success": True, "result": "data"}
        assert is_error_response(success_response) is False

    def test_response_without_error_key_not_error(self):
        """Responses without error key should not be detected as errors."""
        response = {"success": False, "message": "Something went wrong"}
        assert is_error_response(response) is False


class TestGetErrorCode:
    """Tests for get_error_code function."""

    def test_get_error_code_from_error_response(self):
        """Error code should be extracted from error response."""
        response = create_error_response(ErrorCode.ENTITY_NOT_FOUND, "Not found")
        assert get_error_code(response) == "ENTITY_NOT_FOUND"

    def test_get_error_code_from_success_response(self):
        """None should be returned for success responses."""
        response = {"success": True, "result": "data"}
        assert get_error_code(response) is None


class TestGetErrorMessage:
    """Tests for get_error_message function."""

    def test_get_error_message_from_error_response(self):
        """Error message should be extracted from error response."""
        response = create_error_response(ErrorCode.INTERNAL_ERROR, "Test message")
        assert get_error_message(response) == "Test message"

    def test_get_error_message_from_success_response(self):
        """None should be returned for success responses."""
        response = {"success": True, "result": "data"}
        assert get_error_message(response) is None

    def test_get_error_message_from_legacy_string_error(self):
        """Error message should be extracted from legacy string error format."""
        response = {"success": False, "error": "Legacy error message"}
        assert get_error_message(response) == "Legacy error message"
