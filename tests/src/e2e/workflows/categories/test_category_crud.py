"""
E2E tests for Home Assistant category CRUD operations.

Tests the complete lifecycle of categories including:
- List, create, get, update, and delete operations
- Category assignment to entities via ha_set_entity
- Category properties (name, icon)
"""

import logging

import pytest

from ...utilities.assertions import assert_mcp_success, safe_call_tool

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
@pytest.mark.config
class TestCategoryCRUD:
    """Test category CRUD operations."""

    async def test_list_categories(self, mcp_client):
        """Test listing all categories for a scope."""
        logger.info("Testing ha_config_get_category (list mode)")

        result = await mcp_client.call_tool(
            "ha_config_get_category",
            {"scope": "automation"},
        )

        data = assert_mcp_success(result, "List categories")

        assert "categories" in data, f"Missing 'categories' in response: {data}"
        assert "count" in data, f"Missing 'count' in response: {data}"
        assert isinstance(data["categories"], list), (
            f"categories should be a list: {data}"
        )

        logger.info(f"Found {data['count']} categories for scope 'automation'")
        for cat in data["categories"][:5]:  # Log first 5
            logger.info(
                f"  - {cat.get('name', 'Unknown')} (id: {cat.get('category_id')})"
            )

    async def test_category_full_lifecycle(self, mcp_client, cleanup_tracker):
        """Test complete category lifecycle: create, get, update, delete."""
        logger.info("Testing category full lifecycle")

        category_name = "E2E Test Category"
        scope = "automation"

        # CREATE
        create_result = await mcp_client.call_tool(
            "ha_config_set_category",
            {
                "name": category_name,
                "scope": scope,
                "icon": "mdi:tag",
            },
        )

        create_data = assert_mcp_success(create_result, "Create category")
        category_id = create_data.get("category_id")
        assert category_id, f"Missing category_id in create response: {create_data}"
        cleanup_tracker.track("category", category_id)
        logger.info(f"Created category: {category_name} (id: {category_id})")

        # GET specific category
        get_result = await mcp_client.call_tool(
            "ha_config_get_category",
            {"scope": scope, "category_id": category_id},
        )
        get_data = assert_mcp_success(get_result, "Get category")
        assert "category" in get_data, f"Missing 'category' in response: {get_data}"
        assert get_data["category"]["name"] == category_name, (
            f"Name mismatch: {get_data['category']}"
        )
        logger.info("Category retrieved successfully")

        # UPDATE
        update_result = await mcp_client.call_tool(
            "ha_config_set_category",
            {
                "category_id": category_id,
                "name": "E2E Test Category Updated",
                "scope": scope,
            },
        )
        update_data = assert_mcp_success(update_result, "Update category")
        logger.info(f"Updated category: {update_data.get('message')}")

        # VERIFY UPDATE via get
        get_result = await mcp_client.call_tool(
            "ha_config_get_category",
            {"scope": scope, "category_id": category_id},
        )
        get_data = assert_mcp_success(get_result, "Get updated category")
        assert get_data["category"]["name"] == "E2E Test Category Updated", (
            f"Updated name mismatch: {get_data['category']}"
        )
        logger.info("Category update verified")

        # DELETE
        delete_result = await mcp_client.call_tool(
            "ha_config_remove_category",
            {"scope": scope, "category_id": category_id},
        )
        delete_data = assert_mcp_success(delete_result, "Delete category")
        logger.info(f"Deleted category: {delete_data.get('message')}")

        # VERIFY DELETION
        get_data = await safe_call_tool(
            mcp_client,
            "ha_config_get_category",
            {"scope": scope, "category_id": category_id},
        )
        assert not get_data.get("success"), (
            f"Deleted category should not be found: {get_data}"
        )
        logger.info("Category deletion verified")

    async def test_create_category_minimal(self, mcp_client, cleanup_tracker):
        """Test creating category with minimal required fields (name + scope)."""
        logger.info("Testing minimal category creation")

        result = await mcp_client.call_tool(
            "ha_config_set_category",
            {"name": "E2E Minimal Category", "scope": "automation"},
        )

        data = assert_mcp_success(result, "Create minimal category")
        category_id = data.get("category_id")
        assert category_id, f"Missing category_id: {data}"
        cleanup_tracker.track("category", category_id)
        logger.info(f"Created minimal category: {category_id}")

        # Clean up
        await mcp_client.call_tool(
            "ha_config_remove_category",
            {"scope": "automation", "category_id": category_id},
        )

    async def test_create_category_with_icon(self, mcp_client, cleanup_tracker):
        """Test creating category with MDI icon."""
        logger.info("Testing category creation with icon")

        result = await mcp_client.call_tool(
            "ha_config_set_category",
            {
                "name": "E2E Icon Category",
                "scope": "automation",
                "icon": "mdi:robot",
            },
        )

        data = assert_mcp_success(result, "Create category with icon")
        category_id = data.get("category_id")
        cleanup_tracker.track("category", category_id)
        logger.info(f"Created category with icon: {category_id}")

        # Verify icon was saved
        get_result = await mcp_client.call_tool(
            "ha_config_get_category",
            {"scope": "automation", "category_id": category_id},
        )
        get_data = assert_mcp_success(get_result, "Get category with icon")
        assert get_data["category"].get("icon") == "mdi:robot", (
            f"Icon mismatch: {get_data['category']}"
        )
        logger.info("Category icon verified")

        # Clean up
        await mcp_client.call_tool(
            "ha_config_remove_category",
            {"scope": "automation", "category_id": category_id},
        )

    async def test_get_nonexistent_category(self, mcp_client):
        """Test getting a non-existent category."""
        logger.info("Testing get non-existent category")

        data = await safe_call_tool(
            mcp_client,
            "ha_config_get_category",
            {"scope": "automation", "category_id": "nonexistent_category_xyz_12345"},
        )

        assert not data.get("success"), f"Should fail for non-existent category: {data}"
        logger.info("Non-existent category properly returned error")

    async def test_delete_nonexistent_category(self, mcp_client):
        """
        Test deleting a non-existent category returns a structured error,
        not success=True.

        Source path: WebSocket result.success=False →
        raise_tool_error(SERVICE_CALL_FAILED, "Failed to delete category: ...").
        Hardened from if/else-log pattern to explicit assertions.
        """
        logger.info("Testing delete non-existent category")

        data = await safe_call_tool(
            mcp_client,
            "ha_config_remove_category",
            {"scope": "automation", "category_id": "nonexistent_category_xyz_12345"},
        )

        assert not data.get("success"), (
            f"Expected failure for nonexistent category, got success=True: {data}"
        )
        assert data["error"]["code"] == "SERVICE_CALL_FAILED", (
            f"Expected error code SERVICE_CALL_FAILED, got: {data.get('error')}"
        )
        error_msg = str(data.get("error", "")).lower()
        assert "doesn't exist" in error_msg or "not found" in error_msg, (
            f"Expected 'doesn't exist'/'not found' in error message, got: {data.get('error')}"
        )


