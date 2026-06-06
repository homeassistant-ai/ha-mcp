"""Unit tests for parallel Attempt C config fetching in deep_search.

Validates that when bulk config fetches fail (Attempts A & B), Attempt C
fetches configs in parallel batches without name-score prioritization. This
ensures entities referenced only inside automation/script conditions/actions
(not in the name) are still found. Regression test for #879.
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


class TestAttemptCParallelFetch:
    """Test that Attempt C fetches configs in parallel without name-score prioritization."""

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
    async def test_attempt_c_fetches_all_configs_not_just_name_matches(
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
                "action": [
                    {
                        "service": "light.turn_on",
                        "target": {"entity_id": "light.living_room"},
                    }
                ],
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
    async def test_attempt_c_respects_time_budget(self, mock_client, smart_tools):
        """Attempt C should stop fetching when time budget is exhausted."""
        automations = _make_automation_entities(30)
        mock_client.get_states = AsyncMock(return_value=automations)

        call_count = 0

        async def _slow_fetch(method: str, url: str) -> dict:
            nonlocal call_count
            uid = url.split("/")[-1]
            if url.rstrip("/") == "/config/automation/config":
                raise Exception("Bulk unavailable")
            call_count += 1
            await asyncio.sleep(0.01)  # Minimal sleep to yield control
            return {"id": uid, "action": []}

        mock_client._request = AsyncMock(side_effect=_slow_fetch)
        mock_client.send_websocket_message = AsyncMock(
            side_effect=Exception("WebSocket unavailable")
        )

        # Budget of 0.005s: batch 1 starts at t=0 (passes check), but by the
        # time it completes (~0.01s), the budget is exceeded so batch 2 is skipped.
        with patch(
            "ha_mcp.tools.smart_search._deep.AUTOMATION_CONFIG_TIME_BUDGET", 0.005
        ):
            await smart_tools.deep_search(
                query="test",
                search_types=["automation"],
                limit=10,
            )

        # Batch 1 (10 items) completes because the budget check happens before
        # launching each batch. Batch 2 at ~0.01s exceeds the 0.005s budget.
        assert call_count == 10, (
            f"Expected exactly one batch of 10, but fetched {call_count}"
        )

    @pytest.mark.parametrize(
        ("search_type", "budget_const_path", "individual_attr"),
        [
            (
                "automation",
                "ha_mcp.tools.smart_search._deep.AUTOMATION_CONFIG_TIME_BUDGET",
                "_request",
            ),
            (
                "script",
                "ha_mcp.tools.smart_search._deep.SCRIPT_CONFIG_TIME_BUDGET",
                "get_script_config",
            ),
            (
                "scene",
                "ha_mcp.tools.smart_search._scenes.SCENE_CONFIG_TIME_BUDGET",
                "get_scene_config",
            ),
        ],
        ids=["automation", "script", "scene"],
    )
    async def test_config_time_budget_param_overrides_env_default(
        self,
        mock_client,
        smart_tools,
        search_type,
        budget_const_path,
        individual_attr,
    ):
        """`config_time_budget=` param replaces the per-type env default for that call.

        Pins the "automation, script, AND scene" promise in the param
        docstring — all three per-type branches feed the same
        ``_individual_fetch_budgeted`` helper, so a regression that
        silently drops the override on one branch would otherwise slip
        through (the previous automation-only assertion missed two of the
        three threads).

        The env-default is patched high enough that without an override the
        test would fetch all three batches; the per-call override is tight
        enough to skip after the first batch (10 fetches). The assertion
        ``call_count == 10`` proves the override was honoured rather than
        silently ignored.
        """
        entities = [
            {
                "entity_id": f"{search_type}.x_{i}",
                "state": "on",
                "attributes": {"friendly_name": f"X {i}", "id": f"uid_{i}"},
            }
            for i in range(30)
        ]
        mock_client.get_states = AsyncMock(return_value=entities)

        call_count = 0

        async def _count_individual() -> dict:
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.01)
            return {"id": "x", "config": {}, "action": []}

        if search_type == "automation":
            # `_request` covers both the bulk-fetch URL (must raise to force
            # Tier 3) and the per-id URL (must count). Discriminate by path.
            async def _auto_request(method: str, url: str) -> dict:
                if url.rstrip("/") == "/config/automation/config":
                    raise Exception("Bulk unavailable")
                return await _count_individual()

            mock_client._request = AsyncMock(side_effect=_auto_request)
        else:
            # Script/scene per-id calls go through a dedicated client method;
            # the fixture's `_request`/`send_websocket_message` exceptions
            # already block their bulk-fetch and registry-walk tiers.
            async def _typed_individual(sid: str) -> dict:
                return await _count_individual()

            setattr(
                mock_client,
                individual_attr,
                AsyncMock(side_effect=_typed_individual),
            )

        # Env default high (would fetch all 3 batches); per-call override
        # is tight (stops after batch 1).
        with patch(budget_const_path, 60.0):
            await smart_tools.deep_search(
                query="test",
                search_types=[search_type],
                limit=10,
                config_time_budget=0.005,
            )

        assert call_count == 10, (
            f"Per-call config_time_budget=0.005 must override the {search_type} "
            f"env default; got {call_count} fetches (expected 10 = single batch)"
        )


class TestAttemptCScriptParallelFetch:
    """Test that Attempt C works for scripts (structurally different from automations)."""

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.get_config = AsyncMock(return_value={"time_zone": "UTC"})
        client._request = AsyncMock(side_effect=Exception("Bulk fetch unavailable"))
        client.send_websocket_message = AsyncMock(
            side_effect=Exception("WebSocket unavailable")
        )
        return client

    @pytest.fixture
    def smart_tools(self, mock_client):
        return _make_tools(mock_client)

    @pytest.mark.asyncio
    async def test_script_attempt_c_fetches_configs(self, mock_client, smart_tools):
        """Script Attempt C should fetch configs and find matches inside them.

        Scripts use a different tuple format (entity_id, friendly_name, script_id,
        name_score) and client.get_script_config() instead of client._request().
        """
        scripts = [
            {
                "entity_id": "script.morning_coffee",
                "state": "off",
                "attributes": {"friendly_name": "Morning Coffee"},
            },
            {
                "entity_id": "script.night_lockup",
                "state": "off",
                "attributes": {"friendly_name": "Night Lockup"},
            },
        ]
        all_entities = scripts + [
            _make_entity("lock.front_door", "Front Door Lock"),
        ]
        mock_client.get_states = AsyncMock(return_value=all_entities)

        fetched_sids = []

        async def _script_config(sid: str) -> dict:
            fetched_sids.append(sid)
            if sid == "night_lockup":
                return {
                    "config": {
                        "sequence": [
                            {
                                "service": "lock.lock",
                                "target": {"entity_id": "lock.front_door"},
                            }
                        ]
                    }
                }
            return {"config": {"sequence": [{"service": "switch.turn_on"}]}}

        mock_client.get_script_config = AsyncMock(side_effect=_script_config)

        result = await smart_tools.deep_search(
            query="front_door",
            search_types=["script"],
            limit=10,
        )

        # Both script configs should have been fetched
        assert len(fetched_sids) == 2, f"Expected 2 script fetches, got {fetched_sids}"

        # Night Lockup references front_door in its sequence
        script_results = result.get("scripts", [])
        matched_ids = [r["entity_id"] for r in script_results]
        assert "script.night_lockup" in matched_ids, (
            f"Should find script referencing front_door. Got: {matched_ids}"
        )
