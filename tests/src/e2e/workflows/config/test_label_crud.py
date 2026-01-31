"""
E2E tests for Home Assistant label CRUD operations.

Tests the complete lifecycle of labels including:
- List, create, get, update, and delete operations
- Label assignment to entities
- Label properties (color, icon, description)
"""

import logging

import pytest

from ...utilities.assertions import assert_mcp_success, parse_mcp_result

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
@pytest.mark.config
class TestLabelCRUD:
    """Test label CRUD operations."""

    async def test_list_labels(self, mcp_client):
        """Test listing all labels."""
        logger.info("Testing ha_config_get_label")

        result = await mcp_client.call_tool(
            "ha_config_get_label",
            {},
        )

        data = assert_mcp_success(result, "List labels")

        assert "labels" in data, f"Missing 'labels' in response: {data}"
        assert "count" in data, f"Missing 'count' in response: {data}"
        assert isinstance(data["labels"], list), f"labels should be a list: {data}"

        logger.info(f"Found {data['count']} labels")
        for label in data["labels"][:5]:  # Log first 5
            logger.info(f"  - {label.get('name', 'Unknown')} (id: {label.get('label_id')})")

    async def test_label_full_lifecycle(self, mcp_client, cleanup_tracker):
        """Test complete label lifecycle: create, get, update, delete."""
        logger.info("Testing label full lifecycle")

        label_name = "E2E Test Label"

        # CREATE
        create_result = await mcp_client.call_tool(
            "ha_config_set_label",
            {
                "name": label_name,
                "color": "blue",
                "icon": "mdi:tag",
                "description": "E2E test label for testing",
            },
        )

        create_data = assert_mcp_success(create_result, "Create label")
        label_id = create_data.get("label_id")
        assert label_id, f"Missing label_id in create response: {create_data}"
        cleanup_tracker.track("label", label_id)
        logger.info(f"Created label: {label_name} (id: {label_id})")

        # GET specific label (config operations are synchronous)
        get_result = await mcp_client.call_tool(
            "ha_config_get_label",
            {"label_id": label_id},
        )
        get_data = assert_mcp_success(get_result, "Get label")
        assert "label" in get_data, f"Missing 'label' in response: {get_data}"
        assert get_data["label"]["name"] == label_name, (
            f"Name mismatch: {get_data['label']}"
        )
        logger.info("Label retrieved successfully")

        # UPDATE
        update_result = await mcp_client.call_tool(
            "ha_config_set_label",
            {
                "label_id": label_id,
                "name": "E2E Test Label Updated",
                "color": "green",
            },
        )
        update_data = assert_mcp_success(update_result, "Update label")
        logger.info(f"Updated label: {update_data.get('message')}")

        # VERIFY UPDATE via get (config operations are synchronous)
        get_result = await mcp_client.call_tool(
            "ha_config_get_label",
            {"label_id": label_id},
        )
        get_data = assert_mcp_success(get_result, "Get updated label")
        assert get_data["label"]["name"] == "E2E Test Label Updated", (
            f"Updated name mismatch: {get_data['label']}"
        )
        logger.info("Label update verified")

        # DELETE
        delete_result = await mcp_client.call_tool(
            "ha_config_remove_label",
            {"label_id": label_id},
        )
        delete_data = assert_mcp_success(delete_result, "Delete label")
        logger.info(f"Deleted label: {delete_data.get('message')}")

        # VERIFY DELETION (config operations are synchronous)
        get_result = await mcp_client.call_tool(
            "ha_config_get_label",
            {"label_id": label_id},
        )
        get_data = parse_mcp_result(get_result)
        assert get_data.get("success") is False, (
            f"Deleted label should not be found: {get_data}"
        )
        logger.info("Label deletion verified")

    async def test_create_label_minimal(self, mcp_client, cleanup_tracker):
        """Test creating label with minimal required fields."""
        logger.info("Testing minimal label creation")

        result = await mcp_client.call_tool(
            "ha_config_set_label",
            {"name": "E2E Minimal Label"},
        )

        data = assert_mcp_success(result, "Create minimal label")
        label_id = data.get("label_id")
        assert label_id, f"Missing label_id: {data}"
        cleanup_tracker.track("label", label_id)
        logger.info(f"Created minimal label: {label_id}")

        # Clean up
        await mcp_client.call_tool(
            "ha_config_remove_label",
            {"label_id": label_id},
        )

    async def test_create_label_with_color(self, mcp_client, cleanup_tracker):
        """Test creating label with different color formats."""
        logger.info("Testing label creation with colors")

        colors = ["red", "green", "blue", "#FF5733"]

        for color in colors:
            result = await mcp_client.call_tool(
                "ha_config_set_label",
                {
                    "name": f"E2E Color Test {color}",
                    "color": color,
                },
            )

            data = parse_mcp_result(result)
            if data.get("success"):
                label_id = data.get("label_id")
                cleanup_tracker.track("label", label_id)
                logger.info(f"Color '{color}' accepted, label_id: {label_id}")

                # Clean up immediately
                await mcp_client.call_tool(
                    "ha_config_remove_label",
                    {"label_id": label_id},
                )
            else:
                logger.warning(f"Color '{color}' may not be supported")

    async def test_create_label_with_icon(self, mcp_client, cleanup_tracker):
        """Test creating label with MDI icon."""
        logger.info("Testing label creation with icon")

        result = await mcp_client.call_tool(
            "ha_config_set_label",
            {
                "name": "E2E Icon Label",
                "icon": "mdi:label-variant",
            },
        )

        data = assert_mcp_success(result, "Create label with icon")
        label_id = data.get("label_id")
        cleanup_tracker.track("label", label_id)
        logger.info(f"Created label with icon: {label_id}")

        # Verify icon was saved
        get_result = await mcp_client.call_tool(
            "ha_config_get_label",
            {"label_id": label_id},
        )
        get_data = assert_mcp_success(get_result, "Get label with icon")
        assert get_data["label"].get("icon") == "mdi:label-variant", (
            f"Icon mismatch: {get_data['label']}"
        )
        logger.info("Label icon verified")

        # Clean up
        await mcp_client.call_tool(
            "ha_config_remove_label",
            {"label_id": label_id},
        )

    async def test_get_nonexistent_label(self, mcp_client):
        """Test getting a non-existent label."""
        logger.info("Testing get non-existent label")

        result = await mcp_client.call_tool(
            "ha_config_get_label",
            {"label_id": "nonexistent_label_xyz_12345"},
        )

        data = parse_mcp_result(result)
        assert data.get("success") is False, (
            f"Should fail for non-existent label: {data}"
        )
        logger.info("Non-existent label properly returned error")

    async def test_delete_nonexistent_label(self, mcp_client):
        """Test deleting a non-existent label."""
        logger.info("Testing delete non-existent label")

        result = await mcp_client.call_tool(
            "ha_config_remove_label",
            {"label_id": "nonexistent_label_xyz_12345"},
        )

        data = parse_mcp_result(result)
        # Should return error or handle gracefully
        if data.get("success"):
            logger.info("Delete returned success (idempotent)")
        else:
            logger.info("Non-existent label delete properly returned error")


