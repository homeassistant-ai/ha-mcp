"""E2E tests for python_transform parameter on automations."""

import pytest

from tests.src.e2e.utilities.assertions import MCPAssertions


@pytest.mark.asyncio
async def test_python_transform_simple_update(mcp_client, ha_client):
    """Test simple property update with python_transform."""
    mcp = MCPAssertions(mcp_client)

    create_result = await mcp.call_tool_success(
        "ha_config_set_automation",
        {
            "config": {
                "alias": "Test Python Transform",
                "trigger": [{"platform": "time", "at": "07:00:00"}],
                "action": [
                    {
                        "alias": "Turn on light",
                        "action": "light.turn_on",
                        "target": {"entity_id": "light.test"},
                        "data": {"brightness": 100},
                    }
                ],
            }
        },
    )
    entity_id = create_result["entity_id"]

    get_result = await mcp.call_tool_success(
        "ha_config_get_automation", {"identifier": entity_id}
    )
    config_hash = get_result["config_hash"]
    assert config_hash is not None

    result = await mcp.call_tool_success(
        "ha_config_set_automation",
        {
            "identifier": entity_id,
            "config_hash": config_hash,
            "python_transform": "config['action'][0]['data']['brightness'] = 255",
        },
    )

    assert result["success"] is True
    assert result["action"] == "python_transform"
    assert result["config_hash"] is not None

    verify = await mcp.call_tool_success(
        "ha_config_get_automation", {"identifier": entity_id}
    )
    assert verify["config"]["action"][0]["data"]["brightness"] == 255


@pytest.mark.asyncio
async def test_python_transform_pattern_update(mcp_client, ha_client):
    """Test pattern-based update with python_transform."""
    mcp = MCPAssertions(mcp_client)

    create_result = await mcp.call_tool_success(
        "ha_config_set_automation",
        {
            "config": {
                "alias": "Test Pattern Transform",
                "trigger": [{"platform": "time", "at": "08:00:00"}],
                "action": [
                    {"alias": "Step A", "action": "light.turn_on", "target": {"entity_id": "light.a"}, "data": {"brightness": 50}},
                    {"alias": "Step B", "action": "light.turn_on", "target": {"entity_id": "light.b"}, "data": {"brightness": 50}},
                    {"alias": "Step C", "action": "climate.set_temperature", "target": {"entity_id": "climate.test"}, "data": {"temperature": 22}},
                ],
            }
        },
    )
    entity_id = create_result["entity_id"]

    get_result = await mcp.call_tool_success(
        "ha_config_get_automation", {"identifier": entity_id}
    )
    config_hash = get_result["config_hash"]

    result = await mcp.call_tool_success(
        "ha_config_set_automation",
        {
            "identifier": entity_id,
            "config_hash": config_hash,
            "python_transform": """
for a in config['action']:
    if a.get('action') == 'light.turn_on':
        a['data']['brightness'] = 200
""",
        },
    )

    assert result["success"] is True

    verify = await mcp.call_tool_success(
        "ha_config_get_automation", {"identifier": entity_id}
    )
    actions = verify["config"]["action"]
    assert actions[0]["data"]["brightness"] == 200
    assert actions[1]["data"]["brightness"] == 200
    assert actions[2]["data"]["temperature"] == 22


@pytest.mark.asyncio
async def test_python_transform_requires_config_hash(mcp_client, ha_client):
    """Test that python_transform requires config_hash."""
    mcp = MCPAssertions(mcp_client)

    create_result = await mcp.call_tool_success(
        "ha_config_set_automation",
        {
            "config": {
                "alias": "Test Hash Required",
                "trigger": [{"platform": "time", "at": "09:00:00"}],
                "action": [{"action": "light.turn_on", "target": {"entity_id": "light.test"}}],
            }
        },
    )
    entity_id = create_result["entity_id"]

    result = await mcp.call_tool_failure(
        "ha_config_set_automation",
        {
            "identifier": entity_id,
            "python_transform": "config['action'] = []",
        },
    )
    error_msg = result["error"].get("message", str(result["error"])) if isinstance(result["error"], dict) else result["error"]
    assert "config_hash" in error_msg.lower()


@pytest.mark.asyncio
async def test_python_transform_requires_identifier(mcp_client, ha_client):
    """Test that python_transform requires identifier."""
    mcp = MCPAssertions(mcp_client)

    result = await mcp.call_tool_failure(
        "ha_config_set_automation",
        {
            "config_hash": "fakehash",
            "python_transform": "config['action'] = []",
        },
    )
    error_msg = result["error"].get("message", str(result["error"])) if isinstance(result["error"], dict) else result["error"]
    assert "identifier" in error_msg.lower()


