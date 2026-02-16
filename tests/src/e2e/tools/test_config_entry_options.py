"""
Tests for config entry options flow tools.

Tests the ha_start_config_entry_options_flow, ha_submit_config_entry_options_step,
and ha_abort_config_entry_options_flow tools for configuring integrations via
their options flow.
"""

import logging

import pytest
from ..utilities.assertions import assert_mcp_success, parse_mcp_result, safe_call_tool

logger = logging.getLogger(__name__)


async def _find_options_entry(mcp_client) -> str | None:
    """Find a config entry that supports options flow.

    Uses ha_get_integration() to list all entries and returns the first
    entry_id where supports_options is true.
    """
    result = await mcp_client.call_tool(
        "ha_get_integration",
        {},
    )
    data = parse_mcp_result(result)
    entries = data.get("entries", [])

    for entry in entries:
        if entry.get("supports_options"):
            return entry["entry_id"]

    return None


@pytest.mark.asyncio
async def test_start_options_flow_returns_flow_id(mcp_client):
    """Test that starting an options flow returns a valid flow_id and step info."""
    entry_id = await _find_options_entry(mcp_client)
    if entry_id is None:
        pytest.skip("No integrations with supports_options=true in test environment")

    logger.info(f"Starting options flow for entry: {entry_id}")

    result = await mcp_client.call_tool(
        "ha_start_config_entry_options_flow",
        {"entry_id": entry_id},
    )
    data = assert_mcp_success(result, "Start options flow")

    assert data.get("success") is True
    assert data.get("flow_id") is not None, "Expected flow_id in response"
    assert data.get("type") in ("menu", "form"), (
        f"Expected type 'menu' or 'form', got '{data.get('type')}'"
    )

    # Clean up: abort the flow so it doesn't leak
    flow_id = data["flow_id"]
    await safe_call_tool(
        mcp_client,
        "ha_abort_config_entry_options_flow",
        {"flow_id": flow_id},
    )

    logger.info(
        f"Options flow started: flow_id={flow_id}, type={data.get('type')}, "
        f"step_id={data.get('step_id')}"
    )


@pytest.mark.asyncio
async def test_start_options_flow_invalid_entry_id(mcp_client):
    """Test that starting an options flow with a nonexistent entry_id fails."""
    logger.info("Testing start options flow with invalid entry_id")

    result = await safe_call_tool(
        mcp_client,
        "ha_start_config_entry_options_flow",
        {"entry_id": "nonexistent_entry_id_12345"},
    )

    assert result.get("success") is not True, (
        "Expected failure for nonexistent entry_id"
    )

    logger.info("Invalid entry_id correctly rejected")


@pytest.mark.asyncio
async def test_submit_options_flow_step(mcp_client):
    """Test submitting a step in an options flow.

    Starts a flow, then submits data for the first step. The response
    should indicate the next step or completion.
    """
    entry_id = await _find_options_entry(mcp_client)
    if entry_id is None:
        pytest.skip("No integrations with supports_options=true in test environment")

    # Start the flow
    start_result = await mcp_client.call_tool(
        "ha_start_config_entry_options_flow",
        {"entry_id": entry_id},
    )
    start_data = assert_mcp_success(start_result, "Start options flow for submit test")
    flow_id = start_data["flow_id"]
    flow_type = start_data.get("type")

    logger.info(f"Flow started: flow_id={flow_id}, type={flow_type}")

    try:
        # Build appropriate data for the step type
        if flow_type == "menu":
            menu_options = start_data.get("menu_options", [])
            if not menu_options:
                pytest.skip("Menu flow has no options to select")
            submit_data = {"next_step_id": menu_options[0]}
        else:
            # Form type: submit empty dict to see what happens (may get validation errors)
            submit_data = {}

        submit_result = await safe_call_tool(
            mcp_client,
            "ha_submit_config_entry_options_step",
            {"flow_id": flow_id, "data": submit_data},
        )

        # The submit may succeed (next step) or fail (validation error),
        # either is a valid response — we just verify the tool works
        logger.info(f"Submit result: success={submit_result.get('success')}, "
                     f"type={submit_result.get('type')}, step_id={submit_result.get('step_id')}")

    finally:
        # Always abort to clean up
        await safe_call_tool(
            mcp_client,
            "ha_abort_config_entry_options_flow",
            {"flow_id": flow_id},
        )


@pytest.mark.asyncio
async def test_submit_options_flow_invalid_flow_id(mcp_client):
    """Test that submitting to a nonexistent flow_id fails."""
    logger.info("Testing submit with invalid flow_id")

    result = await safe_call_tool(
        mcp_client,
        "ha_submit_config_entry_options_step",
        {"flow_id": "nonexistent_flow_12345", "data": "{}"},
    )

    assert result.get("success") is not True, (
        "Expected failure for nonexistent flow_id"
    )

    logger.info("Invalid flow_id correctly rejected")


@pytest.mark.asyncio
async def test_abort_options_flow(mcp_client):
    """Test aborting an in-progress options flow."""
    entry_id = await _find_options_entry(mcp_client)
    if entry_id is None:
        pytest.skip("No integrations with supports_options=true in test environment")

    # Start the flow
    start_result = await mcp_client.call_tool(
        "ha_start_config_entry_options_flow",
        {"entry_id": entry_id},
    )
    start_data = assert_mcp_success(start_result, "Start options flow for abort test")
    flow_id = start_data["flow_id"]

    logger.info(f"Aborting flow: {flow_id}")

    # Abort the flow
    abort_result = await mcp_client.call_tool(
        "ha_abort_config_entry_options_flow",
        {"flow_id": flow_id},
    )
    abort_data = assert_mcp_success(abort_result, "Abort options flow")

    assert abort_data.get("success") is True

    logger.info("Options flow aborted successfully")


@pytest.mark.asyncio
async def test_abort_options_flow_invalid_flow_id(mcp_client):
    """Test that aborting a nonexistent flow_id fails."""
    logger.info("Testing abort with invalid flow_id")

    result = await safe_call_tool(
        mcp_client,
        "ha_abort_config_entry_options_flow",
        {"flow_id": "nonexistent_flow_12345"},
    )

    assert result.get("success") is not True, (
        "Expected failure for nonexistent flow_id"
    )

    logger.info("Invalid flow_id correctly rejected on abort")
