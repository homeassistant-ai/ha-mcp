"""
Unit tests for multi-step config flow handling (Bug #18 / issue #1150).

Verifies that ``_handle_flow_steps`` correctly walks a multi-step HA config
flow, submitting only the keys declared in each step's ``data_schema`` and
preserving the remaining keys for subsequent steps.

Regression guard: prior code wiped ``remaining_config`` after the first form
step, which made step 2+ submit ``{}`` and HA respond with HTTP 400. This
broke ``statistics`` (multi-step user → pick-characteristic) and
``utility_meter`` UPDATE.
"""

from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.client.rest_client import HomeAssistantAPIError
from ha_mcp.tools.config_entry_flow import (
    _extract_schema_field_names,
    _handle_config_subentry_flow_steps,
    _handle_flow_steps,
    _handle_form_step,
    _submit_step,
)


class TestExtractSchemaFieldNames:
    """Sanity-check the schema parser used to drive per-step key filtering."""

    def test_extracts_names_from_dict_list(self) -> None:
        schema = [
            {"name": "name", "required": True, "selector": {"text": {}}},
            {"name": "entity_id", "selector": {"entity": {}}},
        ]
        assert _extract_schema_field_names(schema) == {"name", "entity_id"}

    def test_extracts_names_from_expandable_sections(self) -> None:
        schema = [
            {"name": "state", "required": True, "selector": {"template": {}}},
            {
                "type": "expandable",
                "name": "advanced_options",
                "schema": [
                    {
                        "name": "availability",
                        "required": False,
                        "selector": {"template": {}},
                    }
                ],
            },
        ]

        assert _extract_schema_field_names(schema) == {"state", "availability"}

    def test_handles_missing_or_malformed_schema(self) -> None:
        # Non-list inputs signal "schema not available" → None (legacy fallback).
        assert _extract_schema_field_names(None) is None
        assert _extract_schema_field_names({}) is None
        # A list with no parseable name fields is still a valid (empty) schema.
        assert _extract_schema_field_names([{"no_name_key": "x"}]) == set()
        assert _extract_schema_field_names([{"name": 123}]) == set()


