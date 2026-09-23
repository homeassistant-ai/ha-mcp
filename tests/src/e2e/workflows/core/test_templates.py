"""
E2E tests for ha_eval_template tool - Jinja2 template evaluation.

Tests the template evaluation functionality that allows testing and debugging
of Jinja2 templates used in Home Assistant automations and configurations.
"""

import logging

import pytest

from ...utilities.assertions import (
    MCPAssertions,
    assert_mcp_success,
    parse_mcp_result,
)

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
@pytest.mark.core
class TestEvalTemplate:
    """Test ha_eval_template tool functionality."""

    async def test_eval_simple_state_template(self, mcp_client):
        """Test evaluating a simple state access template."""
        logger.info("Testing ha_eval_template with simple state template")

        result = await mcp_client.call_tool(
            "ha_eval_template",
            {
                "template": "{{ states('sun.sun') }}",
            },
        )

        data = assert_mcp_success(result, "Eval simple state template")

        # Verify response structure
        assert "result" in data, f"Missing 'result' in response: {data}"
        assert data["result"] in ["above_horizon", "below_horizon"], (
            f"Unexpected template result: {data['result']}"
        )

        logger.info(f"Template result: {data['result']}")

    async def test_eval_now_template(self, mcp_client):
        """Test evaluating now() template function."""
        logger.info("Testing ha_eval_template with now() function")

        result = await mcp_client.call_tool(
            "ha_eval_template",
            {
                "template": "{{ now().strftime('%Y-%m-%d') }}",
            },
        )

        data = assert_mcp_success(result, "Eval now() template")

        assert "result" in data, f"Missing 'result': {data}"
        # Should be a date string in YYYY-MM-DD format
        result_str = str(data["result"])
        assert len(result_str) == 10, f"Unexpected date format: {result_str}"
        assert "-" in result_str, f"Missing dashes in date: {result_str}"

        logger.info(f"Current date from template: {data['result']}")

    async def test_eval_math_template(self, mcp_client):
        """Test evaluating mathematical operations in template."""
        logger.info("Testing ha_eval_template with math operations")

        result = await mcp_client.call_tool(
            "ha_eval_template",
            {
                "template": "{{ (10 + 5) * 2 }}",
            },
        )

        data = assert_mcp_success(result, "Eval math template")

        assert "result" in data, f"Missing 'result': {data}"
        # Convert result to number for comparison
        result_value = float(data["result"]) if data["result"] else 0
        assert result_value == 30, f"Math result mismatch: {data['result']}"

        logger.info(f"Math result: {data['result']}")

    async def test_eval_conditional_template(self, mcp_client):
        """Test evaluating conditional logic in template."""
        logger.info("Testing ha_eval_template with conditional")

        result = await mcp_client.call_tool(
            "ha_eval_template",
            {
                "template": "{{ 'Day' if now().hour < 24 else 'Never' }}",
            },
        )

        data = assert_mcp_success(result, "Eval conditional template")

        assert "result" in data, f"Missing 'result': {data}"
        # This condition is always true (hour < 24), so result should be 'Day'
        assert data["result"] == "Day", f"Conditional result mismatch: {data['result']}"

        logger.info(f"Conditional result: {data['result']}")

    async def test_eval_state_attr_template(self, mcp_client):
        """Test evaluating state_attr() function."""
        logger.info("Testing ha_eval_template with state_attr()")

        result = await mcp_client.call_tool(
            "ha_eval_template",
            {
                "template": "{{ state_attr('sun.sun', 'friendly_name') }}",
            },
        )

        data = assert_mcp_success(result, "Eval state_attr template")

        assert "result" in data, f"Missing 'result': {data}"
        assert data["result"] == "Sun", f"Friendly name mismatch: {data['result']}"

        logger.info(f"state_attr result: {data['result']}")

    async def test_eval_is_state_template(self, mcp_client):
        """Test evaluating is_state() function."""
        logger.info("Testing ha_eval_template with is_state()")

        # First get the actual state
        state_result = await mcp_client.call_tool(
            "ha_get_state",
            {"entity_id": "sun.sun"},
        )
        state_data = parse_mcp_result(state_result)
        current_state = state_data.get("data", {}).get("state", "unknown")

        # Now test is_state with that value
        result = await mcp_client.call_tool(
            "ha_eval_template",
            {
                "template": f"{{{{ is_state('sun.sun', '{current_state}') }}}}",
            },
        )

        data = assert_mcp_success(result, "Eval is_state template")

        assert "result" in data, f"Missing 'result': {data}"
        # Result should be True (as string or bool)
        result_lower = str(data["result"]).lower()
        assert result_lower == "true", f"is_state should be true: {data['result']}"

        logger.info(f"is_state result: {data['result']}")

    async def test_eval_filter_template(self, mcp_client):
        """Test evaluating template with filter."""
        logger.info("Testing ha_eval_template with filter")

        result = await mcp_client.call_tool(
            "ha_eval_template",
            {
                "template": "{{ 'hello world' | title }}",
            },
        )

        data = assert_mcp_success(result, "Eval filter template")

        assert "result" in data, f"Missing 'result': {data}"
        assert data["result"] == "Hello World", (
            f"Filter result mismatch: {data['result']}"
        )

        logger.info(f"Filter result: {data['result']}")

    async def test_eval_float_conversion_template(self, mcp_client):
        """Test evaluating float conversion with default."""
        logger.info("Testing ha_eval_template with float conversion")

        result = await mcp_client.call_tool(
            "ha_eval_template",
            {
                "template": "{{ '42.5' | float(0) }}",
            },
        )

        data = assert_mcp_success(result, "Eval float conversion")

        assert "result" in data, f"Missing 'result': {data}"
        result_value = float(data["result"])
        assert result_value == 42.5, f"Float conversion mismatch: {data['result']}"

        logger.info(f"Float result: {data['result']}")

    async def test_eval_invalid_template_syntax(self, mcp_client):
        """A syntax error returns Home Assistant's Jinja message (#2522)."""
        async with MCPAssertions(mcp_client) as mcp:
            await mcp.call_tool_failure(
                "ha_eval_template",
                {"template": "{% if %}"},
                expected_error="TemplateSyntaxError",
            )

    async def test_eval_runtime_error_keeps_jinja_message(self, mcp_client):
        """A runtime error returns the Jinja error, not ``Command failed: {}``.

        The template is the one from #2522: a dict treated as a string.
        """
        async with MCPAssertions(mcp_client) as mcp:
            await mcp.call_tool_failure(
                "ha_eval_template",
                {
                    "template": (
                        "{% set tv_items = [{'summary': '18:32-18:38 (6 min) - A'}] %}"
                        "{{ tv_items[0].split('-')[0] }}"
                    )
                },
                expected_error="has no attribute 'split'",
            )

    async def test_eval_undefined_function_is_an_error(self, mcp_client):
        """Calling an undefined function fails with the name in the message."""
        async with MCPAssertions(mcp_client) as mcp:
            await mcp.call_tool_failure(
                "ha_eval_template",
                {"template": "{{ invalid_function_xyz() }}"},
                expected_error="invalid_function_xyz",
            )

    async def test_eval_warning_keeps_the_result(self, mcp_client):
        """An undefined variable renders empty and is reported in ``warnings``."""
        async with MCPAssertions(mcp_client) as mcp:
            data = await mcp.call_tool_success(
                "ha_eval_template", {"template": "{{ undefined_var_xyz }}x"}
            )
        assert data["result"] == "x"
        assert any("undefined_var_xyz" in w for w in data.get("warnings", [])), data

    async def test_eval_strict_turns_the_warning_into_an_error(self, mcp_client):
        async with MCPAssertions(mcp_client) as mcp:
            await mcp.call_tool_failure(
                "ha_eval_template",
                {"template": "{{ undefined_var_xyz }}x", "strict": True},
                expected_error="undefined_var_xyz",
            )

    async def test_eval_variables(self, mcp_client):
        async with MCPAssertions(mcp_client) as mcp:
            data = await mcp.call_tool_success(
                "ha_eval_template",
                {"template": "{{ foo * 2 }}", "variables": {"foo": 21}},
            )
        assert data["result"] == 42

    async def test_eval_nonexistent_entity_template(self, mcp_client):
        """Test evaluating template with non-existent entity."""
        logger.info("Testing ha_eval_template with non-existent entity")

        result = await mcp_client.call_tool(
            "ha_eval_template",
            {
                "template": "{{ states('sensor.nonexistent_xyz_12345') }}",
            },
        )

        data = assert_mcp_success(result, "Eval non-existent entity template")

        # HA returns 'unknown' or 'unavailable' for non-existent entities
        assert "result" in data, f"Missing 'result': {data}"
        assert data["result"] in ["unknown", "unavailable", ""], (
            f"Unexpected result for non-existent entity: {data['result']}"
        )

        logger.info(f"Non-existent entity result: {data['result']}")

    async def test_eval_template_with_timeout(self, mcp_client):
        """Test template evaluation with custom timeout."""
        logger.info("Testing ha_eval_template with custom timeout")

        result = await mcp_client.call_tool(
            "ha_eval_template",
            {
                "template": "{{ states('sun.sun') }}",
                "timeout": 5,
            },
        )

        data = assert_mcp_success(result, "Eval template with timeout")

        logger.info(
            f"Template with timeout executed successfully: {data.get('result')}"
        )

    async def test_eval_template_counting_entities(self, mcp_client):
        """Test evaluating template that counts entities."""
        logger.info("Testing ha_eval_template counting entities")

        result = await mcp_client.call_tool(
            "ha_eval_template",
            {
                "template": "{{ states | length }}",
            },
        )

        data = assert_mcp_success(result, "Eval entity counting template")

        assert "result" in data, f"Missing 'result': {data}"
        count = int(data["result"]) if data["result"] else 0
        assert count > 0, f"Should have some entities: {count}"

        logger.info(f"Total entity count: {count}")

    async def test_eval_template_domain_listing(self, mcp_client):
        """Test evaluating template listing entities by domain."""
        logger.info("Testing ha_eval_template domain listing")

        result = await mcp_client.call_tool(
            "ha_eval_template",
            {
                "template": "{{ states.light | list | length }}",
            },
        )

        data = assert_mcp_success(result, "Eval domain listing template")

        assert "result" in data, f"Missing 'result': {data}"
        logger.info(f"Light entity count: {data['result']}")

    async def test_eval_multiline_template(self, mcp_client):
        """Test evaluating multi-line template."""
        logger.info("Testing ha_eval_template with multi-line template")

        template = """
        {% set sun_state = states('sun.sun') %}
        {% if sun_state == 'above_horizon' %}
        The sun is up
        {% else %}
        The sun is down
        {% endif %}
        """

        result = await mcp_client.call_tool(
            "ha_eval_template",
            {
                "template": template,
            },
        )

        data = assert_mcp_success(result, "Eval multi-line template")

        assert "result" in data, f"Missing 'result': {data}"
        result_text = str(data["result"]).strip()
        assert result_text in ["The sun is up", "The sun is down"], (
            f"Unexpected multi-line result: {result_text}"
        )

        logger.info(f"Multi-line template result: {result_text}")

    async def test_eval_template_response_includes_template(self, mcp_client):
        """Test that response includes the original template."""
        logger.info("Testing ha_eval_template response includes template")

        template = "{{ 1 + 1 }}"
        result = await mcp_client.call_tool(
            "ha_eval_template",
            {
                "template": template,
            },
        )

        data = assert_mcp_success(result, "Eval template response structure")

        assert "template" in data, f"Missing 'template' in response: {data}"
        assert data["template"] == template, f"Template mismatch: {data['template']}"

        logger.info("Response includes original template correctly")

    async def test_eval_template_list_operation(self, mcp_client):
        """Test evaluating template with list operations."""
        logger.info("Testing ha_eval_template with list operation")

        result = await mcp_client.call_tool(
            "ha_eval_template",
            {
                "template": "{{ [1, 2, 3, 4, 5] | sum }}",
            },
        )

        data = assert_mcp_success(result, "Eval list operation template")

        assert "result" in data, f"Missing 'result': {data}"
        result_value = int(data["result"]) if data["result"] else 0
        assert result_value == 15, f"List sum mismatch: {data['result']}"

        logger.info(f"List sum result: {data['result']}")


