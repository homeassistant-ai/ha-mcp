"""Unit tests for ha_call_service's ws_command escape hatch (issue #1839).

Covers the raw one-shot WebSocket-command path (``ServiceTools._call_ws_command``)
reached via ``ws_command=...`` on ``ha_call_service``, plus a couple of
service-mode regression checks to confirm the unchanged path still works when
``ws_command`` is omitted.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_service import ServiceTools


def _make_tools(send_websocket_message_return: dict | None = None) -> ServiceTools:
    client = MagicMock()
    client.send_websocket_message = AsyncMock(
        return_value=send_websocket_message_return
    )
    device_tools = MagicMock()
    return ServiceTools(client, device_tools)


class TestWsCommandSuccess:
    async def test_success_dispatches_type_plus_data_and_echoes_parameters(self):
        tools = _make_tools({"success": True, "result": None})
        data = {"domain": "sun", "issue_id": "x", "ignore": True}

        result = await tools.ha_call_service(
            ws_command="repairs/ignore_issue", data=data
        )

        tools._client.send_websocket_message.assert_awaited_once_with(
            {
                "type": "repairs/ignore_issue",
                "domain": "sun",
                "issue_id": "x",
                "ignore": True,
            }
        )
        assert result["success"] is True
        assert result["ws_command"] == "repairs/ignore_issue"
        assert result["parameters"] == data
        assert result["result"] is None
        assert "repairs/ignore_issue" in result["message"]

    async def test_success_with_no_data_sends_type_only_and_treats_data_as_empty(self):
        tools = _make_tools({"success": True, "result": {"issues": []}})

        result = await tools.ha_call_service(ws_command="repairs/list_issues")

        tools._client.send_websocket_message.assert_awaited_once_with(
            {"type": "repairs/list_issues"}
        )
        assert result["success"] is True
        assert result["ws_command"] == "repairs/list_issues"
        # No data was passed in -> parameters echoes back None, not {}.
        assert result["parameters"] is None
        assert result["result"] == {"issues": []}

    async def test_empty_dict_data_is_equivalent_to_no_data(self):
        """data={} must behave exactly like data omitted: no envelope
        pollution and parameters echoed back as None, not {}."""
        tools = _make_tools({"success": True, "result": {"issues": []}})

        result = await tools.ha_call_service(ws_command="repairs/list_issues", data={})

        tools._client.send_websocket_message.assert_awaited_once_with(
            {"type": "repairs/list_issues"}
        )
        assert result["success"] is True
        assert result["parameters"] is None

    async def test_whitespace_stripped_ws_command_succeeds(self):
        """Leading/trailing whitespace around ws_command is stripped before
        both validation and the dispatched envelope's ``type``."""
        tools = _make_tools({"success": True, "result": None})

        result = await tools.ha_call_service(ws_command="  repairs/list_issues  ")

        tools._client.send_websocket_message.assert_awaited_once_with(
            {"type": "repairs/list_issues"}
        )
        assert result["success"] is True
        assert result["ws_command"] == "repairs/list_issues"


class TestWsCommandValidation:
    async def test_ws_command_with_domain_raises_not_both(self):
        tools = _make_tools({"success": True, "result": None})

        with pytest.raises(ToolError) as excinfo:
            await tools.ha_call_service(
                ws_command="repairs/ignore_issue", domain="light"
            )

        assert "not both" in str(excinfo.value)
        tools._client.send_websocket_message.assert_not_awaited()

    async def test_ws_command_with_service_raises_not_both(self):
        tools = _make_tools({"success": True, "result": None})

        with pytest.raises(ToolError) as excinfo:
            await tools.ha_call_service(
                ws_command="repairs/ignore_issue", service="turn_on"
            )

        assert "not both" in str(excinfo.value)
        tools._client.send_websocket_message.assert_not_awaited()

    async def test_empty_ws_command_raises_non_empty(self):
        tools = _make_tools({"success": True, "result": None})

        with pytest.raises(ToolError) as excinfo:
            await tools.ha_call_service(ws_command="  ")

        assert "non-empty" in str(excinfo.value)
        tools._client.send_websocket_message.assert_not_awaited()

    async def test_subscribe_events_raises_streaming_error(self):
        tools = _make_tools({"success": True, "result": None})

        with pytest.raises(ToolError) as excinfo:
            await tools.ha_call_service(ws_command="subscribe_events")

        message = str(excinfo.value)
        assert "streaming or two-phase" in message
        assert "ha_eval_template" in message
        tools._client.send_websocket_message.assert_not_awaited()

    async def test_render_template_raises_streaming_error(self):
        tools = _make_tools({"success": True, "result": None})

        with pytest.raises(ToolError) as excinfo:
            await tools.ha_call_service(ws_command="render_template")

        message = str(excinfo.value)
        assert "streaming or two-phase" in message
        assert "ha_eval_template" in message
        tools._client.send_websocket_message.assert_not_awaited()

    async def test_ha_mcp_tools_prefix_raises_reserved_namespace_error(self):
        tools = _make_tools({"success": True, "result": None})

        with pytest.raises(ToolError) as excinfo:
            await tools.ha_call_service(ws_command="ha_mcp_tools/overview")

        assert "ha_mcp_tools" in str(excinfo.value)
        tools._client.send_websocket_message.assert_not_awaited()