class TestHandleFormStepFiltering:
    """Direct test of the per-step filter that splits config across steps."""

    def test_pops_only_schema_fields_leaves_rest(self) -> None:
        remaining = {
            "name": "Avg Temp",
            "entity_id": "sensor.foo",
            "state_characteristic": "mean",  # belongs to step 2
            "extra_key": "should_remain",
        }
        step = {
            "type": "form",
            "step_id": "user",
            "data_schema": [
                {"name": "name", "required": True},
                {"name": "entity_id", "required": True},
            ],
        }

        form_data = _handle_form_step("flow-1", step, remaining)

        assert form_data == {"name": "Avg Temp", "entity_id": "sensor.foo"}
        # Keys not in this step's schema must stay for later steps.
        assert remaining == {
            "state_characteristic": "mean",
            "extra_key": "should_remain",
        }

    def test_wraps_flat_expandable_fields_for_submission(self) -> None:
        remaining = {
            "state": "{{ 1 }}",
            "availability": "{{ has_value('sensor.x') }}",
        }
        step = {
            "type": "form",
            "step_id": "sensor",
            "data_schema": [
                {"name": "state", "required": True},
                {
                    "type": "expandable",
                    "name": "advanced_options",
                    "schema": [{"name": "availability", "required": False}],
                },
            ],
        }

        form_data = _handle_form_step("flow-1", step, remaining)

        assert form_data == {
            "state": "{{ 1 }}",
            "advanced_options": {
                "availability": "{{ has_value('sensor.x') }}",
            },
        }
        assert remaining == {}

    def test_accepts_explicit_expandable_section_dict(self) -> None:
        remaining = {
            "state": "{{ 1 }}",
            "advanced_options": {
                "availability": "{{ has_value('sensor.x') }}",
            },
        }
        step = {
            "type": "form",
            "step_id": "sensor",
            "data_schema": [
                {"name": "state", "required": True},
                {
                    "type": "expandable",
                    "name": "advanced_options",
                    "schema": [{"name": "availability", "required": False}],
                },
            ],
        }

        form_data = _handle_form_step("flow-1", step, remaining)

        assert form_data == {
            "state": "{{ 1 }}",
            "advanced_options": {
                "availability": "{{ has_value('sensor.x') }}",
            },
        }
        assert remaining == {}

    def test_required_expandable_section_uses_schema_suggestions(self) -> None:
        """Generic Camera's required advanced section has HA-suggested defaults."""
        remaining = {"stream_source": "rtsp://camera.example/stream"}
        step = {
            "type": "form",
            "step_id": "user",
            "data_schema": [
                {"name": "stream_source", "required": False},
                {
                    "type": "expandable",
                    "name": "advanced",
                    "required": True,
                    "expanded": False,
                    "schema": [
                        {
                            "name": "framerate",
                            "required": True,
                            "description": {"suggested_value": 2},
                        },
                        {
                            "name": "verify_ssl",
                            "required": True,
                            "description": {"suggested_value": True},
                        },
                        {
                            "name": "rtsp_transport",
                            "required": False,
                            "description": {"suggested_value": "tcp"},
                        },
                    ],
                },
            ],
        }

        form_data = _handle_form_step("flow-1", step, remaining)

        assert form_data == {
            "stream_source": "rtsp://camera.example/stream",
            "advanced": {
                "framerate": 2,
                "verify_ssl": True,
                "rtsp_transport": "tcp",
            },
        }
        assert remaining == {}

    def test_required_expandable_section_config_overrides_suggestions(self) -> None:
        remaining = {
            "stream_source": "rtsp://camera.example/stream",
            "advanced": {"framerate": 5},
            "verify_ssl": False,
        }
        step = {
            "type": "form",
            "step_id": "user",
            "data_schema": [
                {"name": "stream_source", "required": False},
                {
                    "type": "expandable",
                    "name": "advanced",
                    "required": True,
                    "schema": [
                        {
                            "name": "framerate",
                            "required": True,
                            "description": {"suggested_value": 2},
                        },
                        {
                            "name": "verify_ssl",
                            "required": True,
                            "description": {"suggested_value": True},
                        },
                    ],
                },
            ],
        }

        form_data = _handle_form_step("flow-1", step, remaining)

        assert form_data == {
            "stream_source": "rtsp://camera.example/stream",
            "advanced": {"framerate": 5, "verify_ssl": False},
        }
        assert remaining == {}

    def test_omits_section_when_only_top_level_field_is_updated(self) -> None:
        remaining = {"state": "{{ 2 }}"}
        step = {
            "type": "form",
            "step_id": "sensor",
            "data_schema": [
                {"name": "state", "required": True},
                {
                    "type": "expandable",
                    "name": "advanced_options",
                    "schema": [{"name": "availability", "required": False}],
                },
            ],
        }

        form_data = _handle_form_step("flow-1", step, remaining)

        assert form_data == {"state": "{{ 2 }}"}
        assert remaining == {}

    def test_flat_section_field_overrides_explicit_section_value(self) -> None:
        remaining = {
            "availability": "{{ true }}",
            "advanced_options": {"availability": "{{ false }}"},
        }
        step = {
            "type": "form",
            "step_id": "sensor",
            "data_schema": [
                {
                    "type": "expandable",
                    "name": "advanced_options",
                    "schema": [{"name": "availability", "required": False}],
                },
            ],
        }

        form_data = _handle_form_step("flow-1", step, remaining)

        assert form_data == {
            "advanced_options": {"availability": "{{ true }}"},
        }
        assert remaining == {}

    def test_wraps_depth_two_flat_field_for_submission(self) -> None:
        remaining = {"delay": 30}
        step = {
            "type": "form",
            "step_id": "nested",
            "data_schema": [
                {
                    "type": "expandable",
                    "name": "advanced_options",
                    "schema": [
                        {
                            "type": "expandable",
                            "name": "timing",
                            "schema": [{"name": "delay"}],
                        }
                    ],
                },
            ],
        }

        form_data = _handle_form_step("flow-1", step, remaining)

        assert form_data == {
            "advanced_options": {
                "timing": {"delay": 30},
            },
        }
        assert remaining == {}

    def test_legacy_fallback_submits_all_non_menu_keys(self) -> None:
        remaining = {
            "name": "Legacy",
            "unknown": "still submitted",
            "next_step_id": "sensor",
        }
        step = {"type": "form", "step_id": "user", "data_schema": None}

        form_data = _handle_form_step("flow-1", step, remaining)

        assert form_data == {
            "name": "Legacy",
            "unknown": "still submitted",
        }
        assert remaining == {"next_step_id": "sensor"}

    def test_passes_through_non_dict_explicit_section_value(self) -> None:
        remaining = {"advanced_options": "invalid"}
        step = {
            "type": "form",
            "step_id": "sensor",
            "data_schema": [
                {
                    "type": "expandable",
                    "name": "advanced_options",
                    "schema": [{"name": "availability"}],
                }
            ],
        }

        form_data = _handle_form_step("flow-1", step, remaining)

        assert form_data == {"advanced_options": "invalid"}
        assert remaining == {}

    def test_flattens_children_when_section_name_is_missing(self) -> None:
        remaining = {"availability": "{{ true }}"}
        step = {
            "type": "form",
            "step_id": "sensor",
            "data_schema": [
                {
                    "type": "expandable",
                    "schema": [{"name": "availability"}],
                }
            ],
        }

        form_data = _handle_form_step("flow-1", step, remaining)

        assert form_data == {"availability": "{{ true }}"}
        assert remaining == {}

    def test_strips_menu_selection_keys(self) -> None:
        remaining = {"group_type": "light", "name": "x"}
        step = {
            "type": "form",
            "step_id": "init",
            "data_schema": [
                {"name": "name"},
                # Even if HA includes a key matching a menu selection name,
                # _MENU_SELECTION_KEYS takes precedence as a safety check.
                {"name": "group_type"},
            ],
        }
        form_data = _handle_form_step("flow-1", step, remaining)
        assert form_data == {"name": "x"}
        # group_type was popped from remaining only via the menu-key skip
        # branch — it stays put because the schema-field branch is not reached.
        assert remaining == {"group_type": "light"}