@pytest.mark.asyncio
@pytest.mark.core
async def test_eval_template_with_light_entity(mcp_client, test_light_entity):
    """Test evaluating template with a light entity."""
    logger.info(f"Testing ha_eval_template with light: {test_light_entity}")

    result = await mcp_client.call_tool(
        "ha_eval_template",
        {
            "template": f"{{{{ states('{test_light_entity}') }}}}",
        },
    )

    data = assert_mcp_success(result, "Eval template with light entity")

    assert "result" in data, f"Missing 'result': {data}"
    assert data["result"] in ["on", "off", "unavailable", "unknown"], (
        f"Unexpected light state from template: {data['result']}"
    )

    logger.info(f"Light state from template: {data['result']}")


@pytest.mark.asyncio
@pytest.mark.core
async def test_eval_template_brightness_calculation(mcp_client, test_light_entity):
    """Test evaluating template with brightness calculation."""
    logger.info("Testing ha_eval_template with brightness calculation")

    template = f"""
    {{%- set brightness = state_attr('{test_light_entity}', 'brightness') | default(0, true) | float(0) -%}}
    {{{{ (brightness / 255 * 100) | round(0) }}}}
    """

    result = await mcp_client.call_tool(
        "ha_eval_template",
        {
            "template": template,
        },
    )

    data = assert_mcp_success(result, "Eval brightness calculation template")

    assert "result" in data, f"Missing 'result': {data}"
    # Result should be a number 0-100 (percentage)
    result_value = float(data["result"]) if data["result"] else 0
    assert 0 <= result_value <= 100, (
        f"Brightness percentage out of range: {result_value}"
    )

    logger.info(f"Brightness percentage: {result_value}%")


