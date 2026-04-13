"""E2E tests for python_transform parameter on automations."""

import pytest

from tests.src.e2e.utilities.assertions import MCPAssertions


@pytest.mark.asyncio
async def test_python_transform_simple_update(mcp_client, ha_client):
    """Test simple property update with python_transform."""
    mcp = MCPAssertions(mcp_client)

    # Create automation
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

    # Get config_hash
    get_result = await mcp.call_tool_success(
        "ha_config_get_automation", {"identifier": entity_id}
    )
    config_hash = get_result["config_hash"]
    assert config_hash is not None

    # Update with python_transform
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

    # Verify update
    verify = await mcp.call_tool_success(
        "ha_config_get_automation", {"identifier": entity_id}
    )
    assert verify["config"]["action"][0]["data"]["brightness"] == 255


@pytest.mark.asyncio
async def test_python_transform_pattern_update(mcp_client, ha_client):
    """Test pattern-based update with python_transform."""
    mcp = MCPAssertions(mcp_client)

    # Create automation with multiple actions
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

    # Update all light actions
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

    # Verify updates
    verify = await mcp.call_tool_success(
        "ha_config_get_automation", {"identifier": entity_id}
    )
    actions = verify["config"]["action"]
    assert actions[0]["data"]["brightness"] == 200  # light action updated
    assert actions[1]["data"]["brightness"] == 200  # light action updated
    assert actions[2]["data"]["temperature"] == 22  # climate unchanged


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

    # Try without config_hash - should fail
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

    # Try without identifier - should fail
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

    # Get config_hash
    get_result = await mcp.call_tool_success(
        "ha_config_get_automation", {"identifier": entity_id}
    )
    config_hash = get_result["config_hash"]

    # Modify automation directly (invalidates hash)
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

    # Try to use old hash - should fail
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

    # Try malicious expression - should fail
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
