"""E2E tests for python_transform parameter."""

import pytest

from tests.src.e2e.utilities.assertions import MCPAssertions


@pytest.mark.asyncio
async def test_python_transform_simple_update(mcp_client, ha_client):
    """Test simple icon update with python_transform."""
    mcp = MCPAssertions(mcp_client)

    # Create dashboard
    await mcp.call_tool_success(
        "ha_config_set_dashboard",
        {
            "url_path": "test-python",
            "config": {
                "views": [
                    {
                        "cards": [
                            {"type": "button", "entity": "light.test", "icon": "mdi:lamp"}
                        ]
                    }
                ]
            },
        },
    )

    # Get config_hash
    get_result = await mcp.call_tool_success(
        "ha_config_get_dashboard", {"url_path": "test-python"}
    )
    config_hash = get_result["config_hash"]

    # Update with python_transform
    result = await mcp.call_tool_success(
        "ha_config_set_dashboard",
        {
            "url_path": "test-python",
            "config_hash": config_hash,
            "python_transform": "config['views'][0]['cards'][0]['icon'] = 'mdi:lightbulb'",
        },
    )

    assert result["success"] is True
    assert result["action"] == "python_transform"

    # Verify update
    verify = await mcp.call_tool_success(
        "ha_config_get_dashboard", {"url_path": "test-python"}
    )
    assert verify["config"]["views"][0]["cards"][0]["icon"] == "mdi:lightbulb"


@pytest.mark.asyncio
async def test_python_transform_pattern_update(mcp_client, ha_client):
    """Test pattern-based update with python_transform."""
    mcp = MCPAssertions(mcp_client)

    # Create dashboard with multiple light cards
    await mcp.call_tool_success(
        "ha_config_set_dashboard",
        {
            "url_path": "test-python-pattern",
            "config": {
                "views": [
                    {
                        "cards": [
                            {"entity": "light.living_room", "icon": "mdi:lamp"},
                            {"entity": "light.bedroom", "icon": "mdi:lamp"},
                            {"entity": "climate.thermostat", "icon": "mdi:temp"},
                        ]
                    }
                ]
            },
        },
    )

    # Get config_hash
    get_result = await mcp.call_tool_success(
        "ha_config_get_dashboard", {"url_path": "test-python-pattern"}
    )
    config_hash = get_result["config_hash"]

    # Update all lights with pattern
    result = await mcp.call_tool_success(
        "ha_config_set_dashboard",
        {
            "url_path": "test-python-pattern",
            "config_hash": config_hash,
            "python_transform": """
for card in config['views'][0]['cards']:
    if 'light' in card.get('entity', ''):
        card['icon'] = 'mdi:lightbulb-on'
""",
        },
    )

    assert result["success"] is True

    # Verify updates
    verify = await mcp.call_tool_success(
        "ha_config_get_dashboard", {"url_path": "test-python-pattern"}
    )
    cards = verify["config"]["views"][0]["cards"]
    assert cards[0]["icon"] == "mdi:lightbulb-on"  # light.living_room
    assert cards[1]["icon"] == "mdi:lightbulb-on"  # light.bedroom
    assert cards[2]["icon"] == "mdi:temp"  # climate (unchanged)


@pytest.mark.asyncio
async def test_python_transform_blocked_import(mcp_client, ha_client):
    """Test that imports are blocked."""
    mcp = MCPAssertions(mcp_client)

    # Create dashboard
    await mcp.call_tool_success(
        "ha_config_set_dashboard",
        {"url_path": "test-python-security", "config": {"views": [{"cards": []}]}},
    )

    get_result = await mcp.call_tool_success(
        "ha_config_get_dashboard", {"url_path": "test-python-security"}
    )
    config_hash = get_result["config_hash"]

    # Try malicious expression - should fail
    result = await mcp.call_tool_failure(
        "ha_config_set_dashboard",
        {
            "url_path": "test-python-security",
            "config_hash": config_hash,
            "python_transform": "import os; os.system('echo pwned')",
        },
    )
    # Verify error message mentions import or forbidden
    assert "import" in result["error"].lower() or "forbidden" in result["error"].lower()


@pytest.mark.asyncio
async def test_python_transform_requires_config_hash(mcp_client, ha_client):
    """Test that python_transform requires config_hash."""
    mcp = MCPAssertions(mcp_client)

    await mcp.call_tool_success(
        "ha_config_set_dashboard", {"url_path": "test-python-hash", "config": {"views": []}}
    )

    # Try without config_hash - should fail
    result = await mcp.call_tool_failure(
        "ha_config_set_dashboard",
        {"url_path": "test-python-hash", "python_transform": "config['views'] = []"},
    )
    # Verify error message mentions config_hash
    assert "config_hash" in result["error"].lower()


