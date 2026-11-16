"""ha_call_service's ws_command escape hatch (issue #1839), on real Core.

The server's client is connected to the real ``hass``: an accepted command is
answered by Core's own handler, and ``sent`` (a call-through spy on the
client's WebSocket send) proves a refused command never left the server.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_service import ServiceTools
from ha_mcp.tools.util_helpers import BLOCKED_WS_WRITE_COMMANDS, raw_ws_refusal

from ._tool_errors import error_body, error_of


@pytest.fixture
def tools(core_client: HomeAssistantClient) -> ServiceTools:
    return ServiceTools(core_client, None)


@pytest.fixture
async def issues(hass: HomeAssistant) -> ir.IssueRegistry:
    """Core's repairs commands, with one active issue raised by ``sun``."""
    assert await async_setup_component(hass, "repairs", {})
    ir.async_create_issue(
        hass,
        "sun",
        "stale",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="stale",
    )
    return ir.async_get(hass)


async def refusal(tools: ServiceTools, **kwargs: Any) -> dict[str, Any]:
    """The error of an ``ha_call_service`` call that must be refused."""
    with pytest.raises(ToolError) as excinfo:
        await tools.ha_call_service(**kwargs)
    return error_body(excinfo)


class TestWsCommandReachesCore:
    async def test_command_data_reaches_core_and_is_echoed(
        self, tools: ServiceTools, issues: ir.IssueRegistry
    ) -> None:
        data = {"domain": "sun", "issue_id": "stale", "ignore": True}

        result = await tools.ha_call_service(
            ws_command="repairs/ignore_issue", data=data
        )

        assert result["success"] is True
        assert result["ws_command"] == "repairs/ignore_issue"
        assert result["parameters"] == data
        assert result["result"] is None
        assert "repairs/ignore_issue" in result["message"]
        issue = issues.async_get_issue("sun", "stale")
        assert issue is not None
        assert issue.dismissed_version is not None

    @pytest.mark.parametrize("data", [None, {}], ids=["omitted", "empty"])
    async def test_command_without_data_sends_type_only_and_returns_cores_answer(
        self,
        tools: ServiceTools,
        issues: ir.IssueRegistry,
        sent: AsyncMock,
        data: dict[str, Any] | None,
    ) -> None:
        result = await tools.ha_call_service(
            ws_command="repairs/list_issues", data=data
        )

        sent.assert_awaited_once_with({"type": "repairs/list_issues"})
        assert result["parameters"] is None
        assert [issue["issue_id"] for issue in result["result"]["issues"]] == ["stale"]

    async def test_padded_command_type_is_trimmed_before_sending(
        self, tools: ServiceTools, issues: ir.IssueRegistry
    ) -> None:
        """Core knows no command with spaces in its type: untrimmed, it fails."""
        result = await tools.ha_call_service(ws_command="  repairs/list_issues  ")

        assert result["success"] is True
        assert result["ws_command"] == "repairs/list_issues"

    async def test_core_error_reply_fails_the_call_with_cores_reason(
        self, tools: ServiceTools, issues: ir.IssueRegistry
    ) -> None:
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_call_service(
                ws_command="repairs/ignore_issue",
                data={"domain": "sun", "issue_id": "no_such_issue", "ignore": True},
            )

        error = error_of(excinfo)
        assert error["code"] == "SERVICE_CALL_FAILED"
        assert "no_such_issue" in error["message"]