class TestWsCommandExpandedStreamingBlocklist:
    """Streaming / two-phase commands beyond subscribe_events + render_template.

    ``system_health/info``, ``logbook/event_stream``, and ``assist_pipeline/run``
    are two-phase or indefinitely-streaming WS commands that don't contain the
    "subscribe" substring, so they're rejected via the explicit
    ``_WS_COMMAND_EVENT_BLOCKLIST`` set rather than the substring check. Also
    covers a case-variant of the substring-matched ``subscribe_events``.
    """

    @pytest.mark.parametrize(
        "command",
        [
            "system_health/info",
            "logbook/event_stream",
            "assist_pipeline/run",
            "SUBSCRIBE_EVENTS",
        ],
        ids=[
            "system_health_info",
            "logbook_event_stream",
            "assist_pipeline_run",
            "case_variant",
        ],
    )
    async def test_expanded_blocklist_command_raises_streaming_error(self, command):
        tools = _make_tools({"success": True, "result": None})

        with pytest.raises(ToolError) as excinfo:
            await tools.ha_call_service(ws_command=command)

        message = str(excinfo.value)
        assert "streaming or two-phase" in message
        tools._client.send_websocket_message.assert_not_awaited()


class TestWsCommandServiceInvokerReject:
    """ws_command values that would re-enter HA's service invocation.

    Routing ``call_service`` / ``execute_script`` through the escape hatch
    would bypass ha_call_service's service-mode guards (notably the reserved
    ha_mcp_tools domain block), so they're rejected outright.
    """

    @pytest.mark.parametrize(
        "command",
        ["call_service", "execute_script", "Call_Service"],
        ids=["call_service", "execute_script", "case_variant"],
    )
    async def test_service_invoker_command_rejected(self, command):
        tools = _make_tools({"success": True, "result": None})

        with pytest.raises(ToolError) as excinfo:
            await tools.ha_call_service(ws_command=command)

        message = str(excinfo.value)
        assert "invokes Home Assistant services" in message
        tools._client.send_websocket_message.assert_not_awaited()


class TestWsCommandReservedEnvelopeKeys:
    """data must not smuggle the WS envelope's own ``type``/``id`` keys.

    Allowing them would let a caller override the validated command type
    (defeating every guard above it) or collide with the transport's message
    id.
    """

    @pytest.mark.parametrize("reserved_key", ["type", "id"])
    async def test_reserved_key_in_data_rejected(self, reserved_key):
        tools = _make_tools({"success": True, "result": None})

        with pytest.raises(ToolError) as excinfo:
            await tools.ha_call_service(
                ws_command="repairs/ignore_issue",
                data={reserved_key: "subscribe_events"},
            )

        message = str(excinfo.value)
        assert "reserved WebSocket envelope key" in message
        assert json.loads(message)["parameter"] == "data"
        tools._client.send_websocket_message.assert_not_awaited()


class TestWsCommandIncompatibleServiceParams:
    """Service-call-only params must be omitted when ws_command is set."""

    @pytest.mark.parametrize(
        "kwargs, offender",
        [
            ({"entity_id": "light.x"}, "entity_id"),
            ({"return_response": True}, "return_response"),
            ({"verbose": True}, "verbose"),
            ({"result_fields": ["a"]}, "result_fields"),
            ({"result_attribute_keys": ["b"]}, "result_attribute_keys"),
        ],
        ids=[
            "entity_id",
            "return_response",
            "verbose",
            "result_fields",
            "result_attribute_keys",
        ],
    )
    async def test_incompatible_param_rejected(self, kwargs, offender):
        tools = _make_tools({"success": True, "result": None})

        with pytest.raises(ToolError) as excinfo:
            await tools.ha_call_service(ws_command="repairs/ignore_issue", **kwargs)

        message = str(excinfo.value)
        assert "apply only to service calls" in message
        assert offender in message
        tools._client.send_websocket_message.assert_not_awaited()