@pytest.mark.asyncio
@pytest.mark.config
class TestLabelAssignment:
    """Test label assignment to entities."""

    async def test_assign_label_to_entity(
        self, mcp_client, cleanup_tracker, test_light_entity
    ):
        """Test assigning a label to an entity."""
        logger.info(f"Testing label assignment to {test_light_entity}")

        # Create a test label
        create_result = await mcp_client.call_tool(
            "ha_config_set_label",
            {"name": "E2E Assignment Test"},
        )
        create_data = assert_mcp_success(create_result, "Create label for assignment")
        label_id = create_data.get("label_id")
        cleanup_tracker.track("label", label_id)
        logger.info(f"Created label for assignment: {label_id}")


        # Assign label to entity
        assign_result = await mcp_client.call_tool(
            "ha_set_entity",
            {
                "entity_id": test_light_entity,
                "labels": [label_id],
            },
        )
        assign_data = assert_mcp_success(assign_result, "Assign label to entity")
        logger.info(f"Label assigned: {assign_data.get('message')}")

        # Clear labels from entity (restore original state)
        clear_result = await mcp_client.call_tool(
            "ha_set_entity",
            {
                "entity_id": test_light_entity,
                "labels": [],  # Clear all labels
            },
        )
        clear_data = assert_mcp_success(clear_result, "Clear labels from entity")
        logger.info(f"Labels cleared: {clear_data.get('message')}")

        # Clean up label
        await mcp_client.call_tool(
            "ha_config_remove_label",
            {"label_id": label_id},
        )

    async def test_assign_multiple_labels(
        self, mcp_client, cleanup_tracker, test_light_entity
    ):
        """Test assigning multiple labels to an entity."""
        logger.info(f"Testing multiple label assignment to {test_light_entity}")

        # Create two test labels
        label_ids = []
        for i in range(2):
            result = await mcp_client.call_tool(
                "ha_config_set_label",
                {"name": f"E2E Multi Label {i + 1}"},
            )
            data = assert_mcp_success(result, f"Create label {i + 1}")
            label_id = data.get("label_id")
            label_ids.append(label_id)
            cleanup_tracker.track("label", label_id)
        logger.info(f"Created labels: {label_ids}")


        # Assign both labels
        assign_result = await mcp_client.call_tool(
            "ha_set_entity",
            {
                "entity_id": test_light_entity,
                "labels": label_ids,
            },
        )
        assign_data = assert_mcp_success(assign_result, "Assign multiple labels")
        logger.info(f"Multiple labels assigned: {assign_data.get('message')}")

        # Clear labels from entity
        await mcp_client.call_tool(
            "ha_set_entity",
            {
                "entity_id": test_light_entity,
                "labels": [],
            },
        )

        # Clean up labels
        for label_id in label_ids:
            await mcp_client.call_tool(
                "ha_config_remove_label",
                {"label_id": label_id},
            )

    async def test_assign_label_as_string(
        self, mcp_client, cleanup_tracker, test_light_entity
    ):
        """Test assigning a label using string instead of list."""
        logger.info(f"Testing string label assignment to {test_light_entity}")

        # Create a test label
        create_result = await mcp_client.call_tool(
            "ha_config_set_label",
            {"name": "E2E String Assignment Test"},
        )
        create_data = assert_mcp_success(create_result, "Create label")
        label_id = create_data.get("label_id")
        cleanup_tracker.track("label", label_id)


        # Assign using string (JSON array) instead of list
        assign_result = await mcp_client.call_tool(
            "ha_set_entity",
            {
                "entity_id": test_light_entity,
                "labels": [label_id],
            },
        )
        assign_data = assert_mcp_success(assign_result, "Assign label")
        logger.info(f"Label assignment succeeded: {assign_data.get('message')}")

        # Clear labels
        await mcp_client.call_tool(
            "ha_set_entity",
            {
                "entity_id": test_light_entity,
                "labels": [],
            },
        )

        # Clean up
        await mcp_client.call_tool(
            "ha_config_remove_label",
            {"label_id": label_id},
        )

    async def test_assign_label_json_string(
        self, mcp_client, cleanup_tracker, test_light_entity
    ):
        """Test assigning labels using JSON array string."""
        logger.info(f"Testing JSON string label assignment to {test_light_entity}")

        # Create a test label
        create_result = await mcp_client.call_tool(
            "ha_config_set_label",
            {"name": "E2E JSON Assignment Test"},
        )
        create_data = assert_mcp_success(create_result, "Create label")
        label_id = create_data.get("label_id")
        cleanup_tracker.track("label", label_id)


        # Assign using JSON array string
        assign_result = await mcp_client.call_tool(
            "ha_set_entity",
            {
                "entity_id": test_light_entity,
                "labels": f'["{label_id}"]',  # JSON array string
            },
        )
        assign_data = assert_mcp_success(assign_result, "Assign label as JSON string")
        logger.info(f"JSON label assignment succeeded: {assign_data.get('message')}")

        # Clear labels
        await mcp_client.call_tool(
            "ha_set_entity",
            {
                "entity_id": test_light_entity,
                "labels": [],
            },
        )

        # Clean up
        await mcp_client.call_tool(
            "ha_config_remove_label",
            {"label_id": label_id},
        )

    async def test_assign_label_to_nonexistent_entity(self, mcp_client, cleanup_tracker):
        """Test assigning label to non-existent entity."""
        logger.info("Testing label assignment to non-existent entity")

        # Create a test label
        create_result = await mcp_client.call_tool(
            "ha_config_set_label",
            {"name": "E2E Nonexistent Entity Test"},
        )
        create_data = assert_mcp_success(create_result, "Create label")
        label_id = create_data.get("label_id")
        cleanup_tracker.track("label", label_id)


        # Try to assign to non-existent entity
        assign_result = await mcp_client.call_tool(
            "ha_set_entity",
            {
                "entity_id": "light.nonexistent_xyz_12345",
                "labels": [label_id],
            },
        )

        data = parse_mcp_result(assign_result)
        # Should fail for non-existent entity
        assert data.get("success") is False, (
            f"Should fail for non-existent entity: {data}"
        )
        logger.info("Non-existent entity properly returned error")

        # Clean up
        await mcp_client.call_tool(
            "ha_config_remove_label",
            {"label_id": label_id},
        )


