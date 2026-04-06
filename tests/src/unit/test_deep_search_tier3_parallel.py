"""Unit tests for parallel Tier 3 config fetching in deep_search.

Validates that when bulk config fetches fail (Tier 1 & 2), Tier 3 fetches
configs in parallel batches without name-score prioritization. This ensures
entities referenced only inside automation conditions/actions (not in the
automation name) are still found. Regression test for #879.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp.tools.smart_search import SmartSearchTools


def _make_tools(client):
    """Create SmartSearchTools with mocked global settings."""
    with patch("ha_mcp.tools.smart_search.get_global_settings") as mock_settings:
        mock_settings.return_value.fuzzy_threshold = 60
        return SmartSearchTools(client=client)


def _make_entity(entity_id: str, friendly_name: str) -> dict:
    return {
        "entity_id": entity_id,
        "state": "on",
        "attributes": {"friendly_name": friendly_name},
    }


def _make_automation_entities(count: int) -> list[dict]:
    """Create automation entities with unique IDs in attributes."""
    return [
        {
            "entity_id": f"automation.auto_{i}",
            "state": "on",
            "attributes": {
                "friendly_name": f"Automation {i}",
                "id": f"uid_{i}",
            },
        }
        for i in range(count)
    ]


class TestTier3ParallelFetch:
    """Test that Tier 3 fetches configs in parallel without name-score prioritization."""

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.get_config = AsyncMock(return_value={"time_zone": "UTC"})
        # Bulk fetch fails (triggers Tier 3)
        client._request = AsyncMock(side_effect=Exception("Bulk fetch unavailable"))
        client.send_websocket_message = AsyncMock(
            side_effect=Exception("WebSocket unavailable")
        )
        return client

    @pytest.fixture
    def smart_tools(self, mock_client):
        return _make_tools(mock_client)

    @pytest.mark.asyncio
    async def test_tier3_fetches_all_configs_not_just_name_matches(
        self, mock_client, smart_tools
    ):
        """Configs for ALL automations should be fetched, not just name-matched ones.

        Regression test for #879: an automation named "Morning Routine" that
        references "sensor.kitchen_temp" in its conditions should be found when
        searching for "kitchen_temp", even though the name doesn't match.
        """
        # Create automations: only auto_2 has the search term in its config,
        # but its NAME doesn't match the query at all.
        automations = [
            {
                "entity_id": "automation.morning_routine",
                "state": "on",
                "attributes": {
                    "friendly_name": "Morning Routine",
                    "id": "uid_morning",
                },
            },
            {
                "entity_id": "automation.evening_lights",
                "state": "on",
                "attributes": {
                    "friendly_name": "Evening Lights",
                    "id": "uid_evening",
                },
            },
        ]

        all_entities = automations + [
            _make_entity("sensor.kitchen_temp", "Kitchen Temperature"),
        ]

        mock_client.get_states = AsyncMock(return_value=all_entities)

        # Track which UIDs get fetched
        fetched_uids = []

        async def _individual_fetch(method: str, url: str) -> dict:
            uid = url.split("/")[-1]
            fetched_uids.append(uid)
            # "Morning Routine" references sensor.kitchen_temp in condition
            if uid == "uid_morning":
                return {
                    "id": uid,
                    "trigger": [{"platform": "time", "at": "07:00"}],
                    "condition": [
                        {
                            "condition": "numeric_state",
                            "entity_id": "sensor.kitchen_temp",
                            "below": 20,
                        }
                    ],
                    "action": [{"service": "light.turn_on"}],
                }
            return {
                "id": uid,
                "trigger": [{"platform": "time", "at": "18:00"}],
                "action": [{"service": "light.turn_on", "target": {"entity_id": "light.living_room"}}],
            }

        mock_client._request = AsyncMock(side_effect=_individual_fetch)
        # Keep WebSocket failing to force Tier 3
        mock_client.send_websocket_message = AsyncMock(
            side_effect=Exception("WebSocket unavailable")
        )

        result = await smart_tools.deep_search(
            query="kitchen_temp",
            search_types=["automation"],
            limit=10,
        )

        # Both UIDs should have been fetched (not just name-matched ones)
        assert "uid_morning" in fetched_uids, (
            "Morning Routine config should be fetched even though "
            "its name doesn't match 'kitchen_temp'"
        )
        assert "uid_evening" in fetched_uids, (
            "All automation configs should be fetched in Tier 3"
        )

        # The search should find the automation that references kitchen_temp
        auto_results = result.get("automations", [])
        matched_ids = [r["entity_id"] for r in auto_results]
        assert "automation.morning_routine" in matched_ids, (
            f"Should find automation referencing kitchen_temp in conditions. "
            f"Got: {matched_ids}"
        )

    @pytest.mark.asyncio
    async def test_tier3_respects_time_budget(self, mock_client, smart_tools):
        """Tier 3 should stop fetching when time budget is exhausted."""
        automations = _make_automation_entities(30)
        mock_client.get_states = AsyncMock(return_value=automations)

        call_count = 0

        async def _slow_fetch(method: str, url: str) -> dict:
            nonlocal call_count
            uid = url.split("/")[-1]
            # First call triggers bulk fetch failure
            if url.rstrip("/") == "/config/automation/config":
                raise Exception("Bulk unavailable")
            call_count += 1
            await asyncio.sleep(1.5)  # Simulate slow fetches
            return {"id": uid, "action": []}

        mock_client._request = AsyncMock(side_effect=_slow_fetch)
        mock_client.send_websocket_message = AsyncMock(
            side_effect=Exception("WebSocket unavailable")
        )

        with patch(
            "ha_mcp.tools.smart_search.AUTOMATION_CONFIG_TIME_BUDGET", 2.0
        ):
            result = await smart_tools.deep_search(
                query="test",
                search_types=["automation"],
                limit=10,
            )

        # With 2s budget and 1.5s per fetch (parallel batches of 10),
        # batch 1 (t=0→1.5s) completes under budget, batch 2 (t=1.5→3.0s)
        # may start but batch 3 is skipped. Expect 10-20 fetched.
        assert call_count < 30, (
            f"Should stop before fetching all 30, but fetched {call_count}"
        )
        assert call_count >= 10, (
            f"Should complete at least one full batch of 10, but only fetched {call_count}"
        )
