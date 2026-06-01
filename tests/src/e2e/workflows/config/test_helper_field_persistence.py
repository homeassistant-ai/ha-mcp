"""
E2E tests for helper field persistence across UPDATE operations.

These tests close the destructive-UPDATE coverage gap identified in
``local/TEST_GAP_ANALYSIS.md``: existing CRUD tests verify HA state after
CREATE but never re-verify HA state after UPDATE, so destructive UPDATE
bugs (Bug 8 in issue #1150) — where a partial UPDATE silently wipes
type-specific fields not provided in the call — slipped through.

Pattern per simple type:

1. CREATE the helper with FULL config (every type-specific field set to a
   non-default value, plus icon).
2. Wait for the entity to be registered, then read state via
   ``ha_get_state`` and assert every configured field appears in
   ``attributes``.
3. UPDATE via ``ha_config_set_helper`` with ``helper_id`` set and ONLY the
   ``name`` changed.
4. Re-read state and assert every original field is STILL present — no
   destructive wipe.

Covers: input_boolean, input_select, input_number, input_text,
input_datetime, counter, timer. Skips: schedule, zone, person, tag.

Also includes a cross-cutting test: a fully-configured input_number
updated with only ``min_value=70`` must preserve initial, mode,
unit_of_measurement, step, and icon.
"""

import logging

import pytest

from ...utilities.assertions import (
    assert_mcp_success,
    parse_mcp_result,
    safe_call_tool,
)
from ...utilities.wait_helpers import wait_for_condition

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Local helpers — duplicated from test_helper_crud.py to keep this file
# self-contained without modifying the existing test module.
# ---------------------------------------------------------------------------


async def _wait_for_entity_registration(
    mcp_client, entity_id: str, timeout: int = 20
) -> bool:
    """Poll until the entity is queryable via ``ha_get_state``."""

    async def entity_exists():
        data = await safe_call_tool(
            mcp_client, "ha_get_state", {"entity_id": entity_id}
        )
        return "data" in data and data["data"] is not None

    return await wait_for_condition(
        entity_exists, timeout=timeout, condition_name=f"{entity_id} registration"
    )


def _entity_id_from_create(data: dict, helper_type: str) -> str | None:
    """Extract entity_id from the ``ha_config_set_helper`` create response."""
    entity_id = data.get("entity_id")
    if not entity_id:
        helper_id = data.get("data", {}).get("id")
        if helper_id:
            entity_id = f"{helper_type}.{helper_id}"
    return entity_id


async def _get_attributes(mcp_client, entity_id: str) -> dict:
    """Fetch entity state and return its ``attributes`` dict (or ``{}``)."""
    state_result = await mcp_client.call_tool(
        "ha_get_state", {"entity_id": entity_id}
    )
    state_data = parse_mcp_result(state_result)
    return state_data.get("data", {}).get("attributes", {}) or {}


async def _get_state_value(mcp_client, entity_id: str):
    """Fetch entity state and return the top-level ``state`` value."""
    state_result = await mcp_client.call_tool(
        "ha_get_state", {"entity_id": entity_id}
    )
    state_data = parse_mcp_result(state_result)
    return state_data.get("data", {}).get("state")


def _assert_field(
    attrs: dict, field: str, expected, *, phase: str, entity_id: str
) -> None:
    """Assert ``attrs[field] == expected`` with a clear error message."""
    actual = attrs.get(field)
    assert actual == expected, (
        f"[{phase}] {entity_id} attribute {field!r}: "
        f"expected {expected!r}, got {actual!r}. Full attrs: {attrs}"
    )