class TestMultiStepFlow:
    """End-to-end walk of a fake 2-step flow via _handle_flow_steps."""

    async def test_two_form_steps_each_get_correct_keys(self) -> None:
        """Step 1 expects {name, entity_id}; step 2 expects {state_characteristic}.

        Both steps must receive ONLY the keys that match their schemas, and
        step 2 must NOT receive an empty dict (the original bug).
        """
        # Step 2 form, returned after step 1 is submitted.
        step2_form: dict[str, Any] = {
            "type": "form",
            "flow_id": "flow-1",
            "step_id": "state_characteristic",
            "data_schema": [
                {"name": "state_characteristic", "required": True},
            ],
        }
        # Final create_entry, returned after step 2 is submitted.
        final_entry: dict[str, Any] = {
            "type": "create_entry",
            "flow_id": "flow-1",
            "result": {
                "entry_id": "entry-stat-1",
                "title": "Avg Temp",
                "domain": "statistics",
            },
        }

        submit_fn = AsyncMock(side_effect=[step2_form, final_entry])

        initial_step: dict[str, Any] = {
            "type": "form",
            "flow_id": "flow-1",
            "step_id": "user",
            "data_schema": [
                {"name": "name", "required": True},
                {"name": "entity_id", "required": True},
            ],
        }

        config = {
            "name": "Avg Temp",
            "entity_id": "sensor.foo",
            "state_characteristic": "mean",
        }

        result = await _handle_flow_steps(
            client=None,  # unused because submit_fn is provided
            flow_id="flow-1",
            initial_step=initial_step,
            config=config,
            submit_fn=submit_fn,
        )

        assert result == {"success": True, "entry": final_entry}
        assert submit_fn.await_count == 2

        # Step 1: the user step
        first_call_args = submit_fn.await_args_list[0].args
        assert first_call_args[0] == "flow-1"
        assert first_call_args[1] == {
            "name": "Avg Temp",
            "entity_id": "sensor.foo",
        }

        # Step 2: the state_characteristic step — MUST receive its key,
        # not {} (the bug). Must NOT receive step-1 keys.
        second_call_args = submit_fn.await_args_list[1].args
        assert second_call_args[0] == "flow-1"
        assert second_call_args[1] == {"state_characteristic": "mean"}

    async def test_extra_unknown_keys_are_reported_as_warnings(self) -> None:
        """Keys never declared by any step are omitted and reported."""
        final_entry = {
            "type": "create_entry",
            "result": {"entry_id": "e1", "title": "t", "domain": "min_max"},
        }
        submit_fn = AsyncMock(side_effect=[final_entry])

        initial_step = {
            "type": "form",
            "flow_id": "flow-2",
            "step_id": "user",
            "data_schema": [
                {"name": "name"},
                {"name": "entity_ids"},
                {"name": "type"},
            ],
        }
        config = {
            "name": "x",
            "entity_ids": ["sensor.a"],
            "type": "mean",
            "junk": "ignored",
        }

        result = await _handle_flow_steps(
            client=None,
            flow_id="flow-2",
            initial_step=initial_step,
            config=config,
            submit_fn=submit_fn,
        )

        submitted = submit_fn.await_args_list[0].args[1]
        assert "junk" not in submitted
        assert submitted == {
            "name": "x",
            "entity_ids": ["sensor.a"],
            "type": "mean",
        }
        assert result["warnings"] == [
            "Ignored config keys not declared by the Home Assistant flow schema: junk"
        ]

    async def test_unknown_explicit_section_keys_are_reported_with_path(self) -> None:
        final_entry = {
            "type": "create_entry",
            "result": {"entry_id": "e1", "title": "t", "domain": "template"},
        }
        submit_fn = AsyncMock(side_effect=[final_entry])
        initial_step = {
            "type": "form",
            "flow_id": "flow-3",
            "step_id": "sensor",
            "data_schema": [
                {"name": "state"},
                {
                    "type": "expandable",
                    "name": "advanced_options",
                    "schema": [{"name": "availability"}],
                },
            ],
        }

        result = await _handle_flow_steps(
            client=None,
            flow_id="flow-3",
            initial_step=initial_step,
            config={
                "state": "{{ 1 }}",
                "advanced_options": {"availabilty": "{{ true }}"},
            },
            submit_fn=submit_fn,
        )

        assert result["warnings"] == [
            "Ignored config keys not declared by the Home Assistant flow schema: "
            "advanced_options.availabilty"
        ]