@pytest.mark.asyncio
@pytest.mark.config
class TestCategoryAssignment:
    """Test category assignment to entities via ha_set_entity."""

    async def test_assign_category_to_automation(
        self, mcp_client, cleanup_tracker, test_light_entity
    ):
        """Test assigning a category to an entity."""
        logger.info(f"Testing category assignment to {test_light_entity}")

        # Create a test category
        create_result = await mcp_client.call_tool(
            "ha_config_set_category",
            {"name": "E2E Assignment Test", "scope": "automation"},
        )
        create_data = assert_mcp_success(
            create_result, "Create category for assignment"
        )
        category_id = create_data.get("category_id")
        cleanup_tracker.track("category", category_id)
        logger.info(f"Created category for assignment: {category_id}")

        # Assign category to entity
        assign_result = await mcp_client.call_tool(
            "ha_set_entity",
            {
                "entity_id": test_light_entity,
                "categories": {"automation": category_id},
            },
        )
        assign_data = assert_mcp_success(assign_result, "Assign category to entity")
        logger.info(f"Category assigned: {assign_data.get('message')}")

        # Clear category from entity
        clear_result = await mcp_client.call_tool(
            "ha_set_entity",
            {
                "entity_id": test_light_entity,
                "categories": {"automation": None},
            },
        )
        clear_data = assert_mcp_success(clear_result, "Clear category from entity")
        logger.info(f"Category cleared: {clear_data.get('message')}")

        # Clean up category
        await mcp_client.call_tool(
            "ha_config_remove_category",
            {"scope": "automation", "category_id": category_id},
        )

    async def test_clear_category_from_automation(
        self, mcp_client, cleanup_tracker, test_light_entity
    ):
        """Test clearing a category from an entity using null value."""
        logger.info(f"Testing category clear on {test_light_entity}")

        # Create a test category
        create_result = await mcp_client.call_tool(
            "ha_config_set_category",
            {"name": "E2E Clear Test", "scope": "automation"},
        )
        create_data = assert_mcp_success(
            create_result, "Create category for clear test"
        )
        category_id = create_data.get("category_id")
        cleanup_tracker.track("category", category_id)

        # Assign category
        await mcp_client.call_tool(
            "ha_set_entity",
            {
                "entity_id": test_light_entity,
                "categories": {"automation": category_id},
            },
        )
        logger.info("Category assigned, now clearing")

        # Clear using null value
        clear_result = await mcp_client.call_tool(
            "ha_set_entity",
            {
                "entity_id": test_light_entity,
                "categories": '{"automation": null}',  # JSON string format
            },
        )
        clear_data = assert_mcp_success(clear_result, "Clear category via null")
        logger.info(f"Category cleared via null: {clear_data.get('message')}")

        # Clean up category
        await mcp_client.call_tool(
            "ha_config_remove_category",
            {"scope": "automation", "category_id": category_id},
        )