# ---------------------------------------------------------------------------
# Per-type destructive-UPDATE protection tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.config
class TestInputBooleanFieldPersistence:
    """input_boolean fields must survive a name-only UPDATE."""

    async def test_input_boolean_icon_persists_after_name_only_update(
        self, mcp_client, cleanup_tracker
    ):
        helper_type = "input_boolean"
        original_name = "E2E Persistence Boolean"
        original_icon = "mdi:toggle-switch"

        # CREATE with full config
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": helper_type,
                "name": original_name,
                "icon": original_icon,
                "initial": "on",
            },
        )
        create_data = assert_mcp_success(create_result, "Create input_boolean")
        entity_id = _entity_id_from_create(create_data, helper_type)
        assert entity_id, f"Missing entity_id: {create_data}"
        cleanup_tracker.track(helper_type, entity_id)

        ready = await _wait_for_entity_registration(mcp_client, entity_id)
        assert ready, f"{entity_id} not registered within timeout"

        # POST-CREATE: icon attribute must reflect what we set
        post_create_attrs = await _get_attributes(mcp_client, entity_id)
        _assert_field(
            post_create_attrs,
            "icon",
            original_icon,
            phase="post-create",
            entity_id=entity_id,
        )

        # UPDATE with ONLY the name changed
        update_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": helper_type,
                "helper_id": entity_id,
                "name": "E2E Persistence Boolean Renamed",
            },
        )
        assert_mcp_success(update_result, "Update input_boolean (name only)")

        # POST-UPDATE: icon must still be the originally configured one
        post_update_attrs = await _get_attributes(mcp_client, entity_id)
        _assert_field(
            post_update_attrs,
            "icon",
            original_icon,
            phase="post-update",
            entity_id=entity_id,
        )


@pytest.mark.asyncio
@pytest.mark.config
class TestInputSelectFieldPersistence:
    """input_select options + icon must survive a name-only UPDATE."""

    async def test_input_select_options_persist_after_name_only_update(
        self, mcp_client, cleanup_tracker
    ):
        helper_type = "input_select"
        original_options = ["Alpha", "Beta", "Gamma"]
        original_icon = "mdi:format-list-bulleted"

        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": helper_type,
                "name": "E2E Persistence Select",
                "options": original_options,
                "initial": "Beta",
                "icon": original_icon,
            },
        )
        create_data = assert_mcp_success(create_result, "Create input_select")
        entity_id = _entity_id_from_create(create_data, helper_type)
        assert entity_id, f"Missing entity_id: {create_data}"
        cleanup_tracker.track(helper_type, entity_id)

        ready = await _wait_for_entity_registration(mcp_client, entity_id)
        assert ready, f"{entity_id} not registered within timeout"

        post_create_attrs = await _get_attributes(mcp_client, entity_id)
        post_create_options = list(post_create_attrs.get("options", []))
        for opt in original_options:
            assert opt in post_create_options, (
                f"[post-create] option {opt!r} missing from {post_create_options}"
            )
        _assert_field(
            post_create_attrs,
            "icon",
            original_icon,
            phase="post-create",
            entity_id=entity_id,
        )

        update_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": helper_type,
                "helper_id": entity_id,
                "name": "E2E Persistence Select Renamed",
            },
        )
        assert_mcp_success(update_result, "Update input_select (name only)")

        post_update_attrs = await _get_attributes(mcp_client, entity_id)
        post_update_options = list(post_update_attrs.get("options", []))
        for opt in original_options:
            assert opt in post_update_options, (
                f"[post-update] option {opt!r} was wiped from "
                f"{post_update_options}. Full attrs: {post_update_attrs}"
            )
        _assert_field(
            post_update_attrs,
            "icon",
            original_icon,
            phase="post-update",
            entity_id=entity_id,
        )


