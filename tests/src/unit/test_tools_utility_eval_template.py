"""``ha_eval_template``'s template and condition paths (#2522).

How the tool turns Core's ``render_template`` answer into a response or a
``ToolError``, how the component's ``template_diagnose`` adds the failing
line, and the ``test_condition`` route. The server's client is connected to
the real ``hass``: Core renders every template and checks every condition, and
``component_core_client`` adds the component's own commands.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client import rest_client
from ha_mcp.client.rest_client import (
    RENDER_NO_VERDICT_WITHOUT_REPORT_ERRORS,
    HomeAssistantClient,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ha_mcp.client.websocket_client import (
    HomeAssistantWebSocketClient,
    websocket_manager,
)
from ha_mcp.tools import tools_utility
from ha_mcp.tools.component_api import (
    SUPPORTED_SCHEMA_VERSION,
    ComponentCaps,
    get_component_caps,
)
from ha_mcp.tools.tools_utility import UtilityTools

from ._tool_errors import error_body

DIAGNOSE = "ha_mcp_tools/template_diagnose"
# Runs far past any timeout; Core's sandbox caps a single range, so two nest.
RUNAWAY = (
    "{% for i in range(100000) %}{% for j in range(100000) %}{% endfor %}{% endfor %}"
)


async def _eval(client: HomeAssistantClient, **kwargs: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "template": None,
        "condition": None,
        "variables": None,
        "strict": False,
        "timeout": 3,
        "report_errors": True,
    }
    args.update(kwargs)
    return await UtilityTools(client).eval_template(**args)


async def _eval_error(client: HomeAssistantClient, **kwargs: Any) -> dict[str, Any]:
    with pytest.raises(ToolError) as exc:
        await _eval(client, **kwargs)
    return error_body(exc)


async def _socket(client: HomeAssistantClient) -> HomeAssistantWebSocketClient:
    """The pooled socket the tool sends ``test_condition`` and the diagnosis on."""
    return await websocket_manager.get_client(
        url=client.base_url, token=client.token, verify_ssl=False
    )


def _diagnose_calls(sent: AsyncMock) -> list[Any]:
    return [c for c in sent.call_args_list if c.args[0] == DIAGNOSE]


def _assert_line_lookup_failed(
    error: dict[str, Any], sent: AsyncMock, invalidated: MagicMock
) -> None:
    """Core's error stands, the missing line is explained, the caps are kept."""
    assert error["error"]["code"] == "VALIDATION_FAILED"
    assert "division by zero" in error["error"]["message"]
    assert "line" not in error
    assert error["line_unavailable"].startswith("line lookup failed")
    assert len(_diagnose_calls(sent)) == 1
    invalidated.assert_not_called()


@pytest.fixture
def invalidated(monkeypatch: pytest.MonkeyPatch) -> Iterator[MagicMock]:
    """A spy on the tool's capability invalidation; the real one still runs."""
    spy = MagicMock(wraps=tools_utility.invalidate_caps)
    monkeypatch.setattr(tools_utility, "invalidate_caps", spy)
    yield spy


@pytest.fixture
async def component_socket(
    component_core_client: HomeAssistantClient,
) -> AsyncIterator[AsyncMock]:
    """A spy on what the tool sends the component; the component still answers.

    The capability probe runs first, so the spy sees only the tool's own calls.
    """
    assert await get_component_caps(component_core_client) is not None
    socket = await _socket(component_core_client)
    with patch.object(socket, "send_command", wraps=socket.send_command) as spy:
        yield spy


class TestArguments:
    @pytest.mark.parametrize(
        "kwargs",
        [{}, {"template": "{{ 1 }}", "condition": {"condition": "state"}}],
    )
    async def test_exactly_one_of_template_or_condition(
        self, core_client: HomeAssistantClient, kwargs: dict[str, Any]
    ) -> None:
        error = await _eval_error(core_client, **kwargs)
        assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"

    async def test_variables_reach_the_render(
        self, core_client: HomeAssistantClient
    ) -> None:
        data = await _eval(core_client, template="{{ foo * 2 }}", variables={"foo": 21})
        assert data["result"] == 42

    async def test_strict_makes_an_undefined_variable_an_error(
        self, core_client: HomeAssistantClient
    ) -> None:
        """Without strict the same template renders empty with a warning."""
        error = await _eval_error(core_client, template="{{ missing }}", strict=True)
        assert error["error"]["code"] == "VALIDATION_FAILED"
        assert "'missing' is undefined" in error["error"]["message"]


