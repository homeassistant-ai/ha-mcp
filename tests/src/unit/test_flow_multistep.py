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

from ha_mcp.tools.tools_config_entry_flow import (
    _extract_schema_field_names,
    _handle_flow_steps,
    _handle_form_step,
)


class TestExtractSchemaFieldNames:
    """Sanity-check the schema parser used to drive per-step key filtering."""

    def test_extracts_names_from_dict_list(self) -> None:
        schema = [
            {"name": "name", "required": True, "selector": {"text": {}}},
            {"name": "entity_id", "selector": {"entity": {}}},
        ]
        assert _extract_schema_field_names(schema) == {"name", "entity_id"}

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

    async def test_extra_unknown_keys_are_dropped(self) -> None:
        """Keys never declared by any step are silently dropped (HA will ignore)."""
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

        await _handle_flow_steps(
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


