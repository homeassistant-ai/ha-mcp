"""
E2E tests for ha_config_list_helpers(describe=True) (#2632).

The field list comes from the running Home Assistant: a flow helper's config
or options flow (with HA's own field help text), a storage helper's Core
schema via the component, or the static table when no component is present.
"""

import logging

import pytest

from ...utilities.assertions import assert_mcp_success, safe_call_tool

logger = logging.getLogger(__name__)


async def _describe(mcp_client, **args) -> dict:
    result = await mcp_client.call_tool(
        "ha_config_list_helpers", {"describe": True, **args}
    )
    return assert_mcp_success(result, f"describe {args}")


@pytest.mark.asyncio
@pytest.mark.config
async def test_flow_helper_fields_carry_ha_own_help_text(mcp_client):
    """An agent learns what a template sensor's state field means from HA itself."""
    data = await _describe(mcp_client, helper_type="template", menu_choice="sensor")

    fields = {f["name"]: f for f in data["fields"]}
    assert fields["state"]["required"] is True
    assert fields["state"].get("description"), fields["state"]


@pytest.mark.asyncio
@pytest.mark.config
async def test_menu_helper_without_choice_lists_its_sub_types(mcp_client):
    data = await _describe(mcp_client, helper_type="template")

    assert "sensor" in data["menu_options"]


@pytest.mark.asyncio
@pytest.mark.config
async def test_existing_flow_helper_reports_only_editable_fields_with_values(
    mcp_client,
):
    """Describing a created utility meter shows what its options flow allows."""
    created = assert_mcp_success(
        await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "utility_meter",
                "name": "E2E Describe Meter",
                "config": {"source": "sensor.outlet_1_power", "cycle": "daily"},
            },
        ),
        "create utility_meter",
    )
    entry_id = created["data"]["entry_id"]
    try:
        data = await _describe(
            mcp_client, helper_type="utility_meter", helper_id=entry_id
        )
        fields = {f["name"]: f for f in data["fields"]}
        assert fields["source"]["current"] == "sensor.outlet_1_power"
        # HA does not offer the reset cycle for editing after creation.
        assert "cycle" not in fields
    finally:
        await safe_call_tool(
            mcp_client,
            "ha_remove_helpers_integrations",
            {"target": entry_id, "confirm": True},
        )


@pytest.mark.asyncio
@pytest.mark.config
async def test_existing_storage_helper_reports_current_values(
    mcp_client, cleanup_tracker
):
    """Holds with and without the component: Core schema or static fallback."""
    created = assert_mcp_success(
        await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_number",
                "name": "E2E Describe Number",
                "config": {"min": 0, "max": 40},
            },
        ),
        "create input_number",
    )
    entity_id = created["entity_id"]
    cleanup_tracker.track("input_number", entity_id)
    helper_id = created["data"]["id"]

    data = await _describe(mcp_client, helper_type="input_number", helper_id=helper_id)

    currents = [f.get("current") for f in data["fields"] if "max" in f["name"]]
    assert currents == [40], data["fields"]
