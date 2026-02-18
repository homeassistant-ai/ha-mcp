"""
Tests for ha_get_overview persistent notification integration.

Verifies that ha_get_overview includes notification_count at all detail levels,
and that the include_notifications parameter controls notification fetching.
"""

import logging

import pytest

from ..utilities.assertions import assert_mcp_success

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
async def test_overview_includes_notification_count(mcp_client):
    """Test that ha_get_overview returns notification_count at minimal detail level."""
    logger.info("Testing ha_get_overview includes notification_count")

    result = await mcp_client.call_tool(
        "ha_get_overview",
        {"detail_level": "minimal"},
    )
    raw_data = assert_mcp_success(result, "Overview with notifications")
    data = raw_data.get("data", raw_data)

    assert "notification_count" in data, (
        "Expected 'notification_count' in overview response"
    )
    assert isinstance(data["notification_count"], int)
    logger.info(f"notification_count: {data['notification_count']}")

    # If notifications exist, verify the array structure
    if data["notification_count"] > 0:
        assert "notifications" in data
        for notif in data["notifications"]:
            assert "notification_id" in notif
            assert "title" in notif
            assert "message" in notif
            assert "created_at" in notif


@pytest.mark.asyncio
async def test_overview_notifications_at_all_detail_levels(mcp_client):
    """Test that notification_count appears at all detail levels."""
    for level in ("minimal", "standard", "full"):
        logger.info(f"Testing notification_count at detail_level={level}")

        result = await mcp_client.call_tool(
            "ha_get_overview",
            {"detail_level": level},
        )
        raw_data = assert_mcp_success(result, f"Overview at {level}")
        data = raw_data.get("data", raw_data)

        assert "notification_count" in data, (
            f"Expected 'notification_count' at detail_level={level}"
        )


@pytest.mark.asyncio
async def test_overview_exclude_notifications(mcp_client):
    """Test that include_notifications=False omits notification data."""
    logger.info("Testing ha_get_overview with include_notifications=False")

    result = await mcp_client.call_tool(
        "ha_get_overview",
        {"detail_level": "minimal", "include_notifications": False},
    )
    raw_data = assert_mcp_success(result, "Overview without notifications")
    data = raw_data.get("data", raw_data)

    assert "notification_count" not in data, (
        "Expected no 'notification_count' when include_notifications=False"
    )
    assert "notifications" not in data, (
        "Expected no 'notifications' when include_notifications=False"
    )


@pytest.mark.asyncio
async def test_overview_notification_lifecycle(mcp_client):
    """Test that creating and dismissing a notification is reflected in the overview."""
    logger.info("Testing notification lifecycle via overview")

    # Create a test notification
    create_result = await mcp_client.call_tool(
        "ha_call_service",
        {
            "domain": "persistent_notification",
            "service": "create",
            "service_data": {
                "title": "Test Notification",
                "message": "This is a test notification for e2e",
                "notification_id": "e2e_test_notification",
            },
        },
    )
    assert_mcp_success(create_result, "Create test notification")

    # Verify it appears in the overview
    result = await mcp_client.call_tool(
        "ha_get_overview",
        {"detail_level": "minimal"},
    )
    raw_data = assert_mcp_success(result, "Overview after creating notification")
    data = raw_data.get("data", raw_data)

    assert data["notification_count"] > 0, "Expected at least 1 notification"
    assert "notifications" in data

    found = any(
        n["notification_id"] == "e2e_test_notification"
        for n in data["notifications"]
    )
    assert found, "Expected to find the test notification in overview"

    # Dismiss the notification
    dismiss_result = await mcp_client.call_tool(
        "ha_call_service",
        {
            "domain": "persistent_notification",
            "service": "dismiss",
            "service_data": {
                "notification_id": "e2e_test_notification",
            },
        },
    )
    assert_mcp_success(dismiss_result, "Dismiss test notification")

    # Verify it's gone
    result2 = await mcp_client.call_tool(
        "ha_get_overview",
        {"detail_level": "minimal"},
    )
    raw_data2 = assert_mcp_success(result2, "Overview after dismissing notification")
    data2 = raw_data2.get("data", raw_data2)

    if data2.get("notifications"):
        not_found = all(
            n["notification_id"] != "e2e_test_notification"
            for n in data2["notifications"]
        )
        assert not_found, "Test notification should be dismissed"
