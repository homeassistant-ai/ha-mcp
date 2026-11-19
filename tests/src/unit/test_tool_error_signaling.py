"""Tool errors reach the MCP client as errors, with a structured body (#518).

A tool reports a failure by raising ``ToolError`` that carries the structured
error as JSON, which the MCP reply flags with ``isError``.
``exception_to_structured_error`` maps what the server's client raised to the
code and guidance the caller sees. Where an error comes from Home Assistant,
the server's own client gets it from a real ``hass``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import pytest
import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import (
    MockConfigEntry,
    MockModule,
    mock_config_flow,
    mock_integration,
    mock_platform,
)

from ha_mcp._vendor.fastmcp import Client
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantAdminRequiredError,
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantClient,
    HomeAssistantCommandError,
    HomeAssistantConnectionError,
)
from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.tools.component_registry_lookup import WS_REGISTRY_LOOKUP
from ha_mcp.tools.helpers import exception_to_structured_error, raise_tool_error
from ha_mcp.tools.tools_integrations import IntegrationTools

from ._mock_flow_integration import open_flow_ids
from ._real_server import build_server
from ._tool_errors import rejected_call_body

RECONFIGURABLE = "reconfigurable_hub"


class TestRaiseToolError:
    """raise_tool_error carries the whole structured error to the caller."""

    def test_tool_error_contains_structured_json(self) -> None:
        error_response = create_error_response(
            ErrorCode.ENTITY_NOT_FOUND,
            "Entity light.test not found",
            suggestions=["Use ha_search() to find valid entity IDs"],
        )

        with pytest.raises(ToolError) as exc_info:
            raise_tool_error(error_response)

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert error_data["error"]["code"] == "ENTITY_NOT_FOUND"
        assert error_data["error"]["message"] == "Entity light.test not found"
        assert "suggestion" in error_data["error"]

    def test_preserves_all_error_fields(self) -> None:
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
    """What a caller's context and hints add to the structured error."""

    def test_context_is_preserved(self) -> None:
        result = exception_to_structured_error(
            HomeAssistantAPIError("Bad request", status_code=400),
            context={"entity_id": "light.test", "action": "get"},
            raise_error=False,
        )

        assert result.get("entity_id") == "light.test"
        assert result.get("action") == "get"

    def test_suggestions_embedded_when_raising(self) -> None:
        suggestions = ["Check connection", "Retry later"]
        with pytest.raises(ToolError) as exc_info:
            exception_to_structured_error(
                ValueError("test error"),
                suggestions=suggestions,
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert error_data["error"]["suggestions"] == suggestions

    def test_suggestions_embedded_when_returning(self) -> None:
        suggestions = ["Try a different query", "Check spelling"]
        result = exception_to_structured_error(
            ValueError("test error"),
            raise_error=False,
            suggestions=suggestions,
        )

        assert result["error"]["suggestions"] == suggestions

    def test_no_suggestions_when_none(self) -> None:
        """No hint at all only when neither the caller nor the code has one."""
        result = exception_to_structured_error(
            HomeAssistantAPIError("Resource not found", status_code=404),
            raise_error=False,
        )

        assert result["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert "suggestions" not in result["error"]
        assert "suggestion" not in result["error"]


class TestInternalErrorTracebackLogging:
    """Issue #1302: an unclassified exception logs its traceback once, here."""

    def test_internal_error_path_logs_traceback(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.ERROR, logger="ha_mcp.tools.helpers"):
            result = exception_to_structured_error(
                Exception("weird thing in card traversal"),
                raise_error=False,
            )

        assert result["error"]["code"] == ErrorCode.INTERNAL_ERROR
        traceback_records = [r for r in caplog.records if r.exc_info is not None]
        assert traceback_records, (
            f"Expected an exc_info=True log record, got: "
            f"{[(r.levelname, r.message) for r in caplog.records]}"
        )

    def test_classified_error_does_not_log_traceback(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.ERROR, logger="ha_mcp.tools.helpers"):
            result = exception_to_structured_error(
                ValueError("Invalid parameter"),
                raise_error=False,
            )

        assert result["error"]["code"] == ErrorCode.VALIDATION_FAILED
        traceback_records = [r for r in caplog.records if r.exc_info is not None]
        assert not traceback_records, (
            f"Classified exceptions must not log a traceback (noise), got: "
            f"{[(r.levelname, r.message) for r in caplog.records]}"
        )


class TestErrorCodeMapping:
    """Exception type and message to error code."""

    def test_value_error_maps_to_validation_failed(self) -> None:
        result = exception_to_structured_error(
            ValueError("Invalid parameter"), raise_error=False
        )
        assert result["error"]["code"] == "VALIDATION_FAILED"

    def test_timeout_error_maps_to_timeout_operation(self) -> None:
        result = exception_to_structured_error(
            TimeoutError("Request timed out"), raise_error=False
        )
        assert result["error"]["code"] == "TIMEOUT_OPERATION"

    def test_connection_refusal_maps_to_connection_failed(self) -> None:
        result = exception_to_structured_error(
            ConnectionRefusedError("peer refused the socket"), raise_error=False
        )
        assert result["error"]["code"] == "CONNECTION_FAILED"


@pytest.fixture
async def mcp_client(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[Client[Any]]:
    """An MCP client of the real server, whose tools answer from Core."""
    monkeypatch.setenv("ENABLE_TOOL_SEARCH", "false")
    monkeypatch.setenv("ENABLE_TOOL_SECURITY_POLICIES", "false")
    monkeypatch.setenv("DISABLED_TOOLS", "")
    server = build_server(monkeypatch, core_client)
    async with Client(server.mcp) as client:
        yield client


class TestToolErrorsOverMCP:
    """A failed tool call is flagged as an error in the MCP reply (#518)."""

    async def test_a_failed_call_is_an_error_reply_with_the_structured_body(
        self, mcp_client: Client[Any]
    ) -> None:
        result = await mcp_client.call_tool(
            "ha_get_state", {"entity_id": "light.missing"}, raise_on_error=False
        )

        assert result.is_error
        body = json.loads(result.content[0].text)
        assert body["success"] is False
        assert body["error"]["code"] == "ENTITY_NOT_FOUND"

    async def test_a_successful_call_is_not_an_error_reply(
        self, hass: HomeAssistant, mcp_client: Client[Any]
    ) -> None:
        hass.states.async_set("light.porch", "on")

        result = await mcp_client.call_tool(
            "ha_get_state", {"entity_id": "light.porch"}, raise_on_error=False
        )

        assert not result.is_error
        assert "light.porch" in result.content[0].text


class _ReconfigurableFlow(ConfigFlow):
    VERSION = 1

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_form(
            step_id="reconfigure", data_schema=vol.Schema({vol.Required("host"): str})
        )


@contextmanager
def token_revoked_on(hass: HomeAssistant, command: str, token: str) -> Iterator[None]:
    """Core revokes ``token`` when ``command`` arrives, and closes its sockets.

    The command gets no answer; Core's own handler is back afterwards.
    """
    core_handler, schema = hass.data[websocket_api.DOMAIN][command]

    def revoke(core: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
        refresh_token = core.auth.async_validate_access_token(token)
        assert refresh_token is not None
        core.auth.async_remove_refresh_token(refresh_token)

    websocket_api.async_register_command(hass, command, revoke, schema)
    try:
        yield
    finally:
        websocket_api.async_register_command(hass, command, core_handler, schema)


class TestSchemaAndAuthClassification:
    """Precedence and type dispatch of the schema and auth branches (#993).

    The per-branch cases of the message classifier are in
    ``test_helper_classifier.py``.
    """

    def test_schema_phrase_without_command_prefix_not_validation(self) -> None:
        """A schema phrase is a validation error only after ``Command failed:``."""
        exc = Exception("Missing option 'foo' in bar")
        result = exception_to_structured_error(exc, raise_error=False)
        assert result["error"]["code"] != "VALIDATION_FAILED"

    def test_authorized_keys_substring_not_auth_error(self) -> None:
        """``authorized_keys`` contains "auth" but is no auth failure (#993)."""
        exc = Exception("Command failed: Missing option 'authorized_keys' in ssh")
        result = exception_to_structured_error(exc, raise_error=False)
        assert result["error"]["code"] != "AUTH_INVALID_TOKEN"

    def test_command_error_unknown_message_is_service_call_failed(self) -> None:
        """A rejected command is a known failure, not an internal error."""
        exc = HomeAssistantCommandError("Command failed: light unreachable")
        result = exception_to_structured_error(exc, raise_error=False)
        assert result["error"]["code"] == "SERVICE_CALL_FAILED"

    def test_auth_required_handshake_is_connection_error(self) -> None:
        """A handshake failure naming ``auth_required`` is a transport problem."""
        exc = HomeAssistantConnectionError("Did not receive auth_required message")
        result = exception_to_structured_error(exc, raise_error=False)
        assert result["error"]["code"] == "CONNECTION_FAILED"

    def test_typed_auth_error_classified_as_auth(self) -> None:
        """The type wins over a message no auth phrase matches."""
        exc = HomeAssistantAuthError("No OAuth token in request context")
        result = exception_to_structured_error(exc, raise_error=False)
        assert result["error"]["code"] == "AUTH_INVALID_TOKEN"

    @pytest.mark.parametrize(
        ("cause", "expected"),
        [
            (
                HomeAssistantAuthError("WebSocket authentication failed"),
                "AUTH_INVALID_TOKEN",
            ),
            (
                HomeAssistantAdminRequiredError("config/x is admin-only"),
                "AUTH_INSUFFICIENT_PERMISSIONS",
            ),
            (OSError("Connection refused"), "CONNECTION_FAILED"),
            (ValueError("boom"), "CONNECTION_FAILED"),
        ],
        ids=["auth-cause", "admin-cause", "transport-cause", "other-cause"],
    )
    def test_connection_error_classified_by_auth_cause(
        self, cause: Exception, expected: str
    ) -> None:
        """A connection error raised from an auth rejection is classified by
        that cause; any other cause keeps the connection classification."""
        try:
            raise HomeAssistantConnectionError(str(cause)) from cause
        except HomeAssistantConnectionError as exc:
            result = exception_to_structured_error(exc, raise_error=False)
        assert result["error"]["code"] == expected

    # Core closes a socket whose token it refused; the client's close reply
    # then re-arms aiohttp's heartbeat timer on that closed server socket.
    @pytest.mark.parametrize("expected_lingering_timers", [True])
    async def test_a_token_refused_on_connect_is_an_auth_error(
        self, core_client: HomeAssistantClient
    ) -> None:
        """Core refuses the WebSocket login; the caller is told to fix the
        token, not to check the connection."""
        refused = HomeAssistantClient(
            base_url=core_client.base_url, token="not-a-token", verify_ssl=False
        )
        try:
            with pytest.raises(HomeAssistantConnectionError) as exc_info:
                await refused.send_websocket_message({"type": "get_states"})
        finally:
            await refused.close()

        assert isinstance(exc_info.value.__cause__, HomeAssistantAuthError)
        result = exception_to_structured_error(exc_info.value, raise_error=False)
        assert result["error"]["code"] == "AUTH_INVALID_TOKEN"

    # The refused reconnect's heartbeat timer, as above.
    @pytest.mark.parametrize("expected_lingering_timers", [True])
    async def test_a_token_revoked_before_the_registry_read_is_an_auth_error(
        self, hass: HomeAssistant, component_core_client: HomeAssistantClient
    ) -> None:
        """A reconfigure whose token Core revokes after reading the entry
        fails the registry read, and the caller is told the token was
        refused, not that Home Assistant is unreachable."""
        assert await async_setup_component(hass, "config", {})
        mock_integration(hass, MockModule(RECONFIGURABLE))
        mock_platform(hass, f"{RECONFIGURABLE}.config_flow")
        entry = MockConfigEntry(domain=RECONFIGURABLE, title="Hub")
        entry.add_to_hass(hass)
        client = component_core_client

        with (
            mock_config_flow(RECONFIGURABLE, _ReconfigurableFlow),
            token_revoked_on(hass, WS_REGISTRY_LOOKUP, client.token),
            patch.object(
                client, "list_entity_registry", wraps=client.list_entity_registry
            ) as registry_read,
        ):
            body = await rejected_call_body(
                IntegrationTools(client).ha_set_integration(
                    entry_id=entry.entry_id,
                    reconfigure=True,
                    expected_entity_ids=["switch.relay"],
                )
            )

        registry_read.assert_awaited_once()
        assert body["error"]["code"] == "AUTH_INVALID_TOKEN"
        assert open_flow_ids(hass) == []


class TestClassifiedGuidanceSurvivesCallerSuggestions:
    """Issue #2698: a caller's own hints must not drop the token, admin or
    reachability guidance an environment failure needs to be fixed."""

    @staticmethod
    def _classified(error: Exception) -> list[str]:
        err = exception_to_structured_error(error, raise_error=False)["error"]
        suggestions: list[str] = err.get("suggestions") or [err["suggestion"]]
        return suggestions

    @pytest.mark.parametrize(
        "error",
        [
            HomeAssistantAuthError("401 Unauthorized"),
            HomeAssistantAdminRequiredError("GET /api/error_log is admin-only"),
            HomeAssistantConnectionError("connection refused"),
        ],
    )
    def test_environment_guidance_comes_first(
        self, monkeypatch: pytest.MonkeyPatch, error: Exception
    ) -> None:
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        classified = self._classified(error)
        result = exception_to_structured_error(
            error, raise_error=False, suggestions=["Caller hint"]
        )
        assert result["error"]["suggestions"] == [*classified, "Caller hint"]
        assert result["error"]["suggestion"] == classified[0]

    def test_add_on_auth_guidance_comes_first(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SUPERVISOR_TOKEN", "x")
        monkeypatch.delenv("HA_MCP_EMBEDDED", raising=False)
        error = HomeAssistantAuthError("401 Unauthorized")
        classified = self._classified(error)
        assert "Supervisor" in classified[0]
        result = exception_to_structured_error(
            error, raise_error=False, suggestions=["Caller hint"]
        )
        assert result["error"]["suggestions"] == [*classified, "Caller hint"]

    def test_a_caller_hint_already_classified_is_not_repeated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        error = HomeAssistantAuthError("401 Unauthorized")
        classified = self._classified(error)
        result = exception_to_structured_error(
            error, raise_error=False, suggestions=[classified[0], "Caller hint"]
        )
        assert result["error"]["suggestions"] == [*classified, "Caller hint"]

    def test_other_codes_keep_the_callers_list(self) -> None:
        result = exception_to_structured_error(
            ValueError("bad input"), raise_error=False, suggestions=["Caller hint"]
        )
        assert result["error"]["suggestions"] == ["Caller hint"]


class TestEmbeddedAuthHints:
    """A refused token on the in-process server is fixed in the HA-MCP Server
    entry; a tool's own hints must not crowd that out (#2427)."""

    TOOL_HINTS = ("Verify entity 'sun.sun' exists in Home Assistant",)

    def _suggestions(self) -> list[str]:
        response = exception_to_structured_error(
            HomeAssistantAuthError("Invalid authentication token"),
            raise_error=False,
            suggestions=list(self.TOOL_HINTS),
        )
        suggestions: list[str] = response["error"]["suggestions"]
        return suggestions

    def test_the_token_fix_comes_before_the_tool_hints(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HA_MCP_EMBEDDED", "1")
        suggestions = self._suggestions()
        assert "HA-MCP Server" in suggestions[0]
        assert suggestions[-1] == self.TOOL_HINTS[0]

    def test_other_deployments_keep_classified_and_tool_hints(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("HA_MCP_EMBEDDED", raising=False)
        suggestions = self._suggestions()
        assert suggestions[-1] == self.TOOL_HINTS[0]
        assert len(suggestions) > len(self.TOOL_HINTS)
