"""E2E tests for python_transform parameter on scripts."""

import pytest

from tests.src.e2e.utilities.assertions import MCPAssertions


@pytest.mark.asyncio
async def test_python_transform_simple_update(mcp_client, ha_client):
    """Test simple property update with python_transform."""
    mcp = MCPAssertions(mcp_client)

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

    get_result = await mcp.call_tool_success(
        "ha_config_get_script", {"script_id": "test_py_transform"}
    )
    config_hash = get_result["config_hash"]
    assert config_hash is not None

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

    # Verify update via inner config body
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

    get_result = await mcp.call_tool_success(
        "ha_config_get_script", {"script_id": "test_py_conflict"}
    )
    config_hash = get_result["config_hash"]

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

    Validates no roundtrip normalization jitter.
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

    read1 = await mcp.call_tool_success(
        "ha_config_get_script", {"script_id": "test_hash_stability"}
    )
    read2 = await mcp.call_tool_success(
        "ha_config_get_script", {"script_id": "test_hash_stability"}
    )

    assert isinstance(read1["config_hash"], str) and len(read1["config_hash"]) == 16
    assert read1["config_hash"] == read2["config_hash"]


@pytest.mark.asyncio
async def test_chained_transforms(mcp_client, ha_client):
    """Test that returned config_hash can be used for a subsequent transform."""
    mcp = MCPAssertions(mcp_client)

    await mcp.call_tool_success(
        "ha_config_set_script",
        {
            "script_id": "test_chain",
            "config": {
                "alias": "Chain Test",
                "sequence": [
                    {"action": "light.turn_on", "target": {"entity_id": "light.test"}, "data": {"brightness": 50}},
                ],
            },
        },
    )

    get_result = await mcp.call_tool_success(
        "ha_config_get_script", {"script_id": "test_chain"}
    )

    # First transform
    result1 = await mcp.call_tool_success(
        "ha_config_set_script",
        {
            "script_id": "test_chain",
            "config_hash": get_result["config_hash"],
            "python_transform": "config['sequence'][0]['data']['brightness'] = 100",
        },
    )
    assert result1["success"] is True

    # Second transform using hash from first
    result2 = await mcp.call_tool_success(
        "ha_config_set_script",
        {
            "script_id": "test_chain",
            "config_hash": result1["config_hash"],
            "python_transform": "config['sequence'][0]['data']['brightness'] = 200",
        },
    )
    assert result2["success"] is True


@pytest.mark.asyncio
async def test_returned_hash_matches_next_get(mcp_client, ha_client):
    """Test that config_hash from transform matches a subsequent get."""
    mcp = MCPAssertions(mcp_client)

    await mcp.call_tool_success(
        "ha_config_set_script",
        {
            "script_id": "test_hash_match",
            "config": {
                "alias": "Hash Match Test",
                "sequence": [{"action": "light.turn_on", "target": {"entity_id": "light.test"}}],
            },
        },
    )

    get_result = await mcp.call_tool_success(
        "ha_config_get_script", {"script_id": "test_hash_match"}
    )

    transform_result = await mcp.call_tool_success(
        "ha_config_set_script",
        {
            "script_id": "test_hash_match",
            "config_hash": get_result["config_hash"],
            "python_transform": "config['alias'] = 'Updated Hash Match'",
        },
    )

    next_get = await mcp.call_tool_success(
        "ha_config_get_script", {"script_id": "test_hash_match"}
    )

    assert transform_result["config_hash"] == next_get["config_hash"]


@pytest.mark.asyncio
async def test_transform_invalid_config_rejected(mcp_client, ha_client):
    """Test that a transform producing invalid config is rejected."""
    mcp = MCPAssertions(mcp_client)

    await mcp.call_tool_success(
        "ha_config_set_script",
        {
            "script_id": "test_invalid_transform",
            "config": {
                "alias": "Invalid Transform Test",
                "sequence": [{"action": "light.turn_on", "target": {"entity_id": "light.test"}}],
            },
        },
    )

    get_result = await mcp.call_tool_success(
        "ha_config_get_script", {"script_id": "test_invalid_transform"}
    )

    # Remove sequence — should be rejected by validation
    result = await mcp.call_tool_failure(
        "ha_config_set_script",
        {
            "script_id": "test_invalid_transform",
            "config_hash": get_result["config_hash"],
            "python_transform": "del config['sequence']",
        },
    )
    error_msg = result["error"].get("message", str(result["error"])) if isinstance(result["error"], dict) else result["error"]
    assert "sequence" in error_msg.lower() or "use_blueprint" in error_msg.lower()


@pytest.mark.asyncio
async def test_full_config_update_with_config_hash(mcp_client, ha_client):
    """Test full config replacement with config_hash for optimistic locking."""
    mcp = MCPAssertions(mcp_client)

    await mcp.call_tool_success(
        "ha_config_set_script",
        {
            "script_id": "test_full_hash",
            "config": {
                "alias": "Full Hash Test",
                "sequence": [{"action": "light.turn_on", "target": {"entity_id": "light.test"}}],
            },
        },
    )

    get_result = await mcp.call_tool_success(
        "ha_config_get_script", {"script_id": "test_full_hash"}
    )

    # Full config update with matching hash — should succeed
    await mcp.call_tool_success(
        "ha_config_set_script",
        {
            "script_id": "test_full_hash",
            "config_hash": get_result["config_hash"],
            "config": {
                "alias": "Full Hash Updated",
                "sequence": [{"action": "light.turn_off", "target": {"entity_id": "light.test"}}],
            },
        },
    )


@pytest.mark.asyncio
async def test_full_config_update_with_stale_hash(mcp_client, ha_client):
    """Test full config replacement with stale config_hash is rejected."""
    mcp = MCPAssertions(mcp_client)

    await mcp.call_tool_success(
        "ha_config_set_script",
        {
            "script_id": "test_full_stale",
            "config": {
                "alias": "Full Stale Test",
                "sequence": [{"action": "light.turn_on", "target": {"entity_id": "light.test"}}],
            },
        },
    )

    get_result = await mcp.call_tool_success(
        "ha_config_get_script", {"script_id": "test_full_stale"}
    )
    old_hash = get_result["config_hash"]

    # Modify to invalidate hash
    await mcp.call_tool_success(
        "ha_config_set_script",
        {
            "script_id": "test_full_stale",
            "config": {
                "alias": "Modified",
                "sequence": [{"delay": {"seconds": 1}}],
            },
        },
    )

    # Full config with stale hash — should fail
    result = await mcp.call_tool_failure(
        "ha_config_set_script",
        {
            "script_id": "test_full_stale",
            "config_hash": old_hash,
            "config": {
                "alias": "Should Fail",
                "sequence": [{"delay": {"seconds": 2}}],
            },
        },
    )
    error_msg = result["error"].get("message", str(result["error"])) if isinstance(result["error"], dict) else result["error"]
    assert "conflict" in error_msg.lower() or "modified" in error_msg.lower()
