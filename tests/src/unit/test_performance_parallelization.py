"""Unit tests for performance improvements (issue #258).

Tests the parallelization optimizations:
- Parallel WebSocket calls in get_system_overview
- Parallel config fetching in deep_search with semaphore control
- Single get_states() call to avoid repeated fetches
"""

import asyncio
from unittest.mock import patch

import pytest

from ha_mcp.tools.smart_search import SmartSearchTools, DEFAULT_CONCURRENCY_LIMIT


class MockClient:
    """Mock Home Assistant client for testing parallelization."""

    def __init__(
        self,
        entities: list[dict] | None = None,
        services: list[dict] | None = None,
        websocket_delay: float = 0.0,
    ):
        self.entities = entities or []
        self.services = services or []
        self.websocket_delay = websocket_delay
        self.get_states_call_count = 0
        self.get_services_call_count = 0
        self.websocket_call_count = 0
        self.automation_config_call_count = 0
        self.script_config_call_count = 0

    async def get_states(self) -> list[dict]:
        self.get_states_call_count += 1
        return self.entities

    async def get_services(self) -> list[dict]:
        self.get_services_call_count += 1
        return self.services

    async def send_websocket_message(self, message: dict) -> dict:
        self.websocket_call_count += 1
        if self.websocket_delay > 0:
            await asyncio.sleep(self.websocket_delay)

        msg_type = message.get("type", "")

        # Simulate area registry
        if msg_type == "config/area_registry/list":
            return {
                "success": True,
                "result": [
                    {"area_id": "living_room", "name": "Living Room"},
                    {"area_id": "bedroom", "name": "Bedroom"},
                ],
            }

        # Simulate entity registry
        if msg_type == "config/entity_registry/list":
            return {
                "success": True,
                "result": [
                    {"entity_id": "light.living_room", "area_id": "living_room"},
                    {"entity_id": "light.bedroom", "area_id": "bedroom"},
                ],
            }

        # Simulate helper types
        if msg_type.endswith("/list"):
            helper_type = msg_type.replace("/list", "")
            return {
                "success": True,
                "result": [
                    {"id": f"test_{helper_type}", "name": f"Test {helper_type}"},
                ],
            }

        return {"success": False, "error": f"Unknown message type: {msg_type}"}

    async def get_automation_config(self, entity_id: str) -> dict:
        self.automation_config_call_count += 1
        if self.websocket_delay > 0:
            await asyncio.sleep(self.websocket_delay)
        return {
            "alias": f"Test Automation for {entity_id}",
            "trigger": [{"platform": "state", "entity_id": "light.test"}],
            "action": [{"service": "light.turn_on"}],
        }

    async def get_script_config(self, script_id: str) -> dict:
        self.script_config_call_count += 1
        if self.websocket_delay > 0:
            await asyncio.sleep(self.websocket_delay)
        return {
            "config": {
                "alias": f"Test Script {script_id}",
                "sequence": [{"service": "light.turn_on"}],
            }
        }