@pytest.mark.asyncio
async def test_python_transform_mutual_exclusivity(mcp_client, ha_client):
    """Test that python_transform is mutually exclusive with config."""
    mcp = MCPAssertions(mcp_client)

    result = await mcp.call_tool_failure(
        "ha_config_set_automation",
        {
            "identifier": "automation.test",
            "config": {"alias": "test", "trigger": [], "action": []},
            "python_transform": "config['action'] = []",
        },
    )
    error_msg = result["error"].get("message", str(result["error"])) if isinstance(result["error"], dict) else result["error"]
    assert "cannot use both" in error_msg.lower()


@pytest.mark.asyncio
async def test_python_transform_hash_conflict(mcp_client, ha_client):
    """Test that hash conflicts are detected."""
    mcp = MCPAssertions(mcp_client)

    create_result = await mcp.call_tool_success(
        "ha_config_set_automation",
        {
            "config": {
                "alias": "Test Conflict",
                "trigger": [{"platform": "time", "at": "10:00:00"}],
                "action": [{"action": "light.turn_on", "target": {"entity_id": "light.test"}}],
            }
        },
    )
    entity_id = create_result["entity_id"]

    get_result = await mcp.call_tool_success(
        "ha_config_get_automation", {"identifier": entity_id}
    )
    config_hash = get_result["config_hash"]

    await mcp.call_tool_success(
        "ha_config_set_automation",
        {
            "identifier": entity_id,
            "config": {
                "alias": "Test Conflict Modified",
                "trigger": [{"platform": "time", "at": "11:00:00"}],
                "action": [{"action": "light.turn_off", "target": {"entity_id": "light.test"}}],
            },
        },
    )

    result = await mcp.call_tool_failure(
        "ha_config_set_automation",
        {
            "identifier": entity_id,
            "config_hash": config_hash,
            "python_transform": "config['action'][0]['data'] = {'brightness': 100}",
        },
    )
    error_msg = result["error"].get("message", str(result["error"])) if isinstance(result["error"], dict) else result["error"]
    assert "conflict" in error_msg.lower() or "modified" in error_msg.lower()


@pytest.mark.asyncio
async def test_python_transform_blocked_import(mcp_client, ha_client):
    """Test that imports are blocked in python_transform."""
    mcp = MCPAssertions(mcp_client)

    create_result = await mcp.call_tool_success(
        "ha_config_set_automation",
        {
            "config": {
                "alias": "Test Security",
                "trigger": [{"platform": "time", "at": "12:00:00"}],
                "action": [{"action": "light.turn_on", "target": {"entity_id": "light.test"}}],
            }
        },
    )
    entity_id = create_result["entity_id"]

    get_result = await mcp.call_tool_success(
        "ha_config_get_automation", {"identifier": entity_id}
    )
    config_hash = get_result["config_hash"]

    result = await mcp.call_tool_failure(
        "ha_config_set_automation",
        {
            "identifier": entity_id,
            "config_hash": config_hash,
            "python_transform": "import os; os.system('echo pwned')",
        },
    )
    error_msg = result["error"].get("message", str(result["error"])) if isinstance(result["error"], dict) else result["error"]
    assert "import" in error_msg.lower() or "forbidden" in error_msg.lower()


@pytest.mark.asyncio
async def test_chained_transforms(mcp_client, ha_client):
    """Test that returned config_hash can be used for a subsequent transform."""
    mcp = MCPAssertions(mcp_client)

    create_result = await mcp.call_tool_success(
        "ha_config_set_automation",
        {
            "config": {
                "alias": "Test Chain",
                "trigger": [{"platform": "time", "at": "13:00:00"}],
                "action": [
                    {"action": "light.turn_on", "target": {"entity_id": "light.test"}, "data": {"brightness": 50}},
                ],
            }
        },
    )
    entity_id = create_result["entity_id"]

    get_result = await mcp.call_tool_success(
        "ha_config_get_automation", {"identifier": entity_id}
    )

    # First transform
    result1 = await mcp.call_tool_success(
        "ha_config_set_automation",
        {
            "identifier": entity_id,
            "config_hash": get_result["config_hash"],
            "python_transform": "config['action'][0]['data']['brightness'] = 100",
        },
    )
    assert result1["success"] is True

    # Second transform using hash from first
    result2 = await mcp.call_tool_success(
        "ha_config_set_automation",
        {
            "identifier": entity_id,
            "config_hash": result1["config_hash"],
            "python_transform": "config['action'][0]['data']['brightness'] = 200",
        },
    )
    assert result2["success"] is True


