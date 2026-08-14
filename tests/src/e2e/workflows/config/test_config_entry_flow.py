"""
E2E tests for Config Entry Flow API.

Covers:
- Creating a form-only helper (min_max, mold_indicator)
- Creating a multi-step form helper (history_stats — user, state, options)
- Creating a menu-based helper (group — menu then form)
- Error feedback on missing menu selection (data_schema_unavailable_reason
  marker + menu_options inline on validation errors)
- Deletion of config-entry-based helpers

Known limitation: CYCLIC menu flows (a menu re-presented after each branch,
issue #2116 — battery_sim, MQTT device-subentry reconfigure) have no e2e
coverage because no integration in the test container exposes one; every
menu-rooted flow reachable here (group, template) is linear. The successive-
selection-list behavior is pinned by the ``TestCyclicMenuFlows`` unit suite
against the shared walker instead.
"""

import logging

import pytest

from ...utilities.assertions import MCPAssertions, safe_call_tool
from ...utilities.wait_helpers import wait_for_tool_result

logger = logging.getLogger(__name__)


async def _create_config_entry_helper(
    mcp_client, helper_type: str, config: dict, description: str
) -> str:
    """Create a config entry helper via unified ha_config_set_helper.

    The unified tool expects either a top-level `name` param or a `name` key
    in the `config` dict. The test fixtures place `name` inside `config`, so
    we forward it as-is. Polls until the new entry is registered, returns entry_id.
    """
    async with MCPAssertions(mcp_client) as mcp:
        data = await mcp.call_tool_success(
            "ha_config_set_helper",
            {
                "helper_type": helper_type,
                "name": config.get("name", ""),
                "config": config,
            },
        )
    assert data.get("success") is True
    entry_id = data.get("entry_id")
    assert entry_id is not None
    logger.info(f"Created {description}: {entry_id}")

    await wait_for_tool_result(
        mcp_client,
        tool_name="ha_get_integration",
        arguments={"entry_id": entry_id},
        predicate=lambda d: d.get("success") is True,
        description=f"{description} is registered",
    )
    return entry_id