@pytest.mark.asyncio
@pytest.mark.core
class TestEvalCondition:
    """ha_eval_template(condition=...) tests a condition through test_condition."""

    async def test_state_condition(self, mcp_client):
        async with MCPAssertions(mcp_client) as mcp:
            matching = await mcp.call_tool_success(
                "ha_eval_template",
                {
                    "condition": {
                        "condition": "state",
                        "entity_id": "sun.sun",
                        "state": ["above_horizon", "below_horizon"],
                    }
                },
            )
            other = await mcp.call_tool_success(
                "ha_eval_template",
                {
                    "condition": {
                        "condition": "state",
                        "entity_id": "sun.sun",
                        "state": "not_a_sun_state",
                    }
                },
            )
        assert matching["result"] is True
        assert other["result"] is False

    async def test_and_condition(self, mcp_client):
        async with MCPAssertions(mcp_client) as mcp:
            data = await mcp.call_tool_success(
                "ha_eval_template",
                {
                    "condition": {
                        "condition": "and",
                        "conditions": [
                            {"condition": "template", "value_template": "{{ true }}"},
                            {"condition": "template", "value_template": "{{ false }}"},
                        ],
                    }
                },
            )
        assert data["result"] is False

    async def test_condition_variables(self, mcp_client):
        async with MCPAssertions(mcp_client) as mcp:
            data = await mcp.call_tool_success(
                "ha_eval_template",
                {
                    "condition": {
                        "condition": "template",
                        "value_template": "{{ trigger.to_state.state == 'on' }}",
                    },
                    "variables": {"trigger": {"to_state": {"state": "on"}}},
                },
            )
        assert data["result"] is True

    async def test_condition_template_warning(self, mcp_client):
        """Template errors hit while the condition still evaluated are returned."""
        async with MCPAssertions(mcp_client) as mcp:
            data = await mcp.call_tool_success(
                "ha_eval_template",
                {
                    "condition": {
                        "condition": "template",
                        "value_template": "{{ undefined_var_xyz }}",
                    }
                },
            )
        assert data["result"] is False
        assert any("undefined_var_xyz" in w for w in data.get("warnings", [])), data

    async def test_invalid_condition_is_rejected(self, mcp_client):
        async with MCPAssertions(mcp_client) as mcp:
            await mcp.call_tool_failure(
                "ha_eval_template",
                {"condition": {"condition": "not_a_condition_type"}},
                expected_error="not_a_condition_type",
            )

    async def test_template_and_condition_together_are_rejected(self, mcp_client):
        async with MCPAssertions(mcp_client) as mcp:
            await mcp.call_tool_failure(
                "ha_eval_template",
                {
                    "template": "{{ 1 }}",
                    "condition": {"condition": "template", "value_template": "{{ 1 }}"},
                },
                expected_error="exactly one",
            )