@pytest.mark.asyncio
async def test_returned_hash_matches_next_get(mcp_client, ha_client):
    """Test that config_hash from transform matches a subsequent get."""
    mcp = MCPAssertions(mcp_client)

    create_result = await mcp.call_tool_success(
        "ha_config_set_automation",
        {
            "config": {
                "alias": "Test Hash Match",
                "trigger": [{"platform": "time", "at": "14:00:00"}],
                "action": [{"action": "light.turn_on", "target": {"entity_id": "light.test"}}],
            }
        },
    )
    entity_id = create_result["entity_id"]

    get_result = await mcp.call_tool_success(
        "ha_config_get_automation", {"identifier": entity_id}
    )

    transform_result = await mcp.call_tool_success(
        "ha_config_set_automation",
        {
            "identifier": entity_id,
            "config_hash": get_result["config_hash"],
            "python_transform": "config['alias'] = 'Updated Hash Match'",
        },
    )

    next_get = await mcp.call_tool_success(
        "ha_config_get_automation", {"identifier": entity_id}
    )

    assert transform_result["config_hash"] == next_get["config_hash"]


@pytest.mark.asyncio
async def test_transform_invalid_config_rejected(mcp_client, ha_client):
    """Test that a transform producing invalid config is rejected."""
    mcp = MCPAssertions(mcp_client)

    create_result = await mcp.call_tool_success(
        "ha_config_set_automation",
        {
            "config": {
                "alias": "Test Invalid Transform",
                "trigger": [{"platform": "time", "at": "15:00:00"}],
                "action": [{"action": "light.turn_on", "target": {"entity_id": "light.test"}}],
            }
        },
    )
    entity_id = create_result["entity_id"]

    get_result = await mcp.call_tool_success(
        "ha_config_get_automation", {"identifier": entity_id}
    )

    # Remove action — should be rejected by validation
    result = await mcp.call_tool_failure(
        "ha_config_set_automation",
        {
            "identifier": entity_id,
            "config_hash": get_result["config_hash"],
            "python_transform": "del config['action']",
        },
    )
    error_msg = result["error"].get("message", str(result["error"])) if isinstance(result["error"], dict) else result["error"]
    assert "action" in error_msg.lower() or "missing" in error_msg.lower()


@pytest.mark.asyncio
async def test_config_hash_stable_across_reads(mcp_client, ha_client):
    """Test that two consecutive reads return the same config_hash."""
    mcp = MCPAssertions(mcp_client)

    create_result = await mcp.call_tool_success(
        "ha_config_set_automation",
        {
            "config": {
                "alias": "Test Hash Stability",
                "trigger": [{"platform": "time", "at": "16:00:00"}],
                "action": [
                    {"action": "light.turn_on", "target": {"entity_id": "light.test"}},
                    {"delay": {"seconds": 1}},
                    {"action": "light.turn_off", "target": {"entity_id": "light.test"}},
                ],
            }
        },
    )
    entity_id = create_result["entity_id"]

    read1 = await mcp.call_tool_success(
        "ha_config_get_automation", {"identifier": entity_id}
    )
    read2 = await mcp.call_tool_success(
        "ha_config_get_automation", {"identifier": entity_id}
    )

    assert isinstance(read1["config_hash"], str) and len(read1["config_hash"]) == 16
    assert read1["config_hash"] == read2["config_hash"]


@pytest.mark.asyncio
async def test_plural_key_hash_stability(mcp_client, ha_client):
    """Test that plural keys (triggers/actions) don't cause hash instability.

    HA REST API returns plural keys which get normalized to singular.
    Hash must be stable across this normalization.
    """
    mcp = MCPAssertions(mcp_client)

    create_result = await mcp.call_tool_success(
        "ha_config_set_automation",
        {
            "config": {
                "alias": "Test Plural Keys",
                "triggers": [{"platform": "time", "at": "17:00:00"}],
                "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.test"}}],
            }
        },
    )
    entity_id = create_result["entity_id"]

    read1 = await mcp.call_tool_success(
        "ha_config_get_automation", {"identifier": entity_id}
    )
    read2 = await mcp.call_tool_success(
        "ha_config_get_automation", {"identifier": entity_id}
    )

    assert isinstance(read1["config_hash"], str) and len(read1["config_hash"]) == 16
    assert read1["config_hash"] == read2["config_hash"]

    # Transform using the hash should succeed
    await mcp.call_tool_success(
        "ha_config_set_automation",
        {
            "identifier": entity_id,
            "config_hash": read1["config_hash"],
            "python_transform": "config['alias'] = 'Plural Keys Updated'",
        },
    )