@pytest.mark.asyncio
@pytest.mark.config
@pytest.mark.slow
class TestConfigEntryFlow:
    """Test Config Entry Flow helper creation."""

    async def test_create_min_max_helper(self, mcp_client):
        """Create a min_max helper (single form step, no menu)."""
        config = {
            "name": "test_min_max_e2e",
            "entity_ids": [
                "sensor.demo_temperature",
                "sensor.demo_outside_temperature",
            ],
            "type": "min",
        }
        entry_id = await _create_config_entry_helper(
            mcp_client, "min_max", config, "min_max helper"
        )

        await safe_call_tool(
            mcp_client,
            "ha_remove_helpers_integrations",
            {"target": entry_id, "confirm": True},
        )

    async def test_create_history_stats_helper(self, mcp_client):
        """Create a history_stats helper (issue #2187).

        Three form steps — user, state, options — driven from one flat config:
        each step pops the keys its own schema declares and leaves the rest for
        the next. The options step takes exactly two of start/end/duration; the
        `entity_id`/`state`/`type` it redeclares read-only are already in the
        flow's options, so nothing is resubmitted to them.
        """
        async with MCPAssertions(mcp_client) as mcp:
            data = await mcp.call_tool_success(
                "ha_config_set_helper",
                {
                    "helper_type": "history_stats",
                    "name": "test_history_stats_e2e",
                    "config": {
                        "name": "test_history_stats_e2e",
                        "entity_id": "sensor.demo_temperature",
                        "type": "time",
                        "state": ["22.5"],
                        "start": "{{ today_at('00:00') }}",
                        "end": "{{ now() }}",
                    },
                },
            )
        assert data.get("success") is True
        entry_id = data.get("entry_id")
        assert entry_id is not None

        try:
            entity_ids = data.get("entity_ids") or []
            assert entity_ids, f"No entity_ids in response: {data}"
            await wait_for_tool_result(
                mcp_client,
                tool_name="ha_get_state",
                arguments={"entity_id": entity_ids[0]},
                predicate=lambda d: d.get("data", {}).get("entity_id") == entity_ids[0],
                description="history_stats helper entity is queryable",
            )
        finally:
            await safe_call_tool(
                mcp_client,
                "ha_remove_helpers_integrations",
                {"target": entry_id, "confirm": True},
            )

    async def test_create_mold_indicator_helper(self, mcp_client):
        """Create a mold_indicator helper (issue #2187).

        Single form step taking three source entities plus a calibration
        factor, which HA rejects at zero.
        """
        async with MCPAssertions(mcp_client) as mcp:
            data = await mcp.call_tool_success(
                "ha_config_set_helper",
                {
                    "helper_type": "mold_indicator",
                    "name": "test_mold_indicator_e2e",
                    "config": {
                        "name": "test_mold_indicator_e2e",
                        "indoor_temp_sensor": "sensor.demo_temperature",
                        "indoor_humidity_sensor": "sensor.demo_humidity",
                        "outdoor_temp_sensor": "sensor.demo_outside_temperature",
                        "calibration_factor": 2.0,
                    },
                },
            )
        assert data.get("success") is True
        entry_id = data.get("entry_id")
        assert entry_id is not None

        try:
            entity_ids = data.get("entity_ids") or []
            assert entity_ids, f"No entity_ids in response: {data}"
            await wait_for_tool_result(
                mcp_client,
                tool_name="ha_get_state",
                arguments={"entity_id": entity_ids[0]},
                predicate=lambda d: d.get("data", {}).get("entity_id") == entity_ids[0],
                description="mold_indicator helper entity is queryable",
            )
        finally:
            await safe_call_tool(
                mcp_client,
                "ha_remove_helpers_integrations",
                {"target": entry_id, "confirm": True},
            )

    async def test_create_group_helper_light(self, mcp_client):
        """Create a light group helper (menu then form flow)."""
        config = {
            "group_type": "light",
            "name": "test_light_group_e2e",
            "entities": [],  # empty list is valid
            "hide_members": False,
        }
        entry_id = await _create_config_entry_helper(
            mcp_client, "group", config, "light group helper"
        )

        await safe_call_tool(
            mcp_client,
            "ha_remove_helpers_integrations",
            {"target": entry_id, "confirm": True},
        )

    async def test_create_template_sensor(self, mcp_client):
        """Create a template sensor helper end-to-end."""
        config = {
            "next_step_id": "sensor",
            "name": "test_template_sensor_e2e",
            "state": "{{ states('sun.sun') }}",
        }
        entry_id = await _create_config_entry_helper(
            mcp_client, "template", config, "template sensor"
        )

        await safe_call_tool(
            mcp_client,
            "ha_remove_helpers_integrations",
            {"target": entry_id, "confirm": True},
        )

    async def test_update_template_sensor_availability(self, mcp_client):
        """Persist and read a template field nested under advanced options."""
        config = {
            "next_step_id": "sensor",
            "name": "test_template_availability_e2e",
            "state": "{{ 1 }}",
        }
        entry_id = await _create_config_entry_helper(
            mcp_client, "template", config, "template sensor with availability"
        )
        availability = "{{ has_value('sensor.demo_temperature') }}"

        try:
            async with MCPAssertions(mcp_client) as mcp:
                update_data = await mcp.call_tool_success(
                    "ha_config_set_helper",
                    {
                        "helper_type": "template",
                        "helper_id": entry_id,
                        "action": "update",
                        "config": {
                            "state": "{{ 1 }}",
                            "availability": availability,
                            "availabilty": "{{ false }}",
                        },
                    },
                )
            assert update_data.get("warnings") == [
                "Ignored config keys not declared by the Home Assistant flow "
                "schema: availabilty"
            ]

            async with MCPAssertions(mcp_client) as mcp:
                integration_data = await mcp.call_tool_success(
                    "ha_get_integration",
                    {"entry_id": entry_id, "include_options": True},
                )
            options = (integration_data.get("entry") or {}).get("options") or {}
            assert options.get("availability") == availability, (
                "Nested availability option was not persisted or read back; "
                f"got {options!r}"
            )
        finally:
            await safe_call_tool(
                mcp_client,
                "ha_remove_helpers_integrations",
                {"target": entry_id, "confirm": True},
            )

    async def test_create_template_binary_sensor(self, mcp_client):
        """Create a template binary sensor helper end-to-end."""
        config = {
            "next_step_id": "binary_sensor",
            "name": "test_template_binary_sensor_e2e",
            "state": "{{ is_state('sun.sun', 'above_horizon') }}",
        }
        entry_id = await _create_config_entry_helper(
            mcp_client, "template", config, "template binary sensor"
        )

        await safe_call_tool(
            mcp_client,
            "ha_remove_helpers_integrations",
            {"target": entry_id, "confirm": True},
        )

    async def test_create_template_sensor_with_icon(self, mcp_client):
        """A template (flow) helper accepts `icon` and HA applies it to the
        resulting entity via the entity-registry icon override.

        This is the end-to-end proof for flow-helper icon support: the unit
        test only checks the WS wire format against a mock; here a real
        template sensor is created with an icon and HA's own state for the
        entity must report that icon.
        """
        async with MCPAssertions(mcp_client) as mcp:
            data = await mcp.call_tool_success(
                "ha_config_set_helper",
                {
                    "helper_type": "template",
                    "name": "e2e icon template",
                    "config": {
                        "next_step_id": "sensor",
                        "name": "e2e icon template",
                        "state": "{{ states('sun.sun') }}",
                    },
                    "icon": "mdi:flash",
                    "wait": True,
                },
            )
        assert data.get("success") is True
        entry_id = data.get("entry_id")
        assert entry_id is not None

        try:
            # icon is echoed back in the flow response (applied registry override).
            assert data.get("icon") == "mdi:flash", f"icon not echoed: {data}"
            entity_ids = data.get("entity_ids") or []
            assert entity_ids, f"No entity_ids in response: {data}"
            entity_id = entity_ids[0]
            # HA surfaces the entity-registry icon override in the entity's
            # state attributes — poll until it propagates.
            await wait_for_tool_result(
                mcp_client,
                tool_name="ha_get_state",
                arguments={"entity_id": entity_id},
                predicate=lambda d: (
                    d.get("data", {}).get("attributes", {}).get("icon") == "mdi:flash"
                ),
                description="template sensor icon override applied by HA",
            )
        finally:
            await safe_call_tool(
                mcp_client,
                "ha_remove_helpers_integrations",
                {"target": entry_id, "confirm": True},
            )

    async def test_get_entity_config_entry_id_and_options_optimal_path(
        self, mcp_client
    ):
        """End-to-end of the issue #1457 optimal read sequence:

            ha_get_entity(entity_id)                  -> config_entry_id
            ha_get_integration(entry_id,
                               include_options=True)  -> template body

        Covers the two read paths that previously had only unit coverage:
        ``ha_get_entity`` surfacing ``config_entry_id``, and
        ``include_options`` surfacing a UI template helper's body via
        ``description.suggested_value``.
        """
        body_marker = "optimalpathtemplatebody8829"
        name = "e2e optimal path template"
        # HA derives a template helper's entity_id from its name (slugified),
        # e.g. "Weather Message" -> sensor.weather_message (issue #1457).
        entity_id = "sensor.e2e_optimal_path_template"
        config = {
            "next_step_id": "sensor",
            "name": name,
            "state": "{{ states('sensor." + body_marker + "') }}",
        }
        entry_id = await _create_config_entry_helper(
            mcp_client, "template", config, "template sensor (optimal path)"
        )
        try:
            # Change #2: ha_get_entity surfaces the parent config_entry_id, so
            # an agent can jump straight to ha_get_integration without scanning
            # a domain list. Poll because the template entity registers shortly
            # after its config entry; the predicate also asserts the field.
            ge_data = await wait_for_tool_result(
                mcp_client,
                tool_name="ha_get_entity",
                arguments={"entity_id": entity_id},
                predicate=lambda d: (
                    (d.get("entity_entry") or {}).get("config_entry_id") == entry_id
                ),
                timeout=60,
                description="ha_get_entity surfaces config_entry_id",
            )
            assert ge_data["entity_entry"]["config_entry_id"] == entry_id, (
                "ha_get_entity must surface the parent config_entry_id"
            )

            # Optimal read path: include_options surfaces the template body via
            # the options-flow probe (description.suggested_value).
            async with MCPAssertions(mcp_client) as mcp:
                gi_data = await mcp.call_tool_success(
                    "ha_get_integration",
                    {"entry_id": entry_id, "include_options": True},
                )
            assert body_marker in str(gi_data), (
                "include_options should surface the template body for a "
                "UI-created template helper"
            )
            logger.info("✅ optimal read sequence verified end-to-end")
        finally:
            await safe_call_tool(
                mcp_client,
                "ha_remove_helpers_integrations",
                {"target": entry_id, "confirm": True},
            )

    async def test_include_options_reports_stored_hide_members(self, mcp_client):
        """Regression for issue #1575: report stored options, not schema defaults.

        A group helper created with ``hide_members=True`` exposes BOTH
        ``default: False`` and ``description.suggested_value: True`` on the
        same options-flow field; ``include_options`` must report the stored
        ``True``, not the static default the buggy precedence used to pick.
        """
        config = {
            "group_type": "light",
            "name": "test_hide_members_1575_e2e",
            "entities": [],  # empty keeps the test self-contained; the
            # hide_members OPTION is stored regardless of membership
            "hide_members": True,
        }
        entry_id = await _create_config_entry_helper(
            mcp_client, "group", config, "light group helper (hide_members=True)"
        )
        try:
            async with MCPAssertions(mcp_client) as mcp:
                gi_data = await mcp.call_tool_success(
                    "ha_get_integration",
                    {"entry_id": entry_id, "include_options": True},
                )
            options = (gi_data.get("entry") or {}).get("options") or {}
            assert options.get("hide_members") is True, (
                "include_options must report the stored hide_members=True, "
                f"not the schema default False (issue #1575); got {options!r}"
            )
            logger.info("✅ stored hide_members=True surfaced via include_options")
        finally:
            await safe_call_tool(
                mcp_client,
                "ha_remove_helpers_integrations",
                {"target": entry_id, "confirm": True},
            )

    async def test_update_min_max_helper(self, mcp_client):
        """Update an existing min_max helper via options flow (upsert with entry_id)."""
        config = {
            "name": "test_min_max_update_e2e",
            "entity_ids": ["sensor.demo_temperature"],
            "type": "min",
        }
        entry_id = await _create_config_entry_helper(
            mcp_client, "min_max", config, "min_max helper for update test"
        )

        # Update via options flow
        updated_config = {
            "entity_ids": [
                "sensor.demo_temperature",
                "sensor.demo_outside_temperature",
            ],
            "type": "max",
        }
        async with MCPAssertions(mcp_client) as mcp:
            update_data = await mcp.call_tool_success(
                "ha_config_set_helper",
                {
                    "helper_type": "min_max",
                    "name": "test_min_max_update_e2e",
                    "config": updated_config,
                    # unified tool normalizes entry_id -> helper_id for flow helpers
                    "helper_id": entry_id,
                },
            )
        assert update_data.get("updated") is True

        # Cleanup
        await safe_call_tool(
            mcp_client,
            "ha_remove_helpers_integrations",
            {"target": entry_id, "confirm": True},
        )

    async def test_get_integration_include_schema(self, mcp_client):
        """ha_get_integration with include_schema=True returns options_schema for eligible entries."""
        # Find an entry that supports options
        async with MCPAssertions(mcp_client) as mcp:
            list_data = await mcp.call_tool_success("ha_get_integration", {})
        entry = next(
            (e for e in list_data.get("entries", []) if e.get("supports_options")),
            None,
        )
        if entry is None:
            pytest.skip(
                "No config entries with supports_options=true in test environment"
            )

        async with MCPAssertions(mcp_client) as mcp:
            data = await mcp.call_tool_success(
                "ha_get_integration",
                {"entry_id": entry["entry_id"], "include_schema": True},
            )
        assert "options_schema" in data, "Expected options_schema in response"
        schema = data["options_schema"]
        assert schema.get("flow_type") in ("form", "menu")
        logger.info(
            f"options_schema flow_type={schema['flow_type']} for {entry['domain']}"
        )

    async def test_create_group_helper_missing_menu_selection(self, mcp_client):
        """Creating a group helper without group_type returns a helpful error
        with the legal sub-types inline as ``menu_options`` (issue #1186).
        """
        config = {"name": "my_group", "entities": []}  # missing group_type

        data = await safe_call_tool(
            mcp_client,
            "ha_config_set_helper",
            {"helper_type": "group", "name": "my_group", "config": config},
        )
        assert data.get("success") is not True, "Should fail without group_type"
        # The error should mention available options or the missing key
        error_str = str(data)
        assert any(
            kw in error_str.lower()
            for kw in ("menu", "group_type", "next_step_id", "selection", "option")
        ), f"Error should mention menu selection: {error_str}"
        # The error context must carry the legal sub-types inline so the
        # caller can pick a branch on the next try without a discovery
        # round-trip — see _handle_menu_step in config_entry_flow_menu.
        menu_options = data.get("menu_options")
        assert isinstance(menu_options, list) and menu_options, (
            f"Error should carry menu_options list: {data}"
        )
        assert "light" in menu_options, (
            f"Group menu_options should include 'light': {menu_options}"
        )
