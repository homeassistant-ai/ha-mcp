"""
Unit tests for flow-helper `name` injection logic (issue #1150, Bug 19).

The tool injects the top-level ``name`` parameter into the flow form
payload for create actions, but only when the helper's user-step schema
actually accepts a ``name`` field. ``switch_as_x`` is the canonical
counter-example: its user step takes only ``entity_id`` and ``target_domain``;
HA rejects an extra ``name`` key with ``"extra keys not allowed @ data['name']"``.

For update actions, options flows never accept ``name`` (you cannot rename
a flow helper through its options flow). Any caller-supplied ``name``
inside ``config`` must be stripped, and the response must surface a
warning so the caller learns the rename attempt was a no-op.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def mock_client():
    return MagicMock()


@pytest.fixture
def register_tools(mock_client):
    """Register helper config tools and return the captured tool functions."""
    from ha_mcp.tools.tools_config_helpers import register_config_helper_tools

    registered_tools: dict[str, Any] = {}

    def capture_tool(**kwargs):
        def decorator(fn):
            registered_tools[fn.__name__] = fn
            return fn

        return decorator

    mock_mcp = MagicMock()
    mock_mcp.tool = capture_tool
    register_config_helper_tools(mock_mcp, mock_client)
    return registered_tools


def _make_start_config_flow(
    schema_fields: list[str] | None,
    *,
    flow_id: str = "introspect-flow",
    entry_id: str = "entry-1",
    title: str = "probe",
    domain: str = "min_max",
    intro_calls: list[str] | None = None,
    submit_capture: list[dict] | None = None,
):
    """Build a stateful start_config_flow side-effect that handles two phases:

    1. Schema introspection call (returns a form with the given schema_fields,
       or ``type=menu`` when schema_fields is None to simulate a menu helper).
    2. Real create call — returns a form whose submission yields create_entry.

    ``intro_calls`` is appended to with the helper_type each time
    start_config_flow is invoked, so tests can verify the introspection
    happened. ``submit_capture`` collects the final submitted form data.
    """
    state = {"calls": 0}

    async def start_flow(helper_type: str) -> dict[str, Any]:
        state["calls"] += 1
        if intro_calls is not None:
            intro_calls.append(helper_type)
        if state["calls"] == 1:
            # Introspection phase
            if schema_fields is None:
                return {
                    "type": "menu",
                    "flow_id": flow_id + "-intro",
                    "menu_options": ["sensor", "binary_sensor"],
                }
            return {
                "type": "form",
                "flow_id": flow_id + "-intro",
                "step_id": "user",
                "data_schema": [
                    {"name": fname} for fname in schema_fields
                ],
            }
        # Real flow phase
        return {
            "type": "form",
            "flow_id": flow_id,
            "step_id": "user",
            "data_schema": [
                {"name": fname} for fname in (schema_fields or ["name"])
            ],
        }

    async def submit(_flow_id: str, data: dict[str, Any]) -> dict[str, Any]:
        if submit_capture is not None:
            submit_capture.append(dict(data))
        return {
            "type": "create_entry",
            "result": {
                "entry_id": entry_id,
                "title": title,
                "domain": domain,
            },
        }

    return start_flow, submit


class TestSwitchAsXNameNotInjected:
    """switch_as_x's user-step schema has no `name` field — name must NOT be
    folded into config. Otherwise HA replies 400 'extra keys not allowed'."""

    async def test_switch_as_x_create_omits_name_from_submitted_config(
        self, register_tools, mock_client
    ):
        intro_calls: list[str] = []
        submit_capture: list[dict] = []
        start_flow, submit = _make_start_config_flow(
            schema_fields=["entity_id", "target_domain"],
            entry_id="entry-sax",
            domain="switch_as_x",
            intro_calls=intro_calls,
            submit_capture=submit_capture,
        )

        mock_client.start_config_flow = AsyncMock(side_effect=start_flow)
        mock_client.submit_config_flow_step = AsyncMock(side_effect=submit)
        mock_client.abort_config_flow = AsyncMock(return_value={})
        mock_client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": []}
        )

        result = await register_tools["ha_config_set_helper"](
            helper_type="switch_as_x",
            name="My Light From Switch",
            config={
                "entity_id": "switch.basement",
                "target_domain": "light",
            },
            wait=False,
        )

        assert result["success"] is True, result
        # The actual create flow start happens after the introspection start;
        # both call start_config_flow with helper_type="switch_as_x".
        assert intro_calls.count("switch_as_x") >= 1
        # No submitted payload should contain the 'name' key.
        assert submit_capture, "expected at least one form submission"
        for payload in submit_capture:
            assert "name" not in payload, (
                f"switch_as_x submission must not include 'name', got: {payload}"
            )


class TestTemplateAndFormHelperNameInjected:
    """Helpers whose user step accepts `name` (template, min_max, etc.)
    must still receive the top-level `name` in their submitted config."""

    async def test_min_max_create_includes_name(
        self, register_tools, mock_client
    ):
        submit_capture: list[dict] = []
        start_flow, submit = _make_start_config_flow(
            schema_fields=["name", "entity_ids", "type"],
            entry_id="entry-mm",
            domain="min_max",
            submit_capture=submit_capture,
        )

        mock_client.start_config_flow = AsyncMock(side_effect=start_flow)
        mock_client.submit_config_flow_step = AsyncMock(side_effect=submit)
        mock_client.abort_config_flow = AsyncMock(return_value={})
        mock_client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": []}
        )

        result = await register_tools["ha_config_set_helper"](
            helper_type="min_max",
            name="avg_temp",
            config={"entity_ids": ["sensor.a", "sensor.b"], "type": "mean"},
            wait=False,
        )

        assert result["success"] is True, result
        assert submit_capture, "expected at least one form submission"
        # The form-step submission for min_max should carry name.
        names_seen = [p.get("name") for p in submit_capture if "name" in p]
        assert "avg_temp" in names_seen, (
            f"expected 'avg_temp' in submitted name fields, got: {submit_capture}"
        )

    async def test_template_menu_helper_still_injects_name(
        self, register_tools, mock_client
    ):
        """Template's top step is a menu — introspection returns type=menu
        and we have no field-list to check. The legacy fallback (inject
        anyway) keeps template/group working unchanged."""
        submit_capture: list[dict] = []
        # schema_fields=None signals "introspection sees a menu, not a form"
        start_flow, submit = _make_start_config_flow(
            schema_fields=None,
            entry_id="entry-tpl",
            domain="template",
            submit_capture=submit_capture,
        )

        # Real flow: menu -> sensor sub-form -> create_entry
        submit_responses = [
            # First submit: menu choice -> form with `name`
            {
                "type": "form",
                "flow_id": "real-flow",
                "step_id": "sensor",
                "data_schema": [{"name": "name"}, {"name": "state"}],
            },
            # Second submit: form values -> create_entry
            {
                "type": "create_entry",
                "result": {
                    "entry_id": "entry-tpl",
                    "title": "tpl",
                    "domain": "template",
                },
            },
        ]

        async def real_submit(_flow_id: str, data: dict[str, Any]) -> dict:
            submit_capture.append(dict(data))
            return submit_responses.pop(0)

        mock_client.start_config_flow = AsyncMock(
            side_effect=[
                # Call #1 (intro): menu — returns None from get_user_step_field_names
                {
                    "type": "menu",
                    "flow_id": "intro",
                    "menu_options": ["sensor", "binary_sensor"],
                },
                # Call #2 (real create): also a menu, walked by _handle_flow_steps
                {
                    "type": "menu",
                    "flow_id": "real",
                    "menu_options": ["sensor", "binary_sensor"],
                },
            ]
        )
        mock_client.submit_config_flow_step = AsyncMock(side_effect=real_submit)
        mock_client.abort_config_flow = AsyncMock(return_value={})
        mock_client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": []}
        )

        result = await register_tools["ha_config_set_helper"](
            helper_type="template",
            name="my_template",
            config={
                "menu_option": "sensor",
                "state": "{{ states('sensor.x') }}",
            },
            wait=False,
        )

        assert result["success"] is True, result
        # The form-step submission must carry name (menu fallback injects it).
        names_seen = [p.get("name") for p in submit_capture if "name" in p]
        assert "my_template" in names_seen, (
            f"expected name to be injected for template (menu fallback), "
            f"got: {submit_capture}"
        )


class TestUpdateStripsName:
    """Options flows reject `name` as an extra key. The tool must strip it
    from caller-supplied config and surface a warning."""

    async def test_update_strips_name_and_warns(
        self, register_tools, mock_client
    ):
        submit_capture: list[dict] = []

        mock_client.get_config_entry = AsyncMock(
            return_value={"domain": "min_max", "entry_id": "entry-1"}
        )
        mock_client.start_options_flow = AsyncMock(
            return_value={
                "type": "form",
                "flow_id": "options-flow",
                "step_id": "init",
                "data_schema": [{"name": "type"}, {"name": "entity_ids"}],
            }
        )

        async def submit(_flow_id: str, data: dict[str, Any]) -> dict[str, Any]:
            submit_capture.append(dict(data))
            return {
                "type": "create_entry",
                "result": {"entry_id": "entry-1", "title": "avg"},
            }

        mock_client.submit_options_flow_step = AsyncMock(side_effect=submit)
        mock_client.abort_options_flow = AsyncMock(return_value={})
        mock_client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": []}
        )

        result = await register_tools["ha_config_set_helper"](
            helper_type="min_max",
            helper_id="entry-1",
            config={
                # Caller mistakenly tries to rename the helper here.
                "name": "renamed_helper",
                "entity_ids": ["sensor.x", "sensor.y"],
                "type": "max",
            },
            wait=False,
        )

        assert result["success"] is True, result
        assert result["action"] == "update"

        # No submission should have carried `name` to the options flow.
        for payload in submit_capture:
            assert "name" not in payload, (
                f"options flow submission must not include 'name', "
                f"got: {payload}"
            )

        # The result must surface a warning explaining the strip.
        assert "warnings" in result, result
        assert any(
            "name" in w.lower() and "rename" in w.lower()
            for w in result["warnings"]
        ), f"expected rename warning, got: {result['warnings']}"

    async def test_update_without_name_in_config_no_warning(
        self, register_tools, mock_client
    ):
        """Updates that don't include `name` should NOT emit a rename warning."""
        mock_client.get_config_entry = AsyncMock(
            return_value={"domain": "min_max", "entry_id": "entry-2"}
        )
        mock_client.start_options_flow = AsyncMock(
            return_value={
                "type": "create_entry",
                "flow_id": "options-flow",
                "result": {"entry_id": "entry-2", "title": "avg"},
            }
        )
        mock_client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": []}
        )

        result = await register_tools["ha_config_set_helper"](
            helper_type="min_max",
            helper_id="entry-2",
            config={"entity_ids": ["sensor.x"], "type": "max"},
            wait=False,
        )

        assert result["success"] is True
        # If warnings exist they must not be the rename warning.
        for w in result.get("warnings", []):
            assert "rename" not in w.lower(), (
                f"unexpected rename warning when name was not in config: {w}"
            )
