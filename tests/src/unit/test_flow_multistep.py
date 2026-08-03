"""
Unit tests for multi-step config flow handling (Bug #18 / issue #1150).

Verifies that ``_handle_flow_steps`` correctly walks a multi-step HA config
flow, submitting only the keys declared in each step's ``data_schema`` and
preserving the remaining keys for subsequent steps.

Regression guard: prior code wiped ``remaining_config`` after the first form
step, which made step 2+ submit ``{}`` and HA respond with HTTP 400. This
broke ``statistics`` (multi-step user → pick-characteristic) and
``utility_meter`` UPDATE.

``TestRedeclaredFieldReuse`` covers the other half of that split (issue
#2057): a key the caller supplies once, consumed by an early step and then
declared again by a later step, is resubmitted from a scoped record — but only
where the later step marks it required and supplies neither a default nor a
value of its own, only once per step, and never into an optional field or a
section the caller never named.
"""

import copy
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.client.rest_client import HomeAssistantAPIError
from ha_mcp.tools.config_entry_flow import (
    _extract_schema_field_names,
    _handle_config_subentry_flow_steps,
    _handle_flow_steps,
    _handle_form_step,
    _ReuseState,
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

    def test_required_section_uses_all_schema_default_sources(self) -> None:
        remaining: dict[str, Any] = {}
        step = {
            "type": "form",
            "step_id": "user",
            "data_schema": [
                {
                    "type": "expandable",
                    "name": "advanced",
                    "required": True,
                    "schema": [
                        {
                            "name": "from_description",
                            "description": {"suggested_value": 2},
                        },
                        {"name": "from_top_level", "suggested_value": "tcp"},
                        {"name": "from_default", "default": True},
                    ],
                },
            ],
        }

        form_data = _handle_form_step("flow-1", step, remaining)

        assert form_data == {
            "advanced": {
                "from_description": 2,
                "from_top_level": "tcp",
                "from_default": True,
            }
        }
        assert remaining == {}

    def test_null_suggested_values_fall_back_to_defaults(self) -> None:
        remaining: dict[str, Any] = {}
        step = {
            "type": "form",
            "step_id": "user",
            "data_schema": [
                {
                    "type": "expandable",
                    "name": "advanced",
                    "required": True,
                    "schema": [
                        {
                            "name": "description_null",
                            "description": {"suggested_value": None},
                            "default": "fallback-a",
                        },
                        {
                            "name": "top_level_null",
                            "suggested_value": None,
                            "default": "fallback-b",
                        },
                    ],
                },
            ],
        }

        form_data = _handle_form_step("flow-1", step, remaining)

        assert form_data == {
            "advanced": {
                "description_null": "fallback-a",
                "top_level_null": "fallback-b",
            }
        }
        assert remaining == {}

    def test_optional_expandable_section_does_not_seed_suggestions(self) -> None:
        remaining = {"stream_source": "rtsp://camera.example/stream"}
        step = {
            "type": "form",
            "step_id": "user",
            "data_schema": [
                {"name": "stream_source", "required": False},
                {
                    "type": "expandable",
                    "name": "advanced",
                    "required": False,
                    "schema": [
                        {"name": "framerate", "description": {"suggested_value": 2}},
                    ],
                },
            ],
        }

        form_data = _handle_form_step("flow-1", step, remaining)

        assert form_data == {"stream_source": "rtsp://camera.example/stream"}
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

    def test_passes_through_falsy_non_dict_explicit_section_values(self) -> None:
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
        for value in (0, False, "", []):
            remaining = {"advanced_options": value}
            form_data = _handle_form_step("flow-1", step, remaining)
            assert form_data == {"advanced_options": value}
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
        # The warning names the value too — an un-consumed selection means
        # that branch was never configured.
        assert result["warnings"] == [
            "Ignored menu selection key(s) with no matching menu step: "
            "next_step_id='conversation'"
        ]


def _reuse_warning(dotted: str, step_id: str) -> str:
    """The warning a resubmitted key adds to the walk's success response."""
    return (
        f"Resubmitted '{dotted}' at step '{step_id}': supplied once "
        "but requested by more than one step encounter in this flow "
        "(a later step redeclaring the field, or the same step revisited "
        "via a menu loop — per-visit values cannot be expressed)"
    )


def _later_step(redeclared_field: dict[str, Any]) -> dict[str, Any]:
    """Build a second step declaring its own ``id`` key plus ``redeclared_field``."""
    return {
        "type": "form",
        "flow_id": "flow-2057",
        "step_id": "details",
        "data_schema": [
            {"name": "id", "required": True},
            redeclared_field,
        ],
    }


def _first_step() -> dict[str, Any]:
    """Build a first step that consumes ``friendly_name`` and ``host``."""
    return {
        "type": "form",
        "flow_id": "flow-2057",
        "step_id": "user",
        "data_schema": [
            {"name": "friendly_name", "required": True, "default": ""},
            {"name": "host", "required": True, "default": "1.2.3.4"},
        ],
    }


class TestRedeclaredFieldReuse:
    """A later step redeclaring a field an earlier step consumed (issue #2057).

    The first step pops the key out of ``remaining_config``, so a later step
    declaring the same name would be submitted without it and HA would answer
    "required key not provided". Resubmission from the record closes that gap,
    and is deliberately the last resort: a ``"default"`` key means voluptuous
    fills the value in, a suggested value or a constant means the step supplies
    its own, an optional field means nothing may be injected, and a section the
    caller never named must not be brought into existence. Where it does fire
    the response says so, and it fires at most once per step so a flow that
    re-presents a form cannot be rewritten indefinitely.
    """

    async def test_later_step_resubmits_required_field_with_no_default(self) -> None:
        final_entry: dict[str, Any] = {
            "type": "create_entry",
            "flow_id": "flow-2057",
            "result": {
                "entry_id": "entry-1",
                "title": "Device1",
                "domain": "demo",
            },
        }
        submit_fn = AsyncMock(
            side_effect=[
                _later_step({"name": "friendly_name", "required": True}),
                final_entry,
            ]
        )

        result = await _handle_flow_steps(
            client=None,
            flow_id="flow-2057",
            initial_step=_first_step(),
            config={"friendly_name": "Device1", "host": "10.0.0.5", "id": 20},
            submit_fn=submit_fn,
        )

        # The resubmission is reported: the caller wrote one value and two
        # steps were given it.
        assert result == {
            "success": True,
            "entry": final_entry,
            "warnings": [_reuse_warning("friendly_name", "details")],
        }
        assert submit_fn.await_args_list[0].args[1] == {
            "friendly_name": "Device1",
            "host": "10.0.0.5",
        }
        assert submit_fn.await_args_list[1].args[1] == {
            "id": 20,
            "friendly_name": "Device1",
        }

    @pytest.mark.parametrize("default_source", [{"default": ""}, {"default": None}])
    async def test_redeclared_field_with_a_default_is_not_resubmitted(
        self, default_source: dict[str, Any]
    ) -> None:
        """A ``"default"`` key means voluptuous fills the value in itself.

        Key presence is the whole test, so ``default: None`` counts: HA
        serializes a default only from an actual voluptuous default, and
        submitting an earlier step's value over one would change data the caller
        never named for this step.
        """
        final_entry: dict[str, Any] = {
            "type": "create_entry",
            "result": {"entry_id": "entry-2"},
        }
        submit_fn = AsyncMock(
            side_effect=[
                _later_step(
                    {"name": "friendly_name", "required": True, **default_source}
                ),
                final_entry,
            ]
        )

        result = await _handle_flow_steps(
            client=None,
            flow_id="flow-2057",
            initial_step=_first_step(),
            config={"friendly_name": "Device1", "host": "10.0.0.5", "id": 20},
            submit_fn=submit_fn,
        )

        assert submit_fn.await_args_list[1].args[1] == {"id": 20}
        assert "warnings" not in result

    @pytest.mark.parametrize(
        "suggestion_source",
        [
            {"description": {"suggested_value": "Existing entity name"}},
            {"suggested_value": "Existing entity name"},
        ],
    )
    async def test_redeclared_field_submits_the_steps_own_suggested_value(
        self, suggestion_source: dict[str, Any]
    ) -> None:
        """HA's edit-style pre-fill is the step's own data, and it is submitted.

        ``add_suggested_values_to_schema`` puts the current value in
        ``description.suggested_value``; with no voluptuous default on the
        marker, omitting the key would fail validation, while resubmitting the
        caller's value would overwrite the value being edited. The bare
        top-level ``suggested_value`` shape is read defensively —
        ``voluptuous_serialize`` never emits it — and behaves identically.
        """
        final_entry: dict[str, Any] = {
            "type": "create_entry",
            "result": {"entry_id": "entry-2"},
        }
        submit_fn = AsyncMock(
            side_effect=[
                _later_step(
                    {"name": "friendly_name", "required": True, **suggestion_source}
                ),
                final_entry,
            ]
        )

        result = await _handle_flow_steps(
            client=None,
            flow_id="flow-2057",
            initial_step=_first_step(),
            config={"friendly_name": "Device1", "host": "10.0.0.5", "id": 20},
            submit_fn=submit_fn,
        )

        assert submit_fn.await_args_list[1].args[1] == {
            "id": 20,
            "friendly_name": "Existing entity name",
        }
        # Schema data, not a caller key: nothing to report.
        assert "warnings" not in result

    async def test_suggestion_outranks_a_coexisting_static_default(self) -> None:
        """A marker keeps its voluptuous default when HA injects a suggestion.

        Both keys then serialize together: the default is the static schema
        value and the suggestion is the stored current one. The suggestion is
        submitted — omission would let voluptuous substitute the static value
        over the stored one.
        """
        final_entry: dict[str, Any] = {
            "type": "create_entry",
            "result": {"entry_id": "entry-2b"},
        }
        submit_fn = AsyncMock(
            side_effect=[
                _later_step(
                    {
                        "name": "friendly_name",
                        "required": True,
                        "default": 30,
                        "description": {"suggested_value": 300},
                    }
                ),
                final_entry,
            ]
        )

        result = await _handle_flow_steps(
            client=None,
            flow_id="flow-2057",
            initial_step=_first_step(),
            config={"friendly_name": "Device1", "host": "10.0.0.5", "id": 20},
            submit_fn=submit_fn,
        )

        assert submit_fn.await_args_list[1].args[1] == {
            "id": 20,
            "friendly_name": 300,
        }
        assert "warnings" not in result

    async def test_redeclared_constant_field_submits_its_only_legal_value(self) -> None:
        final_entry: dict[str, Any] = {
            "type": "create_entry",
            "result": {"entry_id": "entry-2b"},
        }
        submit_fn = AsyncMock(
            side_effect=[
                _later_step(
                    {
                        "name": "friendly_name",
                        "required": True,
                        "type": "constant",
                        "value": "LOCKED",
                    }
                ),
                final_entry,
            ]
        )

        result = await _handle_flow_steps(
            client=None,
            flow_id="flow-2057",
            initial_step=_first_step(),
            config={"friendly_name": "Device1", "host": "10.0.0.5", "id": 20},
            submit_fn=submit_fn,
        )

        assert submit_fn.await_args_list[1].args[1] == {
            "id": 20,
            "friendly_name": "LOCKED",
        }
        assert "warnings" not in result

    @pytest.mark.parametrize(
        "null_suggestion",
        [{"description": {"suggested_value": None}}, {"suggested_value": None}],
    )
    async def test_present_but_null_suggestion_falls_back_to_the_caller_value(
        self, null_suggestion: dict[str, Any]
    ) -> None:
        """A null suggestion is no value at all, so the caller's is the last resort."""
        final_entry: dict[str, Any] = {
            "type": "create_entry",
            "result": {"entry_id": "entry-2c"},
        }
        submit_fn = AsyncMock(
            side_effect=[
                _later_step(
                    {"name": "friendly_name", "required": True, **null_suggestion}
                ),
                final_entry,
            ]
        )

        result = await _handle_flow_steps(
            client=None,
            flow_id="flow-2057",
            initial_step=_first_step(),
            config={"friendly_name": "Device1", "host": "10.0.0.5", "id": 20},
            submit_fn=submit_fn,
        )

        assert submit_fn.await_args_list[1].args[1] == {
            "id": 20,
            "friendly_name": "Device1",
        }
        assert result["warnings"] == [_reuse_warning("friendly_name", "details")]

    @pytest.mark.parametrize("required_source", [{}, {"required": False}])
    async def test_redeclared_optional_field_is_not_resubmitted(
        self, required_source: dict[str, Any]
    ) -> None:
        final_entry: dict[str, Any] = {
            "type": "create_entry",
            "result": {"entry_id": "entry-3"},
        }
        submit_fn = AsyncMock(
            side_effect=[
                _later_step({"name": "friendly_name", **required_source}),
                final_entry,
            ]
        )

        await _handle_flow_steps(
            client=None,
            flow_id="flow-2057",
            initial_step=_first_step(),
            config={"friendly_name": "Device1", "host": "10.0.0.5", "id": 20},
            submit_fn=submit_fn,
        )

        assert submit_fn.await_args_list[1].args[1] == {"id": 20}

    async def test_resubmits_into_a_later_section_nested_field(self) -> None:
        final_entry: dict[str, Any] = {
            "type": "create_entry",
            "result": {"entry_id": "entry-4"},
        }
        later_step: dict[str, Any] = {
            "type": "form",
            "flow_id": "flow-2057",
            "step_id": "details",
            "data_schema": [
                {"name": "id", "required": True},
                {
                    "type": "expandable",
                    "name": "advanced",
                    "required": True,
                    "schema": [{"name": "friendly_name", "required": True}],
                },
            ],
        }
        submit_fn = AsyncMock(side_effect=[later_step, final_entry])

        result = await _handle_flow_steps(
            client=None,
            flow_id="flow-2057",
            initial_step=_first_step(),
            config={"friendly_name": "Device1", "host": "10.0.0.5", "id": 20},
            submit_fn=submit_fn,
        )

        assert submit_fn.await_args_list[1].args[1] == {
            "id": 20,
            "advanced": {"friendly_name": "Device1"},
        }
        # The warning names the dotted path the value landed on.
        assert result["warnings"] == [
            _reuse_warning("advanced.friendly_name", "details")
        ]

    async def test_resubmitted_value_is_a_copy(self) -> None:
        """Two submissions must not share a mutable value object."""
        final_entry: dict[str, Any] = {
            "type": "create_entry",
            "result": {"entry_id": "entry-5"},
        }
        later_step: dict[str, Any] = {
            "type": "form",
            "flow_id": "flow-2057",
            "step_id": "details",
            "data_schema": [{"name": "entity_ids", "required": True}],
        }
        submit_fn = AsyncMock(side_effect=[later_step, final_entry])
        initial_step: dict[str, Any] = {
            "type": "form",
            "flow_id": "flow-2057",
            "step_id": "user",
            "data_schema": [{"name": "entity_ids", "required": True, "default": []}],
        }

        await _handle_flow_steps(
            client=None,
            flow_id="flow-2057",
            initial_step=initial_step,
            config={"entity_ids": ["sensor.a"]},
            submit_fn=submit_fn,
        )

        first = submit_fn.await_args_list[0].args[1]["entity_ids"]
        second = submit_fn.await_args_list[1].args[1]["entity_ids"]
        assert second == ["sensor.a"]
        assert second is not first

    async def test_subentry_walker_resubmits_redeclared_required_field(self) -> None:
        final_entry: dict[str, Any] = {
            "type": "create_entry",
            "result": {"entry_id": "entry-6"},
        }
        client = AsyncMock()
        client.submit_config_subentry_flow_step = AsyncMock(
            side_effect=[
                _later_step({"name": "friendly_name", "required": True}),
                final_entry,
            ]
        )

        result = await _handle_config_subentry_flow_steps(
            client,
            "flow-2057-sub",
            _first_step(),
            {"friendly_name": "Device1", "host": "10.0.0.5", "id": 20},
            is_reconfigure=False,
        )

        assert result["operation"] == "created"
        # Parity with _handle_flow_steps: the subentry walker reports the
        # resubmission on its own success path.
        assert result["warnings"] == [_reuse_warning("friendly_name", "details")]
        submitted = client.submit_config_subentry_flow_step.await_args_list
        assert submitted[0].args[1] == {
            "friendly_name": "Device1",
            "host": "10.0.0.5",
        }
        assert submitted[1].args[1] == {"id": 20, "friendly_name": "Device1"}

    async def test_repeated_step_is_resubmitted_once_then_left_alone(self) -> None:
        """One reused write per step, however often the flow re-presents it.

        Iteration 1 submits the caller's own key. Iteration 2 resubmits it from
        the record, because a step asking again for a required field with no
        default cannot be answered with nothing. From iteration 3 the key is
        omitted, so a flow stuck on one form gets HA's loud "required key not
        provided" naming the field instead of a silent rewrite loop. Each
        payload is an independent object.
        """
        repeated: dict[str, Any] = {
            "type": "form",
            "flow_id": "flow-2057",
            "step_id": "user",
            "data_schema": [{"name": "entity_ids", "required": True}],
        }
        final_entry: dict[str, Any] = {
            "type": "create_entry",
            "result": {"entry_id": "entry-7"},
        }
        submit_fn = AsyncMock(
            side_effect=[repeated, repeated, repeated, final_entry],
        )

        result = await _handle_flow_steps(
            client=None,
            flow_id="flow-2057",
            initial_step=repeated,
            config={"entity_ids": ["sensor.a"]},
            submit_fn=submit_fn,
        )

        assert result["success"] is True
        assert result["warnings"] == [_reuse_warning("entity_ids", "user")]
        payloads = [call.args[1] for call in submit_fn.await_args_list]
        assert payloads == [
            {"entity_ids": ["sensor.a"]},
            {"entity_ids": ["sensor.a"]},
            {},
            {},
        ]
        first, reused = payloads[0]["entity_ids"], payloads[1]["entity_ids"]
        assert reused is not first

    async def test_walk_still_bounded_when_a_repeated_step_never_finishes(self) -> None:
        """A flow that keeps re-presenting the same form hits the step ceiling.

        The ceiling is what ends the walk; the fire-once bound is what keeps the
        run in between from writing the same value ten times.
        """
        import json

        from fastmcp.exceptions import ToolError

        repeated: dict[str, Any] = {
            "type": "form",
            "flow_id": "flow-2057",
            "step_id": "user",
            "data_schema": [{"name": "entity_ids", "required": True}],
        }
        submit_fn = AsyncMock(return_value=repeated)

        with pytest.raises(ToolError) as exc_info:
            await _handle_flow_steps(
                client=None,
                flow_id="flow-2057",
                initial_step=repeated,
                config={"entity_ids": ["sensor.a"]},
                submit_fn=submit_fn,
            )

        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "TIMEOUT_OPERATION"
        assert submit_fn.await_count == 10
        payloads = [call.args[1] for call in submit_fn.await_args_list]
        assert payloads[:2] == [{"entity_ids": ["sensor.a"]}] * 2
        assert payloads[2:] == [{}] * 8

    async def test_every_later_step_redeclaring_one_field_gets_its_own_write(
        self,
    ) -> None:
        """The bound is one write per step, not one per field for the whole flow.

        Three steps declare ``friendly_name`` and only the first gets the
        caller's key. Both steps after it must be answered: a bound spent at
        one of them would leave the next submitting nothing for a required
        field with no default, which is the "required key not provided" that
        resubmission exists to prevent. Each write names the step it happened
        at.
        """
        second_step: dict[str, Any] = {
            "type": "form",
            "flow_id": "flow-2057",
            "step_id": "second",
            "data_schema": [
                {"name": "id", "required": True},
                {"name": "friendly_name", "required": True},
            ],
        }
        third_step: dict[str, Any] = {
            "type": "form",
            "flow_id": "flow-2057",
            "step_id": "third",
            "data_schema": [{"name": "friendly_name", "required": True}],
        }
        final_entry: dict[str, Any] = {
            "type": "create_entry",
            "result": {"entry_id": "entry-8"},
        }
        submit_fn = AsyncMock(side_effect=[second_step, third_step, final_entry])

        result = await _handle_flow_steps(
            client=None,
            flow_id="flow-2057",
            initial_step=dict(_first_step(), step_id="first"),
            config={"friendly_name": "Device1", "host": "10.0.0.5", "id": 20},
            submit_fn=submit_fn,
        )

        payloads = [call.args[1] for call in submit_fn.await_args_list]
        assert payloads == [
            {"friendly_name": "Device1", "host": "10.0.0.5"},
            {"id": 20, "friendly_name": "Device1"},
            {"friendly_name": "Device1"},
        ]
        assert result["warnings"] == [
            _reuse_warning("friendly_name", "second"),
            _reuse_warning("friendly_name", "third"),
        ]


class TestReuseScoping:
    """Where a recorded value may resurface, and where it must not."""

    @staticmethod
    def _named_section_step(step_id: str, section: str) -> dict[str, Any]:
        return {
            "type": "form",
            "flow_id": "flow-scope",
            "step_id": step_id,
            "data_schema": [
                {"name": "id", "required": True},
                {
                    "type": "expandable",
                    "name": section,
                    "required": True,
                    "schema": [{"name": "name", "required": True}],
                },
            ],
        }

    async def test_value_from_an_explicit_section_is_not_reused_for_a_sibling(
        self,
    ) -> None:
        """``{"left": {"name": ...}}`` names one section, so it fills only that one."""
        final_entry: dict[str, Any] = {
            "type": "create_entry",
            "result": {"entry_id": "entry-scope-1"},
        }
        first_step: dict[str, Any] = {
            "type": "form",
            "flow_id": "flow-scope",
            "step_id": "left_step",
            "data_schema": [
                {
                    "type": "expandable",
                    "name": "left",
                    "required": True,
                    "schema": [{"name": "name", "required": True}],
                }
            ],
        }
        submit_fn = AsyncMock(
            side_effect=[self._named_section_step("right_step", "right"), final_entry]
        )

        result = await _handle_flow_steps(
            client=None,
            flow_id="flow-scope",
            initial_step=first_step,
            config={"left": {"name": "LEFT"}, "id": 20},
            submit_fn=submit_fn,
        )

        assert submit_fn.await_args_list[0].args[1] == {"left": {"name": "LEFT"}}
        # right.name is required with no default, but nothing the caller wrote
        # belongs there — HA's own error is the right answer, not "LEFT".
        assert submit_fn.await_args_list[1].args[1] == {"id": 20}
        assert "warnings" not in result

    async def test_flat_value_is_reused_inside_a_later_section(self) -> None:
        """A flat key names no section, so it fills the leaf wherever it is declared."""
        final_entry: dict[str, Any] = {
            "type": "create_entry",
            "result": {"entry_id": "entry-scope-2"},
        }
        first_step: dict[str, Any] = {
            "type": "form",
            "flow_id": "flow-scope",
            "step_id": "name_step",
            "data_schema": [{"name": "name", "required": True}],
        }
        submit_fn = AsyncMock(
            side_effect=[self._named_section_step("right_step", "right"), final_entry]
        )

        result = await _handle_flow_steps(
            client=None,
            flow_id="flow-scope",
            initial_step=first_step,
            config={"name": "FLAT", "id": 20},
            submit_fn=submit_fn,
        )

        assert submit_fn.await_args_list[1].args[1] == {
            "id": 20,
            "right": {"name": "FLAT"},
        }
        assert result["warnings"] == [_reuse_warning("right.name", "right_step")]

    async def test_flat_override_wins_over_stale_scoped_record_on_reuse(self) -> None:
        """The record carries the value actually submitted for a path.

        A flat key overrides an explicit section value at the step declaring
        both (see ``test_flat_section_field_overrides_explicit_section_value``),
        so a later redeclaration of that path must resubmit the override, not
        the overridden section value.
        """
        final_entry: dict[str, Any] = {
            "type": "create_entry",
            "result": {"entry_id": "entry-scope-3"},
        }
        first_step: dict[str, Any] = {
            "type": "form",
            "flow_id": "flow-scope",
            "step_id": "advanced_step",
            "data_schema": [
                {
                    "type": "expandable",
                    "name": "advanced",
                    "required": True,
                    "schema": [{"name": "name", "required": True}],
                }
            ],
        }
        submit_fn = AsyncMock(
            side_effect=[
                self._named_section_step("later_step", "advanced"),
                final_entry,
            ]
        )

        result = await _handle_flow_steps(
            client=None,
            flow_id="flow-scope",
            initial_step=first_step,
            config={"advanced": {"name": "SCOPED"}, "name": "FLAT", "id": 20},
            submit_fn=submit_fn,
        )

        assert submit_fn.await_args_list[0].args[1] == {"advanced": {"name": "FLAT"}}
        assert submit_fn.await_args_list[1].args[1] == {
            "id": 20,
            "advanced": {"name": "FLAT"},
        }
        assert result["warnings"] == [_reuse_warning("advanced.name", "later_step")]

    async def test_scoped_record_outranks_a_flat_one_for_the_same_leaf(self) -> None:
        """Both records hold ``label``; a sectioned redeclaration takes the scoped one.

        Step order is load-bearing. The step declaring ``label`` flat has to
        run first: with the section step first, its explicit
        ``{"left": {"label": ...}}`` would still be sitting beside an
        unconsumed flat ``label``, and a flat child overrides the section value
        it duplicates — the scoped record would be rewritten to the flat value
        (see ``test_flat_override_wins_over_stale_scoped_record_on_reuse``) and
        both lookups would then answer the same thing.
        """
        flat_step: dict[str, Any] = {
            "type": "form",
            "flow_id": "flow-scope",
            "step_id": "flat_step",
            "data_schema": [{"name": "label", "required": True}],
        }
        section_step: dict[str, Any] = {
            "type": "form",
            "flow_id": "flow-scope",
            "step_id": "section_step",
            "data_schema": [
                {
                    "type": "expandable",
                    "name": "left",
                    "required": True,
                    "schema": [{"name": "label", "required": True}],
                }
            ],
        }
        redeclaring_step: dict[str, Any] = {
            "type": "form",
            "flow_id": "flow-scope",
            "step_id": "redeclaring_step",
            "data_schema": [
                {"name": "id", "required": True},
                {
                    "type": "expandable",
                    "name": "left",
                    "required": True,
                    "schema": [{"name": "label", "required": True}],
                },
            ],
        }
        final_entry: dict[str, Any] = {
            "type": "create_entry",
            "result": {"entry_id": "entry-scope-9"},
        }
        submit_fn = AsyncMock(side_effect=[section_step, redeclaring_step, final_entry])

        result = await _handle_flow_steps(
            client=None,
            flow_id="flow-scope",
            initial_step=flat_step,
            config={"label": "FLAT", "left": {"label": "SCOPED"}, "id": 20},
            submit_fn=submit_fn,
        )

        payloads = [call.args[1] for call in submit_fn.await_args_list]
        assert payloads == [
            {"label": "FLAT"},
            {"left": {"label": "SCOPED"}},
            {"id": 20, "left": {"label": "SCOPED"}},
        ]
        assert result["warnings"] == [_reuse_warning("left.label", "redeclaring_step")]

    async def test_untouched_optional_section_is_not_materialized(self) -> None:
        """Reuse never invents a section the caller did not name."""
        final_entry: dict[str, Any] = {
            "type": "create_entry",
            "result": {"entry_id": "entry-scope-3"},
        }
        later_step: dict[str, Any] = {
            "type": "form",
            "flow_id": "flow-2057",
            "step_id": "details",
            "data_schema": [
                {"name": "id", "required": True},
                {
                    "type": "expandable",
                    "name": "advanced",
                    "required": False,
                    "schema": [{"name": "friendly_name", "required": True}],
                },
            ],
        }
        submit_fn = AsyncMock(side_effect=[later_step, final_entry])

        result = await _handle_flow_steps(
            client=None,
            flow_id="flow-2057",
            initial_step=_first_step(),
            config={"friendly_name": "Device1", "host": "10.0.0.5", "id": 20},
            submit_fn=submit_fn,
        )

        assert submit_fn.await_args_list[1].args[1] == {"id": 20}
        assert "warnings" not in result

    async def test_explicitly_supplied_optional_section_allows_reuse(self) -> None:
        """Naming the section is consent to fill the rest of what it requires."""
        final_entry: dict[str, Any] = {
            "type": "create_entry",
            "result": {"entry_id": "entry-scope-4"},
        }
        later_step: dict[str, Any] = {
            "type": "form",
            "flow_id": "flow-2057",
            "step_id": "details",
            "data_schema": [
                {"name": "id", "required": True},
                {
                    "type": "expandable",
                    "name": "advanced",
                    "required": False,
                    "schema": [
                        {"name": "friendly_name", "required": True},
                        {"name": "extra"},
                    ],
                },
            ],
        }
        submit_fn = AsyncMock(side_effect=[later_step, final_entry])

        result = await _handle_flow_steps(
            client=None,
            flow_id="flow-2057",
            initial_step=_first_step(),
            config={
                "friendly_name": "Device1",
                "host": "10.0.0.5",
                "id": 20,
                "advanced": {"extra": 7},
            },
            submit_fn=submit_fn,
        )

        assert submit_fn.await_args_list[1].args[1] == {
            "id": 20,
            "advanced": {"extra": 7, "friendly_name": "Device1"},
        }
        assert result["warnings"] == [
            _reuse_warning("advanced.friendly_name", "details")
        ]

    def test_one_step_declaring_a_leaf_twice_fills_both(self) -> None:
        """Flat and sectioned declarations of one name in a single step."""
        state = _ReuseState()
        step: dict[str, Any] = {
            "type": "form",
            "step_id": "user",
            "data_schema": [
                {"name": "name", "required": True},
                {
                    "type": "expandable",
                    "name": "advanced",
                    "required": True,
                    "schema": [{"name": "name", "required": True}],
                },
            ],
        }
        remaining = {"name": "X"}

        form_data = _handle_form_step("flow-1", step, remaining, reuse_state=state)

        assert form_data == {"name": "X", "advanced": {"name": "X"}}
        assert remaining == {}
        assert state.notes == [_reuse_warning("advanced.name", "user")]

    async def test_legacy_schemaless_step_records_what_it_dumped(self) -> None:
        """A step HA sent no schema for still feeds later schema'd steps."""
        final_entry: dict[str, Any] = {
            "type": "create_entry",
            "result": {"entry_id": "entry-scope-5"},
        }
        first_step: dict[str, Any] = {
            "type": "form",
            "flow_id": "flow-2057",
            "step_id": "user",
            "data_schema": None,
        }
        submit_fn = AsyncMock(
            side_effect=[
                _later_step({"name": "friendly_name", "required": True}),
                final_entry,
            ]
        )

        result = await _handle_flow_steps(
            client=None,
            flow_id="flow-2057",
            initial_step=first_step,
            config={"friendly_name": "Device1", "host": "10.0.0.5", "id": 20},
            submit_fn=submit_fn,
        )

        assert submit_fn.await_args_list[0].args[1] == {
            "friendly_name": "Device1",
            "host": "10.0.0.5",
            "id": 20,
        }
        # Both of step 2's required no-default fields came out of the dump.
        assert submit_fn.await_args_list[1].args[1] == {
            "id": 20,
            "friendly_name": "Device1",
        }
        assert result["warnings"] == [
            _reuse_warning("id", "details"),
            _reuse_warning("friendly_name", "details"),
        ]

    async def test_injected_section_defaults_are_not_recorded_for_reuse(self) -> None:
        """Mirror of the consumed-keys rule: HA's own defaults are not caller values."""
        final_entry: dict[str, Any] = {
            "type": "create_entry",
            "result": {"entry_id": "entry-scope-6"},
        }
        first_step: dict[str, Any] = {
            "type": "form",
            "flow_id": "flow-2057",
            "step_id": "user",
            "data_schema": [
                {"name": "host", "required": True},
                {
                    "type": "expandable",
                    "name": "advanced",
                    "required": True,
                    "schema": [{"name": "framerate", "default": 2}],
                },
            ],
        }
        later_step: dict[str, Any] = {
            "type": "form",
            "flow_id": "flow-2057",
            "step_id": "details",
            "data_schema": [{"name": "framerate", "required": True}],
        }
        submit_fn = AsyncMock(side_effect=[later_step, final_entry])

        result = await _handle_flow_steps(
            client=None,
            flow_id="flow-2057",
            initial_step=first_step,
            config={"host": "1.2.3.4"},
            submit_fn=submit_fn,
        )

        assert submit_fn.await_args_list[0].args[1] == {
            "host": "1.2.3.4",
            "advanced": {"framerate": 2},
        }
        assert submit_fn.await_args_list[1].args[1] == {}
        assert "warnings" not in result

    async def test_menu_selection_key_is_never_reused_by_a_form_step(self) -> None:
        final_entry: dict[str, Any] = {
            "type": "create_entry",
            "result": {"entry_id": "entry-scope-7"},
        }
        later_step: dict[str, Any] = {
            "type": "form",
            "flow_id": "flow-menu",
            "step_id": "options",
            "data_schema": [
                {"name": "name", "required": True},
                {"name": "group_type", "required": True},
            ],
        }
        submit_fn = AsyncMock(side_effect=[later_step, final_entry])

        result = await _handle_flow_steps(
            client=None,
            flow_id="flow-menu",
            initial_step={
                "type": "menu",
                "flow_id": "flow-menu",
                "step_id": "user",
                "menu_options": ["light", "switch"],
            },
            config={"group_type": "light", "name": "x"},
            submit_fn=submit_fn,
        )

        assert submit_fn.await_args_list[0].args[1] == {"next_step_id": "light"}
        assert submit_fn.await_args_list[1].args[1] == {"name": "x"}
        assert "warnings" not in result

    async def test_recorded_value_is_snapshotted_at_record_time(self) -> None:
        """Mutating the caller's list after step 1 cannot reach step 2's payload."""
        config: dict[str, Any] = {"entity_ids": ["sensor.a"]}
        step: dict[str, Any] = {
            "type": "form",
            "flow_id": "flow-2057",
            "step_id": "user",
            "data_schema": [{"name": "entity_ids", "required": True}],
        }
        later_step = dict(step, step_id="details")
        final_entry: dict[str, Any] = {
            "type": "create_entry",
            "result": {"entry_id": "entry-scope-8"},
        }
        payloads: list[dict[str, Any]] = []

        def submit(flow_id: str, payload: dict[str, Any]) -> dict[str, Any]:
            payloads.append(copy.deepcopy(payload))
            config["entity_ids"].append("sensor.MUTATED")
            return later_step if len(payloads) == 1 else final_entry

        await _handle_flow_steps(
            client=None,
            flow_id="flow-2057",
            initial_step=step,
            config=config,
            submit_fn=AsyncMock(side_effect=submit),
        )

        assert config["entity_ids"] == ["sensor.a", "sensor.MUTATED", "sensor.MUTATED"]
        assert payloads == [{"entity_ids": ["sensor.a"]}, {"entity_ids": ["sensor.a"]}]


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

    async def test_seeded_section_defaults_do_not_count_as_consumed_keys(self) -> None:
        import json

        from fastmcp.exceptions import ToolError

        final_entry = {"type": "create_entry", "result": {"entry_id": "e1"}}
        submit_fn = AsyncMock(side_effect=[final_entry])
        initial_step = {
            "type": "form",
            "flow_id": "flow-defaults",
            "step_id": "user",
            "data_schema": [
                {
                    "type": "expandable",
                    "name": "advanced",
                    "required": True,
                    "schema": [{"name": "framerate", "default": 2}],
                }
            ],
        }

        with pytest.raises(ToolError) as exc_info:
            await _handle_flow_steps(
                client=None,
                flow_id="flow-defaults",
                initial_step=initial_step,
                config={"typo_key": 5},
                submit_fn=submit_fn,
            )

        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "without consuming any" in body["error"]["message"]
        assert submit_fn.await_args.args[1] == {"advanced": {"framerate": 2}}

    async def test_step_owned_submission_does_not_count_as_consumed_keys(self) -> None:
        """A step's own suggestion fills a form but applies nothing the caller asked for."""
        import json

        from fastmcp.exceptions import ToolError

        final_entry = {"type": "create_entry", "result": {"entry_id": "e1"}}
        later_step = {
            "type": "form",
            "flow_id": "flow-typo",
            "step_id": "details",
            "data_schema": [
                {
                    "name": "friendly_name",
                    "required": True,
                    "description": {"suggested_value": "HA name"},
                }
            ],
        }
        submit_fn = AsyncMock(side_effect=[later_step, final_entry])
        initial_step = {
            "type": "form",
            "flow_id": "flow-typo",
            "step_id": "user",
            "data_schema": [{"name": "entities"}],
        }

        with pytest.raises(ToolError) as exc_info:
            await _handle_flow_steps(
                client=None,
                flow_id="flow-typo",
                initial_step=initial_step,
                config={"typo_key": 5},
                submit_fn=submit_fn,
            )

        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "without consuming any" in body["error"]["message"]
        payloads = [call.args[1] for call in submit_fn.await_args_list]
        assert payloads == [{}, {"friendly_name": "HA name"}]

    async def test_preview_confirm_form_is_auto_advanced(self) -> None:
        confirm_step = {
            "type": "form",
            "flow_id": "flow-generic",
            "step_id": "user_confirm",
            "preview": "Camera preview",
            "data_schema": [
                {
                    "name": "confirmed_ok",
                    "required": True,
                    "default": False,
                    "selector": {"boolean": {}},
                }
            ],
        }
        final_entry = {"type": "create_entry", "result": {"entry_id": "camera-1"}}
        submit_fn = AsyncMock(side_effect=[confirm_step, final_entry])
        initial_step = {
            "type": "form",
            "flow_id": "flow-generic",
            "step_id": "user",
            "data_schema": [{"name": "stream_source"}],
        }

        result = await _handle_flow_steps(
            client=None,
            flow_id="flow-generic",
            initial_step=initial_step,
            config={"stream_source": "rtsp://camera.example/stream"},
            submit_fn=submit_fn,
        )

        assert result["success"] is True
        assert submit_fn.await_args_list[0].args[1] == {
            "stream_source": "rtsp://camera.example/stream"
        }
        assert submit_fn.await_args_list[1].args[1] == {"confirmed_ok": True}

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


def _cyclic_menu_step() -> dict[str, Any]:
    """The battery_sim options-flow menu (issue #2116): re-shown after every branch."""
    return {
        "type": "menu",
        "flow_id": "flow-2116",
        "step_id": "init",
        "menu_options": [
            "main_params",
            "input_sensors",
            "delete_leftover_entities",
            "all_done",
        ],
    }


def _main_params_form() -> dict[str, Any]:
    return {
        "type": "form",
        "flow_id": "flow-2116",
        "step_id": "main_params",
        "data_schema": [
            {
                "name": "charge_efficiency",
                "required": True,
                "description": {"suggested_value": 0.85},
            },
            {
                "name": "discharge_efficiency",
                "required": True,
                "description": {"suggested_value": 0.85},
            },
        ],
    }


class TestCyclicMenuFlows:
    """Flows that revisit a menu step (issue #2116).

    battery_sim's options flow loops menu → branch form → menu until
    'all_done' is chosen. A single menu selection key is consumed by the
    first menu, so the walker previously raised a misleading "Menu step
    requires a selection" on the revisit — with the caller's selection
    already forwarded. Menu selection keys now accept a list of successive
    selections, consumed one per menu encounter.
    """

    async def test_selection_list_drives_successive_menus(self) -> None:
        final_entry = {
            "type": "create_entry",
            "flow_id": "flow-2116",
            "result": {"entry_id": "e-batt", "title": "batt", "domain": "battery_sim"},
        }
        submit_fn = AsyncMock(
            side_effect=[_main_params_form(), _cyclic_menu_step(), final_entry]
        )

        result = await _handle_flow_steps(
            client=None,
            flow_id="flow-2116",
            initial_step=_cyclic_menu_step(),
            config={
                "next_step_id": ["main_params", "all_done"],
                "charge_efficiency": 0.92,
                "discharge_efficiency": 0.90,
            },
            submit_fn=submit_fn,
        )

        assert result["success"] is True
        submissions = [c.args[1] for c in submit_fn.await_args_list]
        assert submissions == [
            {"next_step_id": "main_params"},
            {"charge_efficiency": 0.92, "discharge_efficiency": 0.90},
            {"next_step_id": "all_done"},
        ]
        assert "warnings" not in result

    async def test_selection_list_does_not_mutate_caller_config(self) -> None:
        final_entry = {"type": "create_entry", "result": {"entry_id": "e1"}}
        submit_fn = AsyncMock(
            side_effect=[_main_params_form(), _cyclic_menu_step(), final_entry]
        )
        config = {
            "next_step_id": ["main_params", "all_done"],
            "charge_efficiency": 0.92,
            "discharge_efficiency": 0.90,
        }

        await _handle_flow_steps(
            client=None,
            flow_id="flow-2116",
            initial_step=_cyclic_menu_step(),
            config=config,
            submit_fn=submit_fn,
        )

        assert config["next_step_id"] == ["main_params", "all_done"]

    async def test_re_encountered_menu_error_explains_list_syntax(self) -> None:
        """The revisit error must not claim no selection was supplied."""
        import json

        from fastmcp.exceptions import ToolError

        submit_fn = AsyncMock(side_effect=[_main_params_form(), _cyclic_menu_step()])

        with pytest.raises(ToolError) as exc_info:
            await _handle_flow_steps(
                client=None,
                flow_id="flow-2116",
                initial_step=_cyclic_menu_step(),
                config={"next_step_id": "main_params"},
                submit_fn=submit_fn,
            )

        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "CONFIG_MISSING_REQUIRED_FIELDS"
        assert "list of successive" in body["error"]["message"]
        assert "main_params" in body["error"]["message"]
        assert body["consumed_menu_selections"] == ["main_params"]
        assert body["menu_options"] == _cyclic_menu_step()["menu_options"]
        # The example continues from what was consumed and ends in an
        # explicit placeholder — cyclic flows legitimately repeat options,
        # so no concrete "next" can be suggested without guessing.
        assert (
            'Example: {"next_step_id": ["main_params", "<next-selection>"]}'
            in body["error"]["suggestions"]
        )

    async def test_scalar_selection_on_linear_flow_unchanged(self) -> None:
        """A menu-rooted flow that ends after one branch keeps working."""
        final_entry = {"type": "create_entry", "result": {"entry_id": "e1"}}
        submit_fn = AsyncMock(side_effect=[_main_params_form(), final_entry])

        result = await _handle_flow_steps(
            client=None,
            flow_id="flow-2116",
            initial_step=_cyclic_menu_step(),
            config={
                "next_step_id": "main_params",
                "charge_efficiency": 0.92,
                "discharge_efficiency": 0.90,
            },
            submit_fn=submit_fn,
        )

        assert result["success"] is True
        assert submit_fn.await_args_list[0].args[1] == {"next_step_id": "main_params"}
        assert "warnings" not in result

    async def test_first_menu_without_selection_keeps_original_error(self) -> None:
        import json

        from fastmcp.exceptions import ToolError

        with pytest.raises(ToolError) as exc_info:
            await _handle_flow_steps(
                client=None,
                flow_id="flow-2116",
                initial_step=_cyclic_menu_step(),
                config={},
                submit_fn=AsyncMock(),
            )

        body = json.loads(str(exc_info.value))
        assert body["error"]["message"].startswith("Menu step requires a selection")

    async def test_empty_selection_list_is_treated_as_missing(self) -> None:
        import json

        from fastmcp.exceptions import ToolError

        with pytest.raises(ToolError) as exc_info:
            await _handle_flow_steps(
                client=None,
                flow_id="flow-2116",
                initial_step=_cyclic_menu_step(),
                config={"next_step_id": []},
                submit_fn=AsyncMock(),
            )

        body = json.loads(str(exc_info.value))
        assert body["error"]["message"].startswith("Menu step requires a selection")

    async def test_unused_selection_list_items_warn(self) -> None:
        """Selections beyond the menus actually shown surface as a warning."""
        final_entry = {"type": "create_entry", "result": {"entry_id": "e1"}}
        submit_fn = AsyncMock(side_effect=[_main_params_form(), final_entry])

        result = await _handle_flow_steps(
            client=None,
            flow_id="flow-2116",
            initial_step=_cyclic_menu_step(),
            config={
                "next_step_id": ["main_params", "all_done"],
                "charge_efficiency": 0.92,
                "discharge_efficiency": 0.90,
            },
            submit_fn=submit_fn,
        )

        assert result["success"] is True
        # The warning must name the dropped selection values — those branches
        # were never configured, which the key name alone would hide.
        assert any(
            "no matching menu step" in w and "next_step_id=['all_done']" in w
            for w in result["warnings"]
        )

    async def test_subentry_walker_accepts_selection_list(self) -> None:
        """MQTT device-subentry reconfigure loops through summary_menu."""
        summary_menu = {
            "type": "menu",
            "flow_id": "flow-sub-2116",
            "step_id": "summary_menu",
            "menu_options": ["entity", "update_entity", "device", "save_changes"],
        }
        device_form = {
            "type": "form",
            "flow_id": "flow-sub-2116",
            "step_id": "device",
            "data_schema": [{"name": "model"}],
        }
        done = {"type": "abort", "reason": "reconfigure_successful"}
        client = AsyncMock()
        client.submit_config_subentry_flow_step = AsyncMock(
            side_effect=[device_form, summary_menu, done]
        )

        result = await _handle_config_subentry_flow_steps(
            client,
            "flow-sub-2116",
            summary_menu,
            {"next_step_id": ["device", "save_changes"], "model": "M1"},
            is_reconfigure=True,
        )

        assert result["operation"] == "reconfigured"
        submissions = [
            c.args[1] for c in client.submit_config_subentry_flow_step.await_args_list
        ]
        assert submissions == [
            {"next_step_id": "device"},
            {"model": "M1"},
            {"next_step_id": "save_changes"},
        ]

    async def test_long_selection_list_extends_the_step_budget(self) -> None:
        """Six menu cycles cost more than the historical 10-step cap.

        The budget scales with the caller's selection count (2 steps per
        selection), so a legitimate long cyclic walk completes instead of
        raising TIMEOUT_OPERATION mid-flow.
        """
        selections = [f"branch_{i}" for i in range(6)] + ["all_done"]
        form = {
            "type": "form",
            "flow_id": "flow-2116",
            "step_id": "branch",
            "data_schema": [],
        }
        final_entry = {"type": "create_entry", "result": {"entry_id": "e1"}}
        # menu(initial) -> [form -> menu] x6 -> create_entry: 13 walker steps.
        side_effects: list[dict[str, Any]] = []
        for _ in range(6):
            side_effects.append(dict(form))
            side_effects.append(_cyclic_menu_step())
        side_effects.append(final_entry)
        submit_fn = AsyncMock(side_effect=side_effects)

        result = await _handle_flow_steps(
            client=None,
            flow_id="flow-2116",
            initial_step=_cyclic_menu_step(),
            config={"next_step_id": selections},
            submit_fn=submit_fn,
        )

        assert result["success"] is True
        assert submit_fn.await_count == 13

    async def test_multi_key_precedence_is_deterministic(self) -> None:
        """group_type outranks next_step_id at a menu, per the shared
        canonical order (_MENU_SELECTION_KEY_ORDER) — a config carrying two
        selection keys must not consume them in per-process hash order.
        """
        final_entry = {"type": "create_entry", "result": {"entry_id": "e1"}}
        submit_fn = AsyncMock(side_effect=[_main_params_form(), final_entry])

        result = await _handle_flow_steps(
            client=None,
            flow_id="flow-2116",
            initial_step=_cyclic_menu_step(),
            config={
                "group_type": "main_params",
                "next_step_id": "input_sensors",
                "charge_efficiency": 0.92,
                "discharge_efficiency": 0.90,
            },
            submit_fn=submit_fn,
        )

        assert submit_fn.await_args_list[0].args[1] == {"next_step_id": "main_params"}
        assert result["success"] is True
        # The unconsumed next_step_id surfaces through the leftover warning.
        assert any("next_step_id" in w for w in result["warnings"])

    async def test_falsy_list_element_raises_distinct_validation_error(self) -> None:
        """A falsy element is a malformed call, not an exhausted list."""
        import json

        from fastmcp.exceptions import ToolError

        with pytest.raises(ToolError) as exc_info:
            await _handle_flow_steps(
                client=None,
                flow_id="flow-2116",
                initial_step=_cyclic_menu_step(),
                config={"next_step_id": ["", "all_done"]},
                submit_fn=AsyncMock(),
            )

        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "empty element" in body["error"]["message"]
        # It must NOT claim no selection was supplied — that is the exact
        # misdiagnosis issue #2116 was filed about.
        assert "Menu step requires a selection" not in body["error"]["message"]

    async def test_falsy_element_midlist_is_not_reported_as_exhaustion(self) -> None:
        import json

        from fastmcp.exceptions import ToolError

        submit_fn = AsyncMock(side_effect=[_main_params_form(), _cyclic_menu_step()])

        with pytest.raises(ToolError) as exc_info:
            await _handle_flow_steps(
                client=None,
                flow_id="flow-2116",
                initial_step=_cyclic_menu_step(),
                config={"next_step_id": ["main_params", None, "all_done"]},
                submit_fn=submit_fn,
            )

        body = json.loads(str(exc_info.value))
        assert "empty element" in body["error"]["message"]
        assert "were consumed" not in body["error"]["message"]

    async def test_nonstring_list_element_is_coerced_like_scalars(self) -> None:
        """str() coercion applies to list elements exactly as to scalars."""
        final_entry = {"type": "create_entry", "result": {"entry_id": "e1"}}
        submit_fn = AsyncMock(side_effect=[final_entry])

        await _handle_flow_steps(
            client=None,
            flow_id="flow-2116",
            initial_step=_cyclic_menu_step(),
            config={"next_step_id": [123]},
            submit_fn=submit_fn,
        )

        assert submit_fn.await_args_list[0].args[1] == {"next_step_id": "123"}

    async def test_group_type_and_menu_option_lists_work_identically(self) -> None:
        """All three selection keys accept lists through the same branch."""
        for key in ("group_type", "menu_option"):
            final_entry = {"type": "create_entry", "result": {"entry_id": "e1"}}
            submit_fn = AsyncMock(
                side_effect=[_main_params_form(), _cyclic_menu_step(), final_entry]
            )

            result = await _handle_flow_steps(
                client=None,
                flow_id="flow-2116",
                initial_step=_cyclic_menu_step(),
                config={key: ["main_params", "all_done"]},
                submit_fn=submit_fn,
            )

            assert result["success"] is True, key
            selections = [
                c.args[1]["next_step_id"]
                for c in submit_fn.await_args_list
                if "next_step_id" in c.args[1]
            ]
            assert selections == ["main_params", "all_done"], key

    async def test_empty_list_falls_through_to_next_selection_key(self) -> None:
        """An empty list under one key must not mask a selection under another."""
        final_entry = {"type": "create_entry", "result": {"entry_id": "e1"}}
        submit_fn = AsyncMock(side_effect=[_main_params_form(), final_entry])

        result = await _handle_flow_steps(
            client=None,
            flow_id="flow-2116",
            initial_step=_cyclic_menu_step(),
            config={"next_step_id": [], "group_type": "main_params"},
            submit_fn=submit_fn,
        )

        assert result["success"] is True
        assert submit_fn.await_args_list[0].args[1] == {"next_step_id": "main_params"}

    async def test_subentry_walker_exhausted_selections_error(self) -> None:
        """The revisited-menu error exists on the subentry walker too."""
        import json

        from fastmcp.exceptions import ToolError

        summary_menu = {
            "type": "menu",
            "flow_id": "flow-sub-2116",
            "step_id": "summary_menu",
            "menu_options": ["entity", "device", "save_changes"],
        }
        device_form = {
            "type": "form",
            "flow_id": "flow-sub-2116",
            "step_id": "device",
            "data_schema": [{"name": "model"}],
        }
        client = AsyncMock()
        client.submit_config_subentry_flow_step = AsyncMock(
            side_effect=[device_form, dict(summary_menu)]
        )

        with pytest.raises(ToolError) as exc_info:
            await _handle_config_subentry_flow_steps(
                client,
                "flow-sub-2116",
                summary_menu,
                {"next_step_id": "device", "model": "M1"},
                is_reconfigure=True,
            )

        body = json.loads(str(exc_info.value))
        assert "list of successive" in body["error"]["message"]
        assert body["consumed_menu_selections"] == ["device"]

    async def test_over_budget_timeout_names_consumed_selections(self) -> None:
        """A walk that exhausts its budget says what it consumed on the way."""
        import json

        from fastmcp.exceptions import ToolError

        form = {
            "type": "form",
            "flow_id": "flow-2116",
            "step_id": "again",
            "data_schema": [],
        }
        # One selection -> budget 12: the menu + 11 endless forms.
        submit_fn = AsyncMock(side_effect=[dict(form) for _ in range(30)])

        with pytest.raises(ToolError) as exc_info:
            await _handle_flow_steps(
                client=None,
                flow_id="flow-2116",
                initial_step=_cyclic_menu_step(),
                config={"next_step_id": "main_params"},
                submit_fn=submit_fn,
            )

        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "TIMEOUT_OPERATION"
        assert body["max_steps"] == 12
        assert body["consumed_menu_selections"] == ["main_params"]

    async def test_repeated_selection_revisits_branch_with_reuse_semantics(
        self,
    ) -> None:
        """Revisiting a branch resubmits the first visit's values, loudly.

        Per-visit values cannot be expressed in the flat config namespace;
        the second visit gets the recorded values plus a warning.
        """
        final_entry = {"type": "create_entry", "result": {"entry_id": "e1"}}
        submit_fn = AsyncMock(
            side_effect=[
                _main_params_form(),
                _cyclic_menu_step(),
                _main_params_form(),
                _cyclic_menu_step(),
                final_entry,
            ]
        )

        result = await _handle_flow_steps(
            client=None,
            flow_id="flow-2116",
            initial_step=_cyclic_menu_step(),
            config={
                "next_step_id": ["main_params", "main_params", "all_done"],
                "charge_efficiency": 0.92,
                "discharge_efficiency": 0.90,
            },
            submit_fn=submit_fn,
        )

        assert result["success"] is True
        # Visit 1 gets the caller's values; visit 2 gets the step's own
        # served suggestions (in real HA those are re-serialized with the
        # first visit's writes, so this is idempotent) — the caller-value
        # resubmission is reserved for fields with no step-owned value.
        assert submit_fn.await_args_list[1].args[1] == {
            "charge_efficiency": 0.92,
            "discharge_efficiency": 0.90,
        }
        assert submit_fn.await_args_list[3].args[1] == {
            "charge_efficiency": 0.85,
            "discharge_efficiency": 0.85,
        }
        assert "warnings" not in result

    async def test_revisited_suggestionless_branch_resubmits_caller_values(
        self,
    ) -> None:
        """A revisited step with bare required fields reuses the caller's
        values — once, with the reuse warning naming the step encounter."""

        def bare_form() -> dict[str, Any]:
            return {
                "type": "form",
                "flow_id": "flow-2116",
                "step_id": "main_params",
                "data_schema": [
                    {"name": "charge_efficiency", "required": True},
                    {"name": "discharge_efficiency", "required": True},
                ],
            }

        final_entry = {"type": "create_entry", "result": {"entry_id": "e1"}}
        submit_fn = AsyncMock(
            side_effect=[
                bare_form(),
                _cyclic_menu_step(),
                bare_form(),
                _cyclic_menu_step(),
                final_entry,
            ]
        )

        result = await _handle_flow_steps(
            client=None,
            flow_id="flow-2116",
            initial_step=_cyclic_menu_step(),
            config={
                "next_step_id": ["main_params", "main_params", "all_done"],
                "charge_efficiency": 0.92,
                "discharge_efficiency": 0.90,
            },
            submit_fn=submit_fn,
        )

        assert result["success"] is True
        values = {"charge_efficiency": 0.92, "discharge_efficiency": 0.90}
        assert submit_fn.await_args_list[1].args[1] == values
        assert submit_fn.await_args_list[3].args[1] == values
        assert any(
            "Resubmitted" in w and "step encounter" in w for w in result["warnings"]
        )

    async def test_schemaless_sweep_with_queued_selections_warns(self) -> None:
        """A schema-less step swallowing branch values must be visible."""
        schemaless_form = {
            "type": "form",
            "flow_id": "flow-2116",
            "step_id": "mystery",
            # no data_schema at all -> legacy consume-everything fallback
        }
        final_entry = {"type": "create_entry", "result": {"entry_id": "e1"}}
        submit_fn = AsyncMock(
            side_effect=[dict(schemaless_form), _cyclic_menu_step(), final_entry]
        )

        result = await _handle_flow_steps(
            client=None,
            flow_id="flow-2116",
            initial_step=_cyclic_menu_step(),
            config={
                "next_step_id": ["main_params", "all_done"],
                "later_branch_field": 1,
            },
            submit_fn=submit_fn,
        )

        assert result["success"] is True
        assert any("still queued" in w for w in result["warnings"])