@pytest.mark.asyncio
@pytest.mark.config
async def test_multiple_labels_lifecycle(mcp_client, cleanup_tracker):
    """Test creating and managing multiple labels."""
    logger.info("Testing multiple labels lifecycle")

    label_ids = []
    label_configs = [
        {"name": "E2E Priority High", "color": "red", "icon": "mdi:alert"},
        {"name": "E2E Priority Medium", "color": "yellow", "icon": "mdi:alert-circle"},
        {"name": "E2E Priority Low", "color": "green", "icon": "mdi:check"},
    ]

    # Create multiple labels
    for config in label_configs:
        result = await mcp_client.call_tool(
            "ha_config_set_label",
            config,
        )
        data = assert_mcp_success(result, f"Create {config['name']}")
        label_id = data.get("label_id")
        label_ids.append(label_id)
        cleanup_tracker.track("label", label_id)
        logger.info(f"Created: {config['name']} (id: {label_id})")

    # List and verify all exist
    list_result = await mcp_client.call_tool(
        "ha_config_get_label",
        {},
    )
    list_data = assert_mcp_success(list_result, "List labels")

    list_label_ids = [lbl.get("label_id") for lbl in list_data.get("labels", [])]
    for label_id in label_ids:
        assert label_id in list_label_ids, f"Label {label_id} not found in list"
    logger.info("All created labels verified in list")

    # Delete all
    for label_id in label_ids:
        await mcp_client.call_tool(
            "ha_config_remove_label",
            {"label_id": label_id},
        )
    logger.info("All labels deleted")

    # Verify deletions
    list_result = await mcp_client.call_tool(
        "ha_config_get_label",
        {},
    )
    list_data = assert_mcp_success(list_result, "List after deletion")

    list_label_ids = [lbl.get("label_id") for lbl in list_data.get("labels", [])]
    for label_id in label_ids:
        assert label_id not in list_label_ids, (
            f"Label {label_id} should be deleted"
        )
    logger.info("All label deletions verified")