class TestWsCommandNonDictData:
    async def test_list_data_rejected_as_non_object(self):
        tools = _make_tools({"success": True, "result": None})

        with pytest.raises(ToolError) as excinfo:
            await tools.ha_call_service(
                ws_command="repairs/ignore_issue", data=[1, 2, 3]
            )

        assert "ws_command data must be a JSON object" in str(excinfo.value)
        tools._client.send_websocket_message.assert_not_awaited()


class TestWsCommandHaMcpToolsPrefixVariants:
    """Case and whitespace variants of the reserved ha_mcp_tools/* prefix.

    Mirrors the sibling domain-refusal coverage in
    tests/src/e2e/workflows/services/test_ha_mcp_tools_refusal.py for the
    domain/service path.
    """

    @pytest.mark.parametrize(
        "variant",
        [
            "HA_MCP_TOOLS/overview",
            "Ha_Mcp_Tools/overview",
            "  ha_mcp_tools/overview  ",
        ],
        ids=["upper", "mixed", "padded_lower"],
    )
    async def test_prefix_case_and_whitespace_variants_rejected(self, variant):
        tools = _make_tools({"success": True, "result": None})

        with pytest.raises(ToolError) as excinfo:
            await tools.ha_call_service(ws_command=variant)

        assert "ha_mcp_tools" in str(excinfo.value)
        tools._client.send_websocket_message.assert_not_awaited()


class TestWsCommandBackendFailure:
    async def test_backend_failure_raises_service_call_failed_with_error_text(self):
        tools = _make_tools({"success": False, "error": "boom"})

        with pytest.raises(ToolError) as excinfo:
            await tools.ha_call_service(
                ws_command="repairs/ignore_issue",
                data={"domain": "sun", "issue_id": "x", "ignore": True},
            )

        assert "boom" in str(excinfo.value)

    async def test_none_backend_return_raises_websocket_command_failed(self):
        """send_websocket_message never raises (it always returns a dict),
        but defend the non-dict-return shape anyway: None must still surface
        as a structured failure, not an AttributeError from a bare .get()."""
        tools = _make_tools(None)

        with pytest.raises(ToolError) as excinfo:
            await tools.ha_call_service(ws_command="repairs/list_issues")

        assert "WebSocket command failed" in str(excinfo.value)

    async def test_success_false_without_error_key_raises_websocket_command_failed(
        self,
    ):
        """success: False with no 'error' key must still fail with a
        readable message, not surface None or crash formatting str(None)."""
        tools = _make_tools({"success": False})

        with pytest.raises(ToolError) as excinfo:
            await tools.ha_call_service(ws_command="repairs/list_issues")

        assert "WebSocket command failed" in str(excinfo.value)


class TestServiceModeRegression:
    async def test_missing_service_raises_domain_and_service_required(self):
        tools = _make_tools()

        with pytest.raises(ToolError) as excinfo:
            await tools.ha_call_service(domain="light")

        assert "domain and service are required" in str(excinfo.value)
        tools._client.send_websocket_message.assert_not_awaited()

    async def test_no_params_set_raises_domain_and_service_required(self):
        tools = _make_tools()

        with pytest.raises(ToolError) as excinfo:
            await tools.ha_call_service()

        assert "domain and service are required" in str(excinfo.value)
        tools._client.send_websocket_message.assert_not_awaited()

    async def test_service_only_raises_domain_and_service_required(self):
        tools = _make_tools()

        with pytest.raises(ToolError) as excinfo:
            await tools.ha_call_service(service="turn_on")

        assert "domain and service are required" in str(excinfo.value)
        tools._client.send_websocket_message.assert_not_awaited()

    async def test_happy_service_call_still_works_without_ws_command(self):
        client = MagicMock()
        client.call_service = AsyncMock(return_value=[])
        client.send_websocket_message = AsyncMock()
        device_tools = MagicMock()
        tools = ServiceTools(client, device_tools)

        result = await tools.ha_call_service(domain="light", service="turn_on")

        assert result["success"] is True
        assert result["domain"] == "light"
        assert result["service"] == "turn_on"
        client.call_service.assert_awaited_once()
        client.send_websocket_message.assert_not_awaited()