class TestTemplateResults:
    async def test_warnings_are_returned_with_the_result(
        self, core_client: HomeAssistantClient
    ) -> None:
        data = await _eval(core_client, template="{{ undefined_var }}x")

        assert data["success"] is True
        assert data["template"] == "{{ undefined_var }}x"
        assert data["result"] == "x"
        assert isinstance(data["listeners"], dict)
        assert any("'undefined_var' is undefined" in w for w in data["warnings"])

    async def test_home_assistant_error_text_is_the_message(
        self, core_client: HomeAssistantClient
    ) -> None:
        """Without the component the error stands as Core reported it."""
        error = await _eval_error(
            core_client,
            template="{{ x }}{{ d.split('-') }}",
            variables={"d": {}},
        )

        assert error["error"]["code"] == "VALIDATION_FAILED"
        assert "has no attribute 'split'" in error["error"]["message"]
        assert any("'x' is undefined" in w for w in error["warnings"])
        assert "line" not in error
        assert "line_unavailable" not in error

    async def test_component_diagnosis_adds_the_failing_line(
        self, component_core_client: HomeAssistantClient, component_socket: AsyncMock
    ) -> None:
        """The line comes from the component's render with the same variables.

        Without ``v`` the template renders, so a line proves the variables
        reached the diagnosis.
        """
        template = "a\n\n{{ 1 / (v | int(1)) }}"

        error = await _eval_error(
            component_core_client, template=template, variables={"v": 0}
        )

        assert error["error"]["code"] == "VALIDATION_FAILED"
        assert "division by zero" in error["error"]["message"]
        assert error["line"] == 3
        assert error["source_line"] == "{{ 1 / (v | int(1)) }}"
        assert len(_diagnose_calls(component_socket)) == 1

    async def test_unanswered_line_lookup_keeps_home_assistant_error(
        self,
        component_core_client: HomeAssistantClient,
        component_socket: AsyncMock,
        invalidated: MagicMock,
    ) -> None:
        socket = await _socket(component_core_client)
        with patch.object(
            socket,
            "send_command",
            AsyncMock(side_effect=HomeAssistantCommandTimeout("no answer")),
        ) as sent:
            error = await _eval_error(component_core_client, template="{{ 1/0 }}")

        _assert_line_lookup_failed(error, sent, invalidated)

    async def test_dropped_line_lookup_keeps_home_assistant_error(
        self,
        component_core_client: HomeAssistantClient,
        component_socket: AsyncMock,
        invalidated: MagicMock,
    ) -> None:
        socket = await _socket(component_core_client)
        with patch.object(
            socket,
            "send_command",
            AsyncMock(side_effect=HomeAssistantConnectionError("socket closed")),
        ) as sent:
            error = await _eval_error(component_core_client, template="{{ 1/0 }}")

        _assert_line_lookup_failed(error, sent, invalidated)

    async def test_unknown_command_invalidates_the_cached_caps(
        self,
        core_client: HomeAssistantClient,
        invalidated: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cached caps name the command; Core, without the component, does not."""
        stale = ComponentCaps(
            SUPPORTED_SCHEMA_VERSION, "1.0.0", frozenset({"template_diagnose"}), {}
        )
        monkeypatch.setattr(
            tools_utility, "get_component_caps", AsyncMock(return_value=stale)
        )

        error = await _eval_error(core_client, template="{{ 1/0 }}")

        invalidated.assert_called_once_with(core_client)
        assert "division by zero" in error["error"]["message"]
        assert "line_unavailable" not in error

    async def test_no_verdict_uses_the_component_error(
        self,
        component_core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With report_errors off Core sends nothing; the component says why."""
        # Core never sends a verdict, so the client waits out the whole grace.
        monkeypatch.setattr(rest_client, "RENDER_VERDICT_GRACE_S", 0.2)
        error = await _eval_error(
            component_core_client, template="{% if %}", report_errors=False, timeout=1
        )

        assert error["error"]["code"] == "VALIDATION_FAILED"
        assert error["error"]["message"].startswith("TemplateSyntaxError")
        assert error["line"] == 1

    async def test_no_verdict_without_the_component_explains_itself(
        self, core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rest_client, "RENDER_VERDICT_GRACE_S", 0.2)
        error = await _eval_error(
            core_client, template="{% if %}", report_errors=False, timeout=1
        )

        assert error["error"]["code"] == "VALIDATION_FAILED"
        assert error["error"]["message"] == RENDER_NO_VERDICT_WITHOUT_REPORT_ERRORS

    async def test_no_verdict_that_timed_out_in_process_is_a_timeout(
        self, component_core_client: HomeAssistantClient, component_socket: AsyncMock
    ) -> None:
        """Core never answered, and the component's render ran out of time."""
        socket = await _socket(component_core_client)
        with patch.object(
            socket, "subscribe_command", AsyncMock(side_effect=TimeoutError)
        ):
            error = await _eval_error(
                component_core_client,
                template=RUNAWAY,
                report_errors=False,
                timeout=1,
            )

        assert error["error"]["code"] == "TIMEOUT_OPERATION"
        assert len(_diagnose_calls(component_socket)) == 1

    async def test_lost_verdict_with_report_errors_is_a_timeout(
        self, core_client: HomeAssistantClient
    ) -> None:
        """With report_errors Core always answers, so silence means it ran out."""
        socket = await _socket(core_client)
        with patch.object(
            socket, "subscribe_command", AsyncMock(side_effect=TimeoutError)
        ):
            error = await _eval_error(core_client, template="{{ 1 }}")

        assert error["error"]["code"] == "TIMEOUT_OPERATION"

    async def test_home_assistant_timeout_is_not_rendered_again(
        self, component_core_client: HomeAssistantClient, component_socket: AsyncMock
    ) -> None:
        error = await _eval_error(component_core_client, template=RUNAWAY, timeout=1)

        assert error["error"]["code"] == "TIMEOUT_OPERATION"
        assert "Exceeded maximum execution time" in error["error"]["message"]
        assert error["ha_error_code"] == "template_error"
        assert _diagnose_calls(component_socket) == []

    async def test_client_side_fault_is_not_blamed_on_the_template(
        self, component_core_client: HomeAssistantClient, component_socket: AsyncMock
    ) -> None:
        """Variables the client cannot send fail on this side, not in the template."""
        error = await _eval_error(
            component_core_client,
            template="{{ ids }}",
            variables={"ids": {"a", "b"}},
        )

        assert error["error"]["code"] == "SERVICE_CALL_FAILED"
        assert _diagnose_calls(component_socket) == []


class TestCondition:
    async def test_condition_result_and_template_errors(
        self, core_client: HomeAssistantClient
    ) -> None:
        condition = {"condition": "template", "value_template": "{{ undefined_var }}"}

        data = await _eval(core_client, condition=condition)

        assert data["success"] is True
        assert data["condition"] == condition
        assert data["result"] is False
        assert any("'undefined_var' is undefined" in w for w in data["warnings"])

    async def test_variables_reach_the_condition_and_no_errors_mean_no_warnings(
        self, core_client: HomeAssistantClient
    ) -> None:
        condition = {"condition": "template", "value_template": "{{ a == 1 }}"}

        data = await _eval(core_client, condition=condition, variables={"a": 1})

        assert data["result"] is True
        assert "warnings" not in data

    async def test_rejected_condition_keeps_home_assistant_message(
        self, core_client: HomeAssistantClient
    ) -> None:
        error = await _eval_error(core_client, condition={"condition": "nope"})

        assert error["error"]["code"] == "VALIDATION_FAILED"
        assert "nope" in error["error"]["message"]
        assert not error["error"]["message"].startswith("Command failed")
        assert error["ha_error_code"]

    async def test_transport_failure_names_the_condition(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        condition = {"condition": "state", "entity_id": "sun.sun", "state": "x"}
        socket = await _socket(core_client)
        with patch.object(
            socket,
            "send_command",
            AsyncMock(side_effect=HomeAssistantConnectionError("socket closed")),
        ):
            error = await _eval_error(core_client, condition=condition)

        assert error["condition"] == condition
        assert "template" not in error