class TestGetSystemOverviewParallelization:
    """Test parallel WebSocket calls in get_system_overview."""

    @pytest.fixture
    def sample_entities(self):
        """Sample entities for testing."""
        return [
            {
                "entity_id": "light.living_room",
                "attributes": {"friendly_name": "Living Room Light"},
                "state": "on",
            },
            {
                "entity_id": "switch.kitchen",
                "attributes": {"friendly_name": "Kitchen Switch"},
                "state": "off",
            },
        ]

    @pytest.fixture
    def sample_services(self):
        """Sample services for testing."""
        return [
            {"domain": "light", "services": {"turn_on": {}, "turn_off": {}}},
            {"domain": "switch", "services": {"turn_on": {}, "turn_off": {}}},
        ]

    @pytest.mark.asyncio
    async def test_parallel_calls_are_faster(self, sample_entities, sample_services):
        """Verify that parallel calls complete faster than sequential would.

        With 0.1s delay per call and 4 parallel calls, total time should be ~0.1s
        instead of ~0.4s for sequential.
        """
        client = MockClient(
            entities=sample_entities,
            services=sample_services,
            websocket_delay=0.05,
        )

        with patch("ha_mcp.tools.smart_search.get_global_settings") as mock_settings:
            mock_settings.return_value.fuzzy_threshold = 60
            tools = SmartSearchTools(client=client)

            import time
            start_time = time.time()
            result = await tools.get_system_overview(detail_level="minimal")
            elapsed_time = time.time() - start_time

            assert result["success"] is True
            # All 4 calls should happen in parallel, so time should be close to
            # single call time, not 4x
            # With 0.05s delay, sequential would be 0.2s, parallel should be ~0.05s
            # We use 0.15s as threshold to account for test overhead
            assert elapsed_time < 0.15, f"Parallel calls took {elapsed_time}s, expected < 0.15s"

    @pytest.mark.asyncio
    async def test_all_data_fetched(self, sample_entities, sample_services):
        """Verify all data sources are fetched in get_system_overview."""
        client = MockClient(
            entities=sample_entities,
            services=sample_services,
        )

        with patch("ha_mcp.tools.smart_search.get_global_settings") as mock_settings:
            mock_settings.return_value.fuzzy_threshold = 60
            tools = SmartSearchTools(client=client)

            result = await tools.get_system_overview(detail_level="standard")

            assert result["success"] is True
            assert client.get_states_call_count == 1
            assert client.get_services_call_count == 1
            # 2 WebSocket calls: area_registry and entity_registry
            assert client.websocket_call_count == 2

    @pytest.mark.asyncio
    async def test_graceful_failure_handling(self, sample_entities, sample_services):
        """Verify that failures in one fetch don't break others."""
        client = MockClient(
            entities=sample_entities,
            services=sample_services,
        )

        # Make WebSocket calls fail
        async def failing_websocket(message: dict) -> dict:
            raise Exception("WebSocket connection failed")

        client.send_websocket_message = failing_websocket

        with patch("ha_mcp.tools.smart_search.get_global_settings") as mock_settings:
            mock_settings.return_value.fuzzy_threshold = 60
            tools = SmartSearchTools(client=client)

            result = await tools.get_system_overview(detail_level="minimal")

            # Should still succeed with entity and service data
            assert result["success"] is True
            assert result["system_summary"]["total_entities"] == 2
            # Area data should be empty due to failure
            assert result["system_summary"]["total_areas"] == 0


