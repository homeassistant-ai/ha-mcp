"""fetch_helper_flow_info: one-round-trip introspection of a helper's config flow.

Split out of test_helper_schema_inline.py (issue #1186 tests, plus the step_id
it now reports for #2632).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from ha_mcp.tools.config_entry_flow_introspect import fetch_helper_flow_info


class TestFetchHelperFlowInfo:
    """``fetch_helper_flow_info`` introspects a helper's config flow in
    a single HA round-trip and returns a dict with optional ``"schema"``
    and ``"menu_options"`` keys. Replaces the prior two helpers
    (``_fetch_data_schema_for_error_context`` + ``fetch_helper_menu_options``)
    that did the same flow start twice for menu-rooted helpers without
    a branch picked.
    """

    async def test_form_flow_returns_schema(self) -> None:
        # ``filter`` is non-menu — top step is a form whose data_schema
        # is returned directly.
        intro_schema = [{"name": "entity_id", "required": True}]
        client = AsyncMock()
        client.start_config_flow = AsyncMock(
            return_value={
                "type": "form",
                "flow_id": "intro-1",
                "step_id": "user",
                "data_schema": intro_schema,
            }
        )
        client.abort_config_flow = AsyncMock(return_value={})

        info = await fetch_helper_flow_info(client, "filter")

        # step_id keys the step's field help text in HA's translations (#2632).
        assert info == {"schema": intro_schema, "step_id": "user", "last_step": None}
        client.abort_config_flow.assert_called_once_with("intro-1")

    async def test_menu_flow_with_choice_submits_and_returns_branch_schema(
        self,
    ) -> None:
        # ``template`` with ``menu_choice="sensor"`` submits the menu
        # selection and returns the sensor-branch form schema.
        branch_schema = [{"name": "state", "required": True}]
        client = AsyncMock()
        client.start_config_flow = AsyncMock(
            return_value={
                "type": "menu",
                "flow_id": "menu-1",
                "menu_options": ["sensor", "binary_sensor"],
            }
        )
        client.submit_config_flow_step = AsyncMock(
            return_value={
                "type": "form",
                "flow_id": "menu-1",
                "step_id": "sensor",
                "data_schema": branch_schema,
            }
        )
        client.abort_config_flow = AsyncMock(return_value={})

        info = await fetch_helper_flow_info(client, "template", menu_choice="sensor")

        # menu_options is intentionally NOT surfaced when a choice was
        # picked — the caller already has it.
        assert info == {
            "schema": branch_schema,
            "step_id": "sensor",
            "last_step": None,
            "branch": "sensor",
        }

    async def test_menu_flow_without_choice_returns_menu_options(self) -> None:
        # ``template`` without a menu_choice can't be schema-fetched —
        # surface the legal sub-types instead so the caller can pick a
        # branch on the next try.
        client = AsyncMock()
        client.start_config_flow = AsyncMock(
            return_value={
                "type": "menu",
                "flow_id": "menu-1",
                "menu_options": ["sensor", "binary_sensor", "button"],
            }
        )
        client.abort_config_flow = AsyncMock(return_value={})

        info = await fetch_helper_flow_info(client, "template")

        assert info == {"menu_options": ["sensor", "binary_sensor", "button"]}
        # No submit_config_flow_step call — single HA round-trip.
        assert not hasattr(client.submit_config_flow_step, "called") or (
            not client.submit_config_flow_step.called
        )

    async def test_returns_empty_on_ha_failure(self) -> None:
        client = AsyncMock()
        client.start_config_flow = AsyncMock(side_effect=RuntimeError("offline"))

        info = await fetch_helper_flow_info(client, "template")

        assert info == {}

    async def test_filters_non_string_menu_options(self) -> None:
        # Defensive — if HA returns a non-string entry, drop it rather
        # than propagating type confusion to the caller.
        client = AsyncMock()
        client.start_config_flow = AsyncMock(
            return_value={
                "type": "menu",
                "flow_id": "menu-1",
                "menu_options": ["sensor", 42, None, "binary_sensor"],
            }
        )
        client.abort_config_flow = AsyncMock(return_value={})

        info = await fetch_helper_flow_info(client, "template")

        assert info == {"menu_options": ["sensor", "binary_sensor"]}

    async def test_menu_options_absent_when_key_missing(self) -> None:
        # HA returning a menu dict without the ``menu_options`` key (or
        # with a non-list value) yields ``{}`` rather than a broken
        # ``{"menu_options": None}`` shape.
        client = AsyncMock()
        client.start_config_flow = AsyncMock(
            return_value={
                "type": "menu",
                "flow_id": "menu-1",
                # menu_options key intentionally absent
            }
        )
        client.abort_config_flow = AsyncMock(return_value={})

        info = await fetch_helper_flow_info(client, "template")

        assert info == {}

    async def test_menu_options_absent_when_list_is_empty(self) -> None:
        # An empty options list still drops the ``menu_options`` key so
        # callers don't have to special-case empty-list-vs-missing.
        client = AsyncMock()
        client.start_config_flow = AsyncMock(
            return_value={
                "type": "menu",
                "flow_id": "menu-1",
                "menu_options": [],
            }
        )
        client.abort_config_flow = AsyncMock(return_value={})

        info = await fetch_helper_flow_info(client, "template")

        assert info == {}

    async def test_submit_failure_keeps_empty_info(self) -> None:
        # If submitting the menu choice raises, the helper returns
        # ``{}`` rather than swallowing into a partially-populated dict.
        client = AsyncMock()
        client.start_config_flow = AsyncMock(
            return_value={
                "type": "menu",
                "flow_id": "menu-1",
                "menu_options": ["sensor"],
            }
        )
        client.submit_config_flow_step = AsyncMock(
            side_effect=RuntimeError("submit failed")
        )
        client.abort_config_flow = AsyncMock(return_value={})

        info = await fetch_helper_flow_info(client, "template", menu_choice="sensor")

        assert info == {}