@pytest.mark.asyncio
async def test_python_transform_mutual_exclusivity(mcp_client, ha_client):
    """Test that python_transform is mutually exclusive with config."""
    mcp = MCPAssertions(mcp_client)

    # Try using both config and python_transform - should fail
    result = await mcp.call_tool_failure(
        "ha_config_set_dashboard",
        {
            "url_path": "test-exclusive",
            "config": {"views": []},
            "python_transform": "config['views'] = []",
        },
    )
    # Verify error message mentions mutual exclusivity
    assert (
        "multiple" in result["error"].lower()
        or "mutually exclusive" in result["error"].lower()
    )


@pytest.mark.asyncio
async def test_python_transform_add_card(mcp_client, ha_client):
    """Test adding a card with python_transform."""
    mcp = MCPAssertions(mcp_client)

    # Create dashboard
    await mcp.call_tool_success(
        "ha_config_set_dashboard",
        {"url_path": "test-python-add", "config": {"views": [{"cards": []}]}},
    )

    # Get config_hash
    get_result = await mcp.call_tool_success(
        "ha_config_get_dashboard", {"url_path": "test-python-add"}
    )
    config_hash = get_result["config_hash"]

    # Add card with python_transform
    result = await mcp.call_tool_success(
        "ha_config_set_dashboard",
        {
            "url_path": "test-python-add",
            "config_hash": config_hash,
            "python_transform": "config['views'][0]['cards'].append({'type': 'button', 'entity': 'light.bedroom'})",
        },
    )

    assert result["success"] is True

    # Verify card added
    verify = await mcp.call_tool_success(
        "ha_config_get_dashboard", {"url_path": "test-python-add"}
    )
    cards = verify["config"]["views"][0]["cards"]
    assert len(cards) == 1
    assert cards[0]["type"] == "button"
    assert cards[0]["entity"] == "light.bedroom"


@pytest.mark.asyncio
async def test_python_transform_delete_card(mcp_client, ha_client):
    """Test deleting a card with python_transform."""
    mcp = MCPAssertions(mcp_client)

    # Create dashboard with multiple cards
    await mcp.call_tool_success(
        "ha_config_set_dashboard",
        {
            "url_path": "test-python-delete",
            "config": {
                "views": [
                    {
                        "cards": [
                            {"type": "button", "entity": "light.one"},
                            {"type": "button", "entity": "light.two"},
                            {"type": "button", "entity": "light.three"},
                        ]
                    }
                ]
            },
        },
    )

    # Get config_hash
    get_result = await mcp.call_tool_success(
        "ha_config_get_dashboard", {"url_path": "test-python-delete"}
    )
    config_hash = get_result["config_hash"]

    # Delete middle card
    result = await mcp.call_tool_success(
        "ha_config_set_dashboard",
        {
            "url_path": "test-python-delete",
            "config_hash": config_hash,
            "python_transform": "del config['views'][0]['cards'][1]",
        },
    )

    assert result["success"] is True

    # Verify card deleted
    verify = await mcp.call_tool_success(
        "ha_config_get_dashboard", {"url_path": "test-python-delete"}
    )
    cards = verify["config"]["views"][0]["cards"]
    assert len(cards) == 2
    assert cards[0]["entity"] == "light.one"
    assert cards[1]["entity"] == "light.three"


@pytest.mark.asyncio
async def test_python_transform_hash_conflict(mcp_client, ha_client):
    """Test that hash conflicts are detected."""
    mcp = MCPAssertions(mcp_client)

    # Create dashboard
    await mcp.call_tool_success(
        "ha_config_set_dashboard",
        {"url_path": "test-python-conflict", "config": {"views": [{"cards": []}]}},
    )

    # Get config_hash
    get_result = await mcp.call_tool_success(
        "ha_config_get_dashboard", {"url_path": "test-python-conflict"}
    )
    config_hash = get_result["config_hash"]

    # Modify dashboard directly
    await mcp.call_tool_success(
        "ha_config_set_dashboard",
        {
            "url_path": "test-python-conflict",
            "config": {"views": [{"cards": [{"type": "button"}]}]},
        },
    )

    # Try to use old hash - should fail due to conflict
    result = await mcp.call_tool_failure(
        "ha_config_set_dashboard",
        {
            "url_path": "test-python-conflict",
            "config_hash": config_hash,
            "python_transform": "config['views'][0]['cards'].append({'type': 'tile'})",
        },
    )
    # Verify error message mentions conflict
    assert "conflict" in result["error"].lower() or "modified" in result["error"].lower()