@pytest.mark.asyncio
@pytest.mark.config
class TestInputNumberFieldPersistence:
    """input_number numeric/display fields must survive a name-only UPDATE."""

    async def test_input_number_full_config_persists_after_name_only_update(
        self, mcp_client, cleanup_tracker
    ):
        helper_type = "input_number"
        original = {
            "min": 0,
            "max": 100,
            "step": 5,
            "mode": "slider",
            "unit_of_measurement": "%",
            "initial": 25,
            "icon": "mdi:gauge",
        }

        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": helper_type,
                "name": "E2E Persistence Number",
                "min_value": original["min"],
                "max_value": original["max"],
                "step": original["step"],
                "mode": original["mode"],
                "unit_of_measurement": original["unit_of_measurement"],
                "initial": original["initial"],
                "icon": original["icon"],
            },
        )
        create_data = assert_mcp_success(create_result, "Create input_number")
        entity_id = _entity_id_from_create(create_data, helper_type)
        assert entity_id, f"Missing entity_id: {create_data}"
        cleanup_tracker.track(helper_type, entity_id)

        ready = await _wait_for_entity_registration(mcp_client, entity_id)
        assert ready, f"{entity_id} not registered within timeout"

        post_create_attrs = await _get_attributes(mcp_client, entity_id)
        for field, expected in original.items():
            _assert_field(
                post_create_attrs,
                field,
                expected,
                phase="post-create",
                entity_id=entity_id,
            )

        update_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": helper_type,
                "helper_id": entity_id,
                "name": "E2E Persistence Number Renamed",
            },
        )
        assert_mcp_success(update_result, "Update input_number (name only)")

        post_update_attrs = await _get_attributes(mcp_client, entity_id)
        for field, expected in original.items():
            _assert_field(
                post_update_attrs,
                field,
                expected,
                phase="post-update",
                entity_id=entity_id,
            )


@pytest.mark.asyncio
@pytest.mark.config
class TestInputTextFieldPersistence:
    """input_text length/mode/icon must survive a name-only UPDATE."""

    async def test_input_text_full_config_persists_after_name_only_update(
        self, mcp_client, cleanup_tracker
    ):
        helper_type = "input_text"
        # input_text exposes min/max (length) and mode in attributes.
        original = {
            "min": 3,
            "max": 50,
            "mode": "password",
            "icon": "mdi:form-textbox-password",
        }

        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": helper_type,
                "name": "E2E Persistence Text",
                "min_value": original["min"],
                "max_value": original["max"],
                "mode": original["mode"],
                "icon": original["icon"],
                "initial": "secret",
            },
        )
        create_data = assert_mcp_success(create_result, "Create input_text")
        entity_id = _entity_id_from_create(create_data, helper_type)
        assert entity_id, f"Missing entity_id: {create_data}"
        cleanup_tracker.track(helper_type, entity_id)

        ready = await _wait_for_entity_registration(mcp_client, entity_id)
        assert ready, f"{entity_id} not registered within timeout"

        post_create_attrs = await _get_attributes(mcp_client, entity_id)
        for field, expected in original.items():
            _assert_field(
                post_create_attrs,
                field,
                expected,
                phase="post-create",
                entity_id=entity_id,
            )

        update_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": helper_type,
                "helper_id": entity_id,
                "name": "E2E Persistence Text Renamed",
            },
        )
        assert_mcp_success(update_result, "Update input_text (name only)")

        post_update_attrs = await _get_attributes(mcp_client, entity_id)
        for field, expected in original.items():
            _assert_field(
                post_update_attrs,
                field,
                expected,
                phase="post-update",
                entity_id=entity_id,
            )