class TestSubentryFlowIgnoredKeys:
    """The subentry walker reports ignored keys like the main flow walker."""

    async def test_subentry_create_reports_ignored_keys(self) -> None:
        final_entry = {"type": "create_entry", "result": {"entry_id": "e1"}}
        client = AsyncMock()
        client.submit_config_subentry_flow_step = AsyncMock(side_effect=[final_entry])
        initial_step = {
            "type": "form",
            "flow_id": "flow-4",
            "step_id": "user",
            "data_schema": [{"name": "name"}],
        }

        result = await _handle_config_subentry_flow_steps(
            client,
            "flow-4",
            initial_step,
            {"name": "x", "junk": "ignored"},
            is_reconfigure=False,
        )

        submitted = client.submit_config_subentry_flow_step.await_args_list[0].args[1]
        assert "junk" not in submitted
        assert result["operation"] == "created"
        assert result["warnings"] == [
            "Ignored config keys not declared by the Home Assistant flow schema: junk"
        ]

    async def test_subentry_reconfigure_abort_reports_ignored_keys(self) -> None:
        abort_step = {"type": "abort", "reason": "reconfigure_successful"}
        client = AsyncMock()
        client.submit_config_subentry_flow_step = AsyncMock(side_effect=[abort_step])
        initial_step = {
            "type": "form",
            "flow_id": "flow-5",
            "step_id": "reconfigure",
            "data_schema": [{"name": "name"}],
        }

        result = await _handle_config_subentry_flow_steps(
            client,
            "flow-5",
            initial_step,
            {"name": "x", "junk": "ignored"},
            is_reconfigure=True,
        )

        assert result["operation"] == "reconfigured"
        assert result["warnings"] == [
            "Ignored config keys not declared by the Home Assistant flow schema: junk"
        ]

    async def test_subentry_reports_unknown_explicit_section_keys_with_path(
        self,
    ) -> None:
        """The threaded ignored-keys set reaches the subentry success path."""
        final_entry = {"type": "create_entry", "result": {"entry_id": "e1"}}
        client = AsyncMock()
        client.submit_config_subentry_flow_step = AsyncMock(side_effect=[final_entry])
        initial_step = {
            "type": "form",
            "flow_id": "flow-6",
            "step_id": "user",
            "data_schema": [
                {"name": "name"},
                {
                    "type": "expandable",
                    "name": "advanced_options",
                    "schema": [{"name": "availability"}],
                },
            ],
        }

        result = await _handle_config_subentry_flow_steps(
            client,
            "flow-6",
            initial_step,
            {"name": "x", "advanced_options": {"availabilty": "{{ true }}"}},
            is_reconfigure=False,
        )

        assert result["warnings"] == [
            "Ignored config keys not declared by the Home Assistant flow schema: "
            "advanced_options.availabilty"
        ]

    async def test_menu_key_without_menu_step_is_reported(self) -> None:
        """A menu selection key supplied to a menu-less flow is surfaced."""
        final_entry = {"type": "create_entry", "result": {"entry_id": "e1"}}
        client = AsyncMock()
        client.submit_config_subentry_flow_step = AsyncMock(side_effect=[final_entry])
        initial_step = {
            "type": "form",
            "flow_id": "flow-7",
            "step_id": "user",
            "data_schema": [{"name": "name"}],
        }

        result = await _handle_config_subentry_flow_steps(
            client,
            "flow-7",
            initial_step,
            {"name": "x", "next_step_id": "conversation"},
            is_reconfigure=False,
        )

        submitted = client.submit_config_subentry_flow_step.await_args_list[0].args[1]
        assert "next_step_id" not in submitted
        assert result["warnings"] == [
            "Ignored menu selection key(s) with no matching menu step: next_step_id"
        ]