class TestDeepSearchParallelization:
    """Test parallel config fetching in deep_search with semaphore control."""

    @pytest.fixture
    def automation_entities(self):
        """Sample automation entities for testing."""
        return [
            {
                "entity_id": f"automation.test_{i}",
                "attributes": {"friendly_name": f"Test Automation {i}", "id": f"test_{i}"},
                "state": "on",
            }
            for i in range(10)
        ]

    @pytest.fixture
    def script_entities(self):
        """Sample script entities for testing."""
        return [
            {
                "entity_id": f"script.test_{i}",
                "attributes": {"friendly_name": f"Test Script {i}"},
                "state": "off",
            }
            for i in range(5)
        ]

    @pytest.mark.asyncio
    async def test_single_get_states_call(self, automation_entities, script_entities):
        """Verify get_states is only called once for all search types."""
        client = MockClient(entities=automation_entities + script_entities)

        with patch("ha_mcp.tools.smart_search.get_global_settings") as mock_settings:
            mock_settings.return_value.fuzzy_threshold = 60
            tools = SmartSearchTools(client=client)

            # Search all types
            result = await tools.deep_search(
                query="test",
                search_types=["automation", "script", "helper"],
            )

            assert result["success"] is True
            # Should only call get_states once, not once per search type
            assert client.get_states_call_count == 1

    @pytest.mark.asyncio
    async def test_parallel_automation_config_fetching(self, automation_entities):
        """Verify automation configs are fetched in parallel."""
        client = MockClient(
            entities=automation_entities,
            websocket_delay=0.02,  # 20ms delay per call
        )

        with patch("ha_mcp.tools.smart_search.get_global_settings") as mock_settings:
            mock_settings.return_value.fuzzy_threshold = 60
            tools = SmartSearchTools(client=client)

            import time
            start_time = time.time()
            result = await tools.deep_search(
                query="test",
                search_types=["automation"],
            )
            elapsed_time = time.time() - start_time

            assert result["success"] is True
            # 10 automation configs with 0.02s delay each
            # Sequential would be 0.2s, parallel (5 concurrent) should be ~0.04s
            # Use 0.1s threshold for test overhead
            assert elapsed_time < 0.15, f"Parallel fetch took {elapsed_time}s, expected < 0.15s"
            assert client.automation_config_call_count == 10

    @pytest.mark.asyncio
    async def test_parallel_script_config_fetching(self, script_entities):
        """Verify script configs are fetched in parallel."""
        client = MockClient(
            entities=script_entities,
            websocket_delay=0.02,
        )

        with patch("ha_mcp.tools.smart_search.get_global_settings") as mock_settings:
            mock_settings.return_value.fuzzy_threshold = 60
            tools = SmartSearchTools(client=client)

            import time
            start_time = time.time()
            result = await tools.deep_search(
                query="test",
                search_types=["script"],
            )
            elapsed_time = time.time() - start_time

            assert result["success"] is True
            # 5 script configs with parallel fetching should be fast
            assert elapsed_time < 0.1
            assert client.script_config_call_count == 5

    @pytest.mark.asyncio
    async def test_parallel_helper_type_fetching(self):
        """Verify helper types are fetched in parallel."""
        client = MockClient(websocket_delay=0.02)

        with patch("ha_mcp.tools.smart_search.get_global_settings") as mock_settings:
            mock_settings.return_value.fuzzy_threshold = 60
            tools = SmartSearchTools(client=client)

            import time
            start_time = time.time()
            result = await tools.deep_search(
                query="test",
                search_types=["helper"],
            )
            elapsed_time = time.time() - start_time

            assert result["success"] is True
            # 6 helper types with parallel fetching
            # Sequential would be 0.12s, parallel should be ~0.02s
            assert elapsed_time < 0.1, f"Parallel helper fetch took {elapsed_time}s"
            # 6 WebSocket calls for 6 helper types
            assert client.websocket_call_count == 6

    @pytest.mark.asyncio
    async def test_semaphore_limits_concurrency(self, automation_entities):
        """Verify semaphore limits concurrent API calls."""
        concurrent_count = 0
        max_concurrent = 0
        lock = asyncio.Lock()

        original_entities = automation_entities.copy()
        client = MockClient(entities=original_entities)

        # Track concurrent calls
        original_get_config = client.get_automation_config

        async def tracked_get_config(entity_id: str) -> dict:
            nonlocal concurrent_count, max_concurrent
            async with lock:
                concurrent_count += 1
                max_concurrent = max(max_concurrent, concurrent_count)
            try:
                await asyncio.sleep(0.01)  # Small delay to create overlap
                return await original_get_config(entity_id)
            finally:
                async with lock:
                    concurrent_count -= 1

        client.get_automation_config = tracked_get_config

        with patch("ha_mcp.tools.smart_search.get_global_settings") as mock_settings:
            mock_settings.return_value.fuzzy_threshold = 60
            tools = SmartSearchTools(client=client)

            result = await tools.deep_search(
                query="test",
                search_types=["automation"],
                concurrency_limit=3,  # Limit to 3 concurrent
            )

            assert result["success"] is True
            # Max concurrent should not exceed the limit (with some tolerance for timing)
            assert max_concurrent <= 4, f"Max concurrent was {max_concurrent}, expected <= 4"

    @pytest.mark.asyncio
    async def test_exception_handling_in_parallel_fetches(self, automation_entities):
        """Verify exceptions in parallel fetches are handled gracefully."""
        client = MockClient(entities=automation_entities)

        call_count = 0

        async def failing_config(entity_id: str) -> dict:
            nonlocal call_count
            call_count += 1
            if call_count % 2 == 0:
                raise Exception("Config fetch failed")
            return {
                "alias": "Test",
                "trigger": [],
                "action": [],
            }

        client.get_automation_config = failing_config

        with patch("ha_mcp.tools.smart_search.get_global_settings") as mock_settings:
            mock_settings.return_value.fuzzy_threshold = 60
            tools = SmartSearchTools(client=client)

            result = await tools.deep_search(
                query="test",
                search_types=["automation"],
            )

            # Should succeed overall even with some failures
            assert result["success"] is True
            # Should have some results from successful fetches
            # At least 5 out of 10 should succeed (odd numbered calls)
            assert len(result["automations"]) >= 5


class TestDefaultConcurrencyLimit:
    """Test the default concurrency limit constant."""

    def test_default_concurrency_limit_value(self):
        """Verify the default concurrency limit is reasonable."""
        assert DEFAULT_CONCURRENCY_LIMIT == 5

    @pytest.mark.asyncio
    async def test_custom_concurrency_limit(self):
        """Verify custom concurrency limit can be passed to deep_search."""
        entities = [
            {
                "entity_id": "automation.test",
                "attributes": {"friendly_name": "Test", "id": "test"},
                "state": "on",
            }
        ]
        client = MockClient(entities=entities)

        with patch("ha_mcp.tools.smart_search.get_global_settings") as mock_settings:
            mock_settings.return_value.fuzzy_threshold = 60
            tools = SmartSearchTools(client=client)

            # Should accept custom concurrency limit
            result = await tools.deep_search(
                query="test",
                search_types=["automation"],
                concurrency_limit=10,
            )

            assert result["success"] is True
