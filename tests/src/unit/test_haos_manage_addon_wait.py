"""Deadline regression tests for the HAOS app-state polling helper."""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from tests.src.e2e.haos_only.test_manage_addon_modes import _wait_addon_state


async def test_zero_timeout_does_not_poll_addon_state() -> None:
    """An exhausted app-state budget performs no MCP request."""
    client = Mock()
    client.call_tool = AsyncMock()

    with (
        patch(
            "tests.src.e2e.haos_only.test_manage_addon_modes.time.monotonic",
            return_value=5.0,
        ),
        patch(
            "tests.src.e2e.haos_only.test_manage_addon_modes.pytest.fail",
            side_effect=AssertionError("poll timed out"),
        ),
        pytest.raises(AssertionError, match="poll timed out"),
    ):
        await _wait_addon_state(
            client,
            "test_slug",
            frozenset({"started"}),
            timeout=0.0,
        )

    client.call_tool.assert_not_awaited()


async def test_addon_state_request_uses_remaining_budget() -> None:
    """The MCP request timeout cannot exceed the poller's remaining time."""
    client = Mock()
    client.call_tool = AsyncMock(return_value={"addon": {"state": "started"}})

    with patch(
        "tests.src.e2e.haos_only.test_manage_addon_modes.time.monotonic",
        side_effect=[0.0, 0.25, 0.5],
    ):
        state = await _wait_addon_state(
            client,
            "test_slug",
            frozenset({"started"}),
            timeout=1.0,
        )

    assert state == "started"
    client.call_tool.assert_awaited_once_with(
        "ha_get_app", {"slug": "test_slug"}, timeout=0.75
    )