class TestSubmitStep:
    """Unit tests for _submit_step error propagation."""

    @pytest.mark.asyncio
    async def test_non_400_422_api_error_propagates_unwrapped(self):
        """A 500 HomeAssistantAPIError must re-raise unchanged, not be swallowed."""
        err = HomeAssistantAPIError("server error", status_code=500)
        submit_fn = AsyncMock(side_effect=err)
        dummy_step: dict[str, Any] = {"step_id": "user", "type": "form"}

        with pytest.raises(HomeAssistantAPIError) as exc_info:
            await _submit_step(
                submit_fn,
                "flow-1",
                {"name": "x"},
                client=None,
                helper_type=None,
                last_menu_choice=None,
                current_step=dummy_step,
            )

        assert exc_info.value is err
        assert exc_info.value.status_code == 500


class TestAllKeysIgnoredIsAnError:
    """When NONE of the supplied config keys match any step's schema, the
    walker must raise instead of reporting a misleading "updated
    successfully" — the flow completed on empty forms (defaults), applying
    nothing the caller asked for. Partial consumption keeps the established
    success + warnings contract (covered above).
    """

    async def test_all_supplied_keys_ignored_raises(self) -> None:
        import json

        from fastmcp.exceptions import ToolError

        final_entry = {"type": "create_entry", "result": {"entry_id": "e1"}}
        submit_fn = AsyncMock(side_effect=[final_entry])
        initial_step = {
            "type": "form",
            "flow_id": "flow-typo",
            "step_id": "init",
            "data_schema": [{"name": "hide_members"}, {"name": "entities"}],
        }

        with pytest.raises(ToolError) as exc_info:
            await _handle_flow_steps(
                client=None,
                flow_id="flow-typo",
                initial_step=initial_step,
                config={"typo_key": 5, "another_typo": True},
                submit_fn=submit_fn,
            )

        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "without consuming any" in body["error"]["message"]
        assert body.get("supplied_keys") == ["another_typo", "typo_key"]
        # The empty form WAS submitted (single-step flows commit before the
        # mismatch is knowable) — the error is about the outcome contract.
        submit_fn.assert_awaited_once()
        assert submit_fn.await_args.args[1] == {}

    async def test_empty_config_still_succeeds(self) -> None:
        # config={} supplies nothing, so nothing was ignored — deliberate
        # empty submits (confirm-only flows) must keep working.
        final_entry = {"type": "create_entry", "result": {"entry_id": "e1"}}
        submit_fn = AsyncMock(side_effect=[final_entry])
        initial_step = {
            "type": "form",
            "flow_id": "flow-confirm",
            "step_id": "confirm",
            "data_schema": [],
        }

        result = await _handle_flow_steps(
            client=None,
            flow_id="flow-confirm",
            initial_step=initial_step,
            config={},
            submit_fn=submit_fn,
        )

        assert result["success"] is True
        assert "warnings" not in result

    async def test_menu_only_selection_still_succeeds(self) -> None:
        # A caller whose config is JUST a menu selection consumed by a menu
        # step supplied no form keys — that's a complete, valid intent.
        final_entry = {"type": "create_entry", "result": {"entry_id": "e1"}}
        submit_fn = AsyncMock(side_effect=[final_entry])
        initial_step = {
            "type": "menu",
            "flow_id": "flow-menu",
            "step_id": "user",
            "menu_options": ["light", "switch"],
        }

        result = await _handle_flow_steps(
            client=None,
            flow_id="flow-menu",
            initial_step=initial_step,
            config={"group_type": "light"},
            submit_fn=submit_fn,
        )

        assert result["success"] is True
        assert submit_fn.await_args.args[1] == {"next_step_id": "light"}

    async def test_instant_create_entry_keeps_success_with_warning(self) -> None:
        # Flows that complete with NO form step (instant creates — the mock
        # shape used across test_helper_update_persistence, and real
        # confirm-less integrations) had no form for the keys to match, so
        # the established success + ignored-keys warning contract holds.
        result = await _handle_flow_steps(
            client=None,
            flow_id="flow-instant",
            initial_step={
                "type": "create_entry",
                "flow_id": "flow-instant",
                "result": {"entry_id": "e1"},
            },
            config={"name": "x", "source": "sensor.a"},
            submit_fn=AsyncMock(),
        )

        assert result["success"] is True
        assert result["warnings"] == [
            "Ignored config keys not declared by the Home Assistant flow "
            "schema: name, source"
        ]