@pytest.mark.asyncio
@pytest.mark.config
class TestInputDatetimeFieldPersistence:
    """input_datetime has_date/has_time/icon must survive a name-only UPDATE."""

    async def test_input_datetime_full_config_persists_after_name_only_update(
        self, mcp_client, cleanup_tracker
    ):
        helper_type = "input_datetime"
        # Use date-only (non-default would be has_date=False/has_time=True;
        # we choose date-only so destructive-default behavior — silently
        # re-enabling has_time — would be detected).
        original = {
            "has_date": True,
            "has_time": False,
            "icon": "mdi:calendar",
        }

        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": helper_type,
                "name": "E2E Persistence Datetime",
                "has_date": original["has_date"],
                "has_time": original["has_time"],
                "icon": original["icon"],
            },
        )
        create_data = assert_mcp_success(create_result, "Create input_datetime")
        entity_id = _entity_id_from_create(create_data, helper_type)
        assert entity_id, f"Missing entity_id: {create_data}"
        cleanup_tracker.track(helper_type, entity_id)

        ready = await _wait_for_entity_registration(mcp_client, entity_id)
        assert ready, f"{entity_id} not registered within timeout"

        post_create_attrs = await _get_attributes(mcp_client, entity_id)
        for field, expected in original.items():
            _assert_field(
                post_create_attrs,
                field,
                expected,
                phase="post-create",
                entity_id=entity_id,
            )

        update_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": helper_type,
                "helper_id": entity_id,
                "name": "E2E Persistence Datetime Renamed",
            },
        )
        assert_mcp_success(update_result, "Update input_datetime (name only)")

        post_update_attrs = await _get_attributes(mcp_client, entity_id)
        for field, expected in original.items():
            _assert_field(
                post_update_attrs,
                field,
                expected,
                phase="post-update",
                entity_id=entity_id,
            )


@pytest.mark.asyncio
@pytest.mark.config
class TestCounterFieldPersistence:
    """counter min/max/step/initial/icon must survive a name-only UPDATE.

    Note: counter exposes ``minimum`` and ``maximum`` as attribute names
    (not ``min``/``max`` like input_number). The tool maps ``min_value``
    -> ``minimum`` and ``max_value`` -> ``maximum`` per HA's counter API.
    """

    async def test_counter_full_config_persists_after_name_only_update(
        self, mcp_client, cleanup_tracker
    ):
        helper_type = "counter"
        original = {
            "minimum": 0,
            "maximum": 50,
            "step": 2,
            "initial": 10,
            "icon": "mdi:counter",
        }

        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": helper_type,
                "name": "E2E Persistence Counter",
                "min_value": original["minimum"],
                "max_value": original["maximum"],
                "step": original["step"],
                "initial": original["initial"],
                "icon": original["icon"],
                "restore": True,
            },
        )
        create_data = assert_mcp_success(create_result, "Create counter")
        entity_id = _entity_id_from_create(create_data, helper_type)
        assert entity_id, f"Missing entity_id: {create_data}"
        cleanup_tracker.track(helper_type, entity_id)

        ready = await _wait_for_entity_registration(mcp_client, entity_id)
        assert ready, f"{entity_id} not registered within timeout"

        post_create_attrs = await _get_attributes(mcp_client, entity_id)
        for field, expected in original.items():
            _assert_field(
                post_create_attrs,
                field,
                expected,
                phase="post-create",
                entity_id=entity_id,
            )

        update_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": helper_type,
                "helper_id": entity_id,
                "name": "E2E Persistence Counter Renamed",
            },
        )
        assert_mcp_success(update_result, "Update counter (name only)")

        post_update_attrs = await _get_attributes(mcp_client, entity_id)
        for field, expected in original.items():
            _assert_field(
                post_update_attrs,
                field,
                expected,
                phase="post-update",
                entity_id=entity_id,
            )