@pytest.mark.asyncio
async def test_full_config_update_with_config_hash(mcp_client, ha_client):
    """Test full config replacement with config_hash for optimistic locking."""
    mcp = MCPAssertions(mcp_client)

    create_result = await mcp.call_tool_success(
        "ha_config_set_automation",
        {
            "config": {
                "alias": "Test Full Hash",
                "trigger": [{"platform": "time", "at": "18:00:00"}],
                "action": [{"action": "light.turn_on", "target": {"entity_id": "light.test"}}],
            }
        },
    )
    entity_id = create_result["entity_id"]

    get_result = await mcp.call_tool_success(
        "ha_config_get_automation", {"identifier": entity_id}
    )

    # Full config update with matching hash — should succeed
    await mcp.call_tool_success(
        "ha_config_set_automation",
        {
            "identifier": entity_id,
            "config_hash": get_result["config_hash"],
            "config": {
                "alias": "Full Hash Updated",
                "trigger": [{"platform": "time", "at": "18:30:00"}],
                "action": [{"action": "light.turn_off", "target": {"entity_id": "light.test"}}],
            },
        },
    )


@pytest.mark.asyncio
async def test_full_config_update_with_stale_hash(mcp_client, ha_client):
    """Test full config replacement with stale config_hash is rejected."""
    mcp = MCPAssertions(mcp_client)

    create_result = await mcp.call_tool_success(
        "ha_config_set_automation",
        {
            "config": {
                "alias": "Test Full Stale",
                "trigger": [{"platform": "time", "at": "19:00:00"}],
                "action": [{"action": "light.turn_on", "target": {"entity_id": "light.test"}}],
            }
        },
    )
    entity_id = create_result["entity_id"]

    get_result = await mcp.call_tool_success(
        "ha_config_get_automation", {"identifier": entity_id}
    )
    old_hash = get_result["config_hash"]

    # Modify to invalidate hash
    await mcp.call_tool_success(
        "ha_config_set_automation",
        {
            "identifier": entity_id,
            "config": {
                "alias": "Modified",
                "trigger": [{"platform": "time", "at": "19:30:00"}],
                "action": [{"delay": {"seconds": 1}}],
            },
        },
    )

    # Full config with stale hash — should fail
    result = await mcp.call_tool_failure(
        "ha_config_set_automation",
        {
            "identifier": entity_id,
            "config_hash": old_hash,
            "config": {
                "alias": "Should Fail",
                "trigger": [{"platform": "time", "at": "20:00:00"}],
                "action": [{"delay": {"seconds": 2}}],
            },
        },
    )
    error_msg = result["error"].get("message", str(result["error"])) if isinstance(result["error"], dict) else result["error"]
    assert "conflict" in error_msg.lower() or "modified" in error_msg.lower()


@pytest.mark.asyncio
async def test_categorized_automation_transform_preserves_category(mcp_client, ha_client):
    """Test that python_transform on a categorized automation preserves the category.

    Regression test for blocker #1: category must be popped before upsert
    (HA REST API rejects unknown keys) and re-applied afterwards.
    """
    mcp = MCPAssertions(mcp_client)

    # Create a category
    cat_result = await mcp.call_tool_success(
        "ha_config_set_category",
        {"name": "E2E Transform Category Test", "scope": "automation"},
    )
    category_id = cat_result["category_id"]

    # Create automation with category
    create_result = await mcp.call_tool_success(
        "ha_config_set_automation",
        {
            "config": {
                "alias": "Test Categorized Transform",
                "trigger": [{"platform": "time", "at": "21:00:00"}],
                "action": [
                    {"action": "light.turn_on", "target": {"entity_id": "light.test"}, "data": {"brightness": 50}},
                ],
            },
            "category": category_id,
        },
    )
    entity_id = create_result["entity_id"]

    # Get config_hash
    get_result = await mcp.call_tool_success(
        "ha_config_get_automation", {"identifier": entity_id}
    )
    config_hash = get_result["config_hash"]
    assert get_result["config"].get("category") == category_id

    # Transform a property
    await mcp.call_tool_success(
        "ha_config_set_automation",
        {
            "identifier": entity_id,
            "config_hash": config_hash,
            "python_transform": "config['action'][0]['data']['brightness'] = 200",
        },
    )

    # Verify category is preserved and transform applied
    verify = await mcp.call_tool_success(
        "ha_config_get_automation", {"identifier": entity_id}
    )
    assert verify["config"]["action"][0]["data"]["brightness"] == 200
    assert verify["config"].get("category") == category_id