class TestWsCommandRefusedBeforeSending:
    @pytest.mark.parametrize(
        "service_params",
        [{"domain": "light"}, {"service": "turn_on"}],
        ids=["domain", "service"],
    )
    async def test_ws_command_with_service_params_is_refused(
        self, tools: ServiceTools, sent: AsyncMock, service_params: dict[str, str]
    ) -> None:
        error = await refusal(
            tools, ws_command="repairs/ignore_issue", **service_params
        )

        assert "not both" in error["error"]["message"]
        sent.assert_not_awaited()

    async def test_blank_ws_command_is_refused(
        self, tools: ServiceTools, sent: AsyncMock
    ) -> None:
        error = await refusal(tools, ws_command="  ")

        assert "non-empty" in error["error"]["message"]
        sent.assert_not_awaited()

    @pytest.mark.parametrize(
        "command",
        [
            "render_template",
            "system_health/info",
            "assist_pipeline/run",
            "SUBSCRIBE_EVENTS",
            "history/stream",
            "template/start_preview",
        ],
    )
    async def test_streaming_command_is_refused(
        self, tools: ServiceTools, sent: AsyncMock, command: str
    ) -> None:
        """A streaming command would leave its subscription open on the socket."""
        error = await refusal(tools, ws_command=command)

        message = error["error"]["message"]
        assert "streaming or two-phase" in message
        assert "ha_eval_template" in message
        sent.assert_not_awaited()

    @pytest.mark.parametrize(
        "command", ["call_service", "execute_script", "Call_Service"]
    )
    async def test_service_invoking_command_is_refused(
        self, tools: ServiceTools, sent: AsyncMock, command: str
    ) -> None:
        """It would skip the service-mode guards, such as the ha_mcp_tools domain block."""
        error = await refusal(tools, ws_command=command)

        assert "invokes Home Assistant services" in error["error"]["message"]
        sent.assert_not_awaited()

    @pytest.mark.parametrize(
        "command",
        [
            "ha_mcp_tools/overview",
            "HA_MCP_TOOLS/overview",
            "  ha_mcp_tools/overview  ",
        ],
    )
    async def test_component_command_is_refused(
        self, tools: ServiceTools, sent: AsyncMock, command: str
    ) -> None:
        error = await refusal(tools, ws_command=command)

        assert "ha_mcp_tools" in error["error"]["message"]
        sent.assert_not_awaited()

    @pytest.mark.parametrize(
        "command", ["lovelace/config/save", "LOVELACE/CONFIG/SAVE"]
    )
    async def test_write_a_dedicated_tool_guards_is_refused(
        self, tools: ServiceTools, sent: AsyncMock, command: str
    ) -> None:
        """The dedicated tool's backups and conflict checks would be skipped."""
        error = await refusal(tools, ws_command=command)

        assert (
            "dedicated tool guards with backups and conflict checks"
            in error["error"]["message"]
        )
        sent.assert_not_awaited()

    @pytest.mark.parametrize("reserved_key", ["type", "id"])
    async def test_envelope_key_in_data_is_refused(
        self, tools: ServiceTools, sent: AsyncMock, reserved_key: str
    ) -> None:
        """``type`` would replace the checked command, ``id`` the transport's id."""
        error = await refusal(
            tools,
            ws_command="repairs/ignore_issue",
            data={reserved_key: "subscribe_events"},
        )

        assert "reserved WebSocket envelope key" in error["error"]["message"]
        assert error["parameter"] == "data"
        sent.assert_not_awaited()

    @pytest.mark.parametrize(
        "service_only_param",
        [
            {"entity_id": "light.x"},
            {"return_response": True},
            {"verbose": True},
            {"result_fields": ["a"]},
            {"result_attribute_keys": ["b"]},
        ],
        ids=lambda param: next(iter(param)),
    )
    async def test_service_only_param_with_ws_command_is_refused(
        self,
        tools: ServiceTools,
        sent: AsyncMock,
        service_only_param: dict[str, Any],
    ) -> None:
        error = await refusal(
            tools, ws_command="repairs/ignore_issue", **service_only_param
        )

        message = error["error"]["message"]
        assert "apply only to service calls" in message
        assert next(iter(service_only_param)) in message
        sent.assert_not_awaited()

    async def test_non_object_data_is_refused(
        self, tools: ServiceTools, sent: AsyncMock
    ) -> None:
        error = await refusal(tools, ws_command="repairs/ignore_issue", data=[1, 2, 3])

        assert "ws_command data must be a JSON object" in error["error"]["message"]
        sent.assert_not_awaited()


@pytest.mark.parametrize("command", sorted(BLOCKED_WS_WRITE_COMMANDS))
def test_every_blocklisted_command_is_refused(command: str) -> None:
    """Every entry of BLOCKED_WS_WRITE_COMMANDS is refused by raw_ws_refusal,
    which ha_call_service's ws_command and Code Mode's ws_send both ask; the
    test above checks the ws_command wiring, and the E2E suite checks ws_send's
    with config/core/update and lovelace/config/save."""
    assert raw_ws_refusal(command) is not None


class TestServiceMode:
    @pytest.mark.parametrize(
        "service_params",
        [{"domain": "light"}, {}, {"service": "turn_on"}],
        ids=["no_service", "nothing", "no_domain"],
    )
    async def test_service_call_without_domain_and_service_is_refused(
        self, tools: ServiceTools, service_params: dict[str, str]
    ) -> None:
        error = await refusal(tools, **service_params)

        assert "domain and service are required" in error["error"]["message"]

    async def test_service_call_without_ws_command_runs_the_service(
        self, hass: HomeAssistant, tools: ServiceTools, sent: AsyncMock
    ) -> None:
        assert await async_setup_component(
            hass, "input_boolean", {"input_boolean": {"porch": {}}}
        )

        result = await tools.ha_call_service(
            domain="input_boolean",
            service="turn_on",
            entity_id="input_boolean.porch",
            wait=False,
        )

        assert result["success"] is True
        assert result["domain"] == "input_boolean"
        assert result["service"] == "turn_on"
        state = hass.states.get("input_boolean.porch")
        assert state is not None
        assert state.state == "on"
        sent.assert_not_awaited()