@pytest.mark.asyncio
@pytest.mark.config
class TestTimerFieldPersistence:
    """timer duration + icon must survive a name-only UPDATE."""

    async def test_timer_duration_persists_after_name_only_update(
        self, mcp_client, cleanup_tracker
    ):
        helper_type = "timer"
        original_duration = "0:05:00"
        original_icon = "mdi:timer"

        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": helper_type,
                "name": "E2E Persistence Timer",
                "duration": original_duration,
                "icon": original_icon,
                "restore": True,
            },
        )
        create_data = assert_mcp_success(create_result, "Create timer")
        entity_id = _entity_id_from_create(create_data, helper_type)
        assert entity_id, f"Missing entity_id: {create_data}"
        cleanup_tracker.track(helper_type, entity_id)

        ready = await _wait_for_entity_registration(mcp_client, entity_id)
        assert ready, f"{entity_id} not registered within timeout"

        # Timer exposes ``duration`` as an attribute (HH:MM:SS string).
        post_create_attrs = await _get_attributes(mcp_client, entity_id)
        _assert_field(
            post_create_attrs,
            "duration",
            original_duration,
            phase="post-create",
            entity_id=entity_id,
        )
        _assert_field(
            post_create_attrs,
            "icon",
            original_icon,
            phase="post-create",
            entity_id=entity_id,
        )

        update_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": helper_type,
                "helper_id": entity_id,
                "name": "E2E Persistence Timer Renamed",
            },
        )
        assert_mcp_success(update_result, "Update timer (name only)")

        post_update_attrs = await _get_attributes(mcp_client, entity_id)
        _assert_field(
            post_update_attrs,
            "duration",
            original_duration,
            phase="post-update",
            entity_id=entity_id,
        )
        _assert_field(
            post_update_attrs,
            "icon",
            original_icon,
            phase="post-update",
            entity_id=entity_id,
        )


# ---------------------------------------------------------------------------
# Cross-cutting: partial UPDATE that touches one field must preserve the
# other type-specific fields. This generalises the per-type tests above to
# the case where the UPDATE *does* mutate a real attribute, not just the
# name — which is the most common destructive-merge bug class.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.config
class TestInputNumberPartialUpdatePreservesFields:
    """A partial UPDATE that changes only ``min_value`` must preserve the
    other type-specific fields (initial, mode, unit_of_measurement, step,
    icon)."""

    async def test_partial_min_value_update_preserves_other_fields(
        self, mcp_client, cleanup_tracker
    ):
        helper_type = "input_number"
        # Full original config. The test must change min within a range that
        # still contains the existing initial; otherwise HA's voluptuous schema
        # (correctly) rejects "Initial value 25 not in range 30-100". The
        # purpose of the test is to verify the merge preserves the other
        # fields, not to test the cross-field range constraint.
        preserved = {
            "max": 100,
            "step": 5,
            "mode": "slider",
            "unit_of_measurement": "%",
            "initial": 25,
            "icon": "mdi:gauge",
        }
        new_min = 20  # 0 -> 20; still <= initial=25 so HA accepts

        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": helper_type,
                "name": "E2E Partial Update Number",
                "min_value": 0,
                "max_value": preserved["max"],
                "step": preserved["step"],
                "mode": preserved["mode"],
                "unit_of_measurement": preserved["unit_of_measurement"],
                "initial": preserved["initial"],
                "icon": preserved["icon"],
            },
        )
        create_data = assert_mcp_success(create_result, "Create input_number")
        entity_id = _entity_id_from_create(create_data, helper_type)
        assert entity_id, f"Missing entity_id: {create_data}"
        cleanup_tracker.track(helper_type, entity_id)

        ready = await _wait_for_entity_registration(mcp_client, entity_id)
        assert ready, f"{entity_id} not registered within timeout"

        # Sanity check post-create
        post_create_attrs = await _get_attributes(mcp_client, entity_id)
        for field, expected in preserved.items():
            _assert_field(
                post_create_attrs,
                field,
                expected,
                phase="post-create",
                entity_id=entity_id,
            )
        _assert_field(
            post_create_attrs, "min", 0, phase="post-create", entity_id=entity_id
        )

        # PARTIAL UPDATE: only min_value changes (and helper_id is required)
        update_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": helper_type,
                "helper_id": entity_id,
                "min_value": new_min,
            },
        )
        assert_mcp_success(update_result, "Partial update (min_value only)")

        # New min must apply, all other fields must be preserved
        post_update_attrs = await _get_attributes(mcp_client, entity_id)
        _assert_field(
            post_update_attrs,
            "min",
            new_min,
            phase="post-update",
            entity_id=entity_id,
        )
        for field, expected in preserved.items():
            _assert_field(
                post_update_attrs,
                field,
                expected,
                phase="post-update",
                entity_id=entity_id,
            )
