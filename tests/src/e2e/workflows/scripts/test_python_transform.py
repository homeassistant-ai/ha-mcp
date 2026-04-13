"""E2E tests for python_transform parameter on scripts."""

import pytest

from tests.src.e2e.utilities.assertions import MCPAssertions


@pytest.mark.asyncio
async def test_python_transform_simple_update(mcp_client, ha_client):
    """Test simple property update with python_transform."""
    mcp = MCPAssertions(mcp_client)

    # Create script
    await mcp.call_tool_success(
        "ha_config_set_script",
        {
            "script_id": "test_py_transform",
            "config": {
                "alias": "Test Python Transform",
                "sequence": [
                    {
                        "alias": "Turn on light",
                        "action": "light.turn_on",
                        "target": {"entity_id": "light.test"},
                        "data": {"brightness": 100},
                    }
                ],
            },
        },
    )

    # Get config_hash
    get_result = await mcp.call_tool_success(
        "ha_config_get_script", {"script_id": "test_py_transform"}
    )
    config_hash = get_result["config_hash"]
    assert config_hash is not None

    # Update with python_transform
    result = await mcp.call_tool_success(
        "ha_config_set_script",
        {
            "script_id": "test_py_transform",
            "config_hash": config_hash,
            "python_transform": "config['sequence'][0]['data']['brightness'] = 255",
        },
    )

    assert result["success"] is True
    assert result["action"] == "python_transform"
    assert result["config_hash"] is not None

    # Verify update (config contains REST client wrapper with inner "config" key)
    verify = await mcp.call_tool_success(
        "ha_config_get_script", {"script_id": "test_py_transform"}
    )
    actual_config = verify["config"]["config"]
    assert actual_config["sequence"][0]["data"]["brightness"] == 255


@pytest.mark.asyncio
async def test_python_transform_requires_config_hash(mcp_client, ha_client):
    """Test that python_transform requires config_hash."""
    mcp = MCPAssertions(mcp_client)

    await mcp.call_tool_success(
        "ha_config_set_script",
        {
            "script_id": "test_py_hash_req",
            "config": {
                "sequence": [{"action": "light.turn_on", "target": {"entity_id": "light.test"}}],
            },
        },
    )

    # Try without config_hash - should fail
    result = await mcp.call_tool_failure(
        "ha_config_set_script",
        {
            "script_id": "test_py_hash_req",
            "python_transform": "config['sequence'] = []",
        },
    )
    error_msg = result["error"].get("message", str(result["error"])) if isinstance(result["error"], dict) else result["error"]
    assert "config_hash" in error_msg.lower()


@pytest.mark.asyncio
async def test_python_transform_mutual_exclusivity(mcp_client, ha_client):
    """Test that python_transform is mutually exclusive with config."""
    mcp = MCPAssertions(mcp_client)

    result = await mcp.call_tool_failure(
        "ha_config_set_script",
        {
            "script_id": "test_exclusive",
            "config": {"sequence": []},
            "python_transform": "config['sequence'] = []",
        },
    )
    error_msg = result["error"].get("message", str(result["error"])) if isinstance(result["error"], dict) else result["error"]
    assert "cannot use both" in error_msg.lower()


@pytest.mark.asyncio
async def test_python_transform_hash_conflict(mcp_client, ha_client):
    """Test that hash conflicts are detected."""
    mcp = MCPAssertions(mcp_client)

    await mcp.call_tool_success(
        "ha_config_set_script",
        {
            "script_id": "test_py_conflict",
            "config": {
                "alias": "Test Conflict",
                "sequence": [{"action": "light.turn_on", "target": {"entity_id": "light.test"}}],
            },
        },
    )

    # Get config_hash
    get_result = await mcp.call_tool_success(
        "ha_config_get_script", {"script_id": "test_py_conflict"}
    )
    config_hash = get_result["config_hash"]

    # Modify script directly (invalidates hash)
    await mcp.call_tool_success(
        "ha_config_set_script",
        {
            "script_id": "test_py_conflict",
            "config": {
                "alias": "Test Conflict Modified",
                "sequence": [{"action": "light.turn_off", "target": {"entity_id": "light.test"}}],
            },
        },
    )

    # Try to use old hash - should fail
    result = await mcp.call_tool_failure(
        "ha_config_set_script",
        {
            "script_id": "test_py_conflict",
            "config_hash": config_hash,
            "python_transform": "config['sequence'][0]['data'] = {'brightness': 100}",
        },
    )
    error_msg = result["error"].get("message", str(result["error"])) if isinstance(result["error"], dict) else result["error"]
    assert "conflict" in error_msg.lower() or "modified" in error_msg.lower()


@pytest.mark.asyncio
async def test_python_transform_blocked_import(mcp_client, ha_client):
    """Test that imports are blocked in python_transform."""
    mcp = MCPAssertions(mcp_client)

    await mcp.call_tool_success(
        "ha_config_set_script",
        {
            "script_id": "test_py_security",
            "config": {
                "sequence": [{"action": "light.turn_on", "target": {"entity_id": "light.test"}}],
            },
        },
    )

    get_result = await mcp.call_tool_success(
        "ha_config_get_script", {"script_id": "test_py_security"}
    )
    config_hash = get_result["config_hash"]

    # Try malicious expression - should fail
    result = await mcp.call_tool_failure(
        "ha_config_set_script",
        {
            "script_id": "test_py_security",
            "config_hash": config_hash,
            "python_transform": "import os; os.system('echo pwned')",
        },
    )
    error_msg = result["error"].get("message", str(result["error"])) if isinstance(result["error"], dict) else result["error"]
    assert "import" in error_msg.lower() or "forbidden" in error_msg.lower()


@pytest.mark.asyncio
async def test_config_hash_stable_across_reads(mcp_client, ha_client):
    """Test that two consecutive reads return the same config_hash.

    Validates no roundtrip normalization jitter — if the hash changes
    between reads without any modification, optimistic locking would
    produce phantom conflicts.
    """
    mcp = MCPAssertions(mcp_client)

    await mcp.call_tool_success(
        "ha_config_set_script",
        {
            "script_id": "test_hash_stability",
            "config": {
                "alias": "Hash Stability Test",
                "sequence": [
                    {"action": "light.turn_on", "target": {"entity_id": "light.test"}},
                    {"delay": {"seconds": 1}},
                    {"action": "light.turn_off", "target": {"entity_id": "light.test"}},
                ],
                "mode": "single",
            },
        },
    )

    # Two consecutive reads — hashes must be identical
    read1 = await mcp.call_tool_success(
        "ha_config_get_script", {"script_id": "test_hash_stability"}
    )
    read2 = await mcp.call_tool_success(
        "ha_config_get_script", {"script_id": "test_hash_stability"}
    )

    assert read1["config_hash"] == read2["config_hash"]