async def _component_diagnoses(ha_client) -> bool:
    """Whether this lane's ha_mcp_tools component can name a template line.

    The component is absent on the no-component lanes and present, with the
    same WebSocket surface, on every other one, including the embedded
    server-entry-only lane.
    """
    info = await ha_client.send_websocket_message({"type": "ha_mcp_tools/info"})
    if info.get("success") is not True:
        return False
    return "template_diagnose" in info["result"]["capabilities"]


@pytest.mark.asyncio
@pytest.mark.core
class TestEvalTemplateErrorLocation:
    """With the ha_mcp_tools component a failure names its template line;
    without it the error still comes back, with no line."""

    async def _failure(self, mcp_client, args, expected_error):
        async with MCPAssertions(mcp_client) as mcp:
            return await mcp.call_tool_failure(
                "ha_eval_template", args, expected_error=expected_error
            )

    async def test_runtime_error_line(self, mcp_client, ha_client):
        template = "line one\n{% set d = {'a': 1} %}\n{{ d.split('-') }}\nline four"
        data = await self._failure(
            mcp_client, {"template": template}, "has no attribute 'split'"
        )
        if await _component_diagnoses(ha_client):
            assert data.get("line") == 3, data
            assert data.get("source_line") == "{{ d.split('-') }}", data
        else:
            assert "line" not in data, data

    async def test_leading_blank_lines_keep_the_callers_numbering(
        self, mcp_client, ha_client
    ):
        """Core strips the template; the line still points at the caller's text."""
        template = "\n\nline three\n{{ 1/0 }}\n"
        data = await self._failure(
            mcp_client, {"template": template}, "division by zero"
        )
        if await _component_diagnoses(ha_client):
            assert data.get("line") == 4, data
            assert data.get("source_line") == "{{ 1/0 }}", data
        else:
            assert "line" not in data, data

    async def test_syntax_error_line(self, mcp_client, ha_client):
        data = await self._failure(
            mcp_client,
            {"template": "line one\n{% set x = 1 %}\n{% if x == %}\nline four"},
            "TemplateSyntaxError",
        )
        if await _component_diagnoses(ha_client):
            assert data.get("line") == 3, data
        else:
            assert "line" not in data, data

    async def test_report_errors_false(self, mcp_client, ha_client):
        """HA only logs this failure; the component render recovers it."""
        if await _component_diagnoses(ha_client):
            data = await self._failure(
                mcp_client,
                {"template": "{{ 1/0 }}", "report_errors": False},
                "division by zero",
            )
            assert data.get("line") == 1, data
        else:
            await self._failure(
                mcp_client,
                {"template": "{{ 1/0 }}", "report_errors": False},
                "report_errors=true",
            )

    async def test_runaway_template_times_out_and_home_assistant_stays_responsive(
        self, mcp_client
    ):
        runaway = (
            "{% for i in range(100000) %}{% for j in range(100000) %}"
            "{% endfor %}{% endfor %}"
        )
        async with MCPAssertions(mcp_client) as mcp:
            data = await mcp.call_tool_failure(
                "ha_eval_template",
                {"template": runaway, "timeout": 1},
                expected_error="Exceeded maximum execution time",
            )
            assert data["error"]["code"] == "TIMEOUT_OPERATION", data
            after = await mcp.call_tool_success(
                "ha_eval_template", {"template": "{{ 1 + 1 }}"}
            )
        assert after["result"] == 2
