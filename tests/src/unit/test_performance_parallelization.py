"""Unit tests for performance improvements (issue #258).

Tests observable behavior of performance optimizations:
- get_system_overview returns correct data with parallel fetching
- deep_search returns correct results for automations, scripts, helpers
- Failures in one data source don't break others
- get_states() is not called redundantly
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.smart_search import SmartSearchTools


class MockClient:
    """Mock Home Assistant client that responds to all APIs deep_search may use.

    Supports REST, WebSocket, and legacy per-entity methods so tests
    don't break when the implementation changes its fetch strategy.
    """

    def __init__(
        self,
        entities: list[dict] | None = None,
        services: list[dict] | None = None,
        delay: float = 0.0,
    ):
        self.entities = entities or []
        self.services = services or []
        self.delay = delay
        self.get_states_call_count = 0

    async def get_states(self) -> list[dict]:
        self.get_states_call_count += 1
        return self.entities

    async def get_services(self) -> list[dict]:
        return self.services

    async def _request(self, method: str, path: str) -> list | dict:
        if self.delay > 0:
            await asyncio.sleep(self.delay)

        if path == "/config/automation/config":
            return [
                {
                    "id": e["attributes"]["id"],
                    "alias": e["attributes"]["friendly_name"],
                    "trigger": [{"platform": "state", "entity_id": "light.test"}],
                    "action": [{"service": "light.turn_on"}],
                }
                for e in self.entities
                if e.get("entity_id", "").startswith("automation.")
                and e.get("attributes", {}).get("id")
            ]

        if path == "/config/script/config":
            return [
                {
                    "id": e["entity_id"].replace("script.", ""),
                    "alias": e["attributes"]["friendly_name"],
                    "sequence": [{"service": "light.turn_on"}],
                }
                for e in self.entities
                if e.get("entity_id", "").startswith("script.")
            ]

        if path.startswith("/config/automation/config/"):
            uid = path.split("/")[-1]
            return {
                "id": uid,
                "alias": f"Test Automation {uid}",
                "trigger": [{"platform": "state", "entity_id": "light.test"}],
                "action": [{"service": "light.turn_on"}],
            }

        return {}

    async def send_websocket_message(self, message: dict) -> dict:
        if self.delay > 0:
            await asyncio.sleep(self.delay)

        msg_type = message.get("type", "")

        if msg_type == "config/area_registry/list":
            return {
                "success": True,
                "result": [
                    {"area_id": "living_room", "name": "Living Room"},
                    {"area_id": "bedroom", "name": "Bedroom"},
                ],
            }

        if msg_type == "config/entity_registry/list":
            return {
                "success": True,
                "result": [
                    {"entity_id": "light.living_room", "area_id": "living_room"},
                    {"entity_id": "light.bedroom", "area_id": "bedroom"},
                ],
            }

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
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        return {
            "alias": f"Test Automation for {entity_id}",
            "trigger": [{"platform": "state", "entity_id": "light.test"}],
            "action": [{"service": "light.turn_on"}],
        }

    async def get_script_config(self, script_id: str) -> dict:
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        return {
            "config": {
                "alias": f"Test Script {script_id}",
                "sequence": [{"service": "light.turn_on"}],
            }
        }


def _make_tools(client):
    """Create SmartSearchTools with mocked global settings."""
    with patch("ha_mcp.tools.smart_search.get_global_settings") as mock_settings:
        mock_settings.return_value.fuzzy_threshold = 60
        tools = SmartSearchTools(client=client)
    return tools


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def automation_entities():
    return [
        {
            "entity_id": f"automation.test_{i}",
            "attributes": {"friendly_name": f"Test Automation {i}", "id": f"test_{i}"},
            "state": "on",
        }
        for i in range(10)
    ]


@pytest.fixture
def script_entities():
    return [
        {
            "entity_id": f"script.test_{i}",
            "attributes": {"friendly_name": f"Test Script {i}"},
            "state": "off",
        }
        for i in range(5)
    ]


@pytest.fixture
def sample_entities():
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
def sample_services():
    return [
        {"domain": "light", "services": {"turn_on": {}, "turn_off": {}}},
        {"domain": "switch", "services": {"turn_on": {}, "turn_off": {}}},
    ]


# ---------------------------------------------------------------------------
# get_system_overview
# ---------------------------------------------------------------------------

class TestGetSystemOverview:
    """Test get_system_overview returns correct data."""

    @pytest.mark.asyncio
    async def test_returns_entity_and_area_data(self, sample_entities, sample_services):
        client = MockClient(entities=sample_entities, services=sample_services)

        with patch("ha_mcp.tools.smart_search.get_global_settings") as mock_settings:
            mock_settings.return_value.fuzzy_threshold = 60
            tools = SmartSearchTools(client=client)
            result = await tools.get_system_overview(detail_level="standard")

        assert result["success"] is True
        assert "partial" not in result
        assert result["system_summary"]["total_entities"] == 2
        assert result["system_summary"]["total_areas"] == 2

    @pytest.mark.asyncio
    async def test_parallel_calls_faster_than_sequential(
        self, sample_entities, sample_services
    ):
        """With delays, parallel fetch should be much faster than sequential."""
        client = MockClient(
            entities=sample_entities,
            services=sample_services,
            delay=0.05,
        )

        with patch("ha_mcp.tools.smart_search.get_global_settings") as mock_settings:
            mock_settings.return_value.fuzzy_threshold = 60
            tools = SmartSearchTools(client=client)

            import time
            start = time.time()
            result = await tools.get_system_overview(detail_level="minimal")
            elapsed = time.time() - start

        assert result["success"] is True
        # 5 data sources at 0.05s each: sequential = 0.25s, parallel ≈ 0.05s
        assert elapsed < 0.15, f"Expected parallel speedup, took {elapsed:.2f}s"

    @pytest.mark.asyncio
    async def test_websocket_failure_does_not_break_other_data(
        self, sample_entities, sample_services
    ):
        client = MockClient(entities=sample_entities, services=sample_services)

        async def failing_websocket(message: dict) -> dict:
            raise Exception("WebSocket connection failed")

        client.send_websocket_message = failing_websocket

        with patch("ha_mcp.tools.smart_search.get_global_settings") as mock_settings:
            mock_settings.return_value.fuzzy_threshold = 60
            tools = SmartSearchTools(client=client)
            result = await tools.get_system_overview(detail_level="minimal")

        assert result["success"] is True
        assert result["system_summary"]["total_entities"] == 2
        assert result["system_summary"]["total_areas"] == 0

    @pytest.mark.asyncio
    async def test_entities_fetch_failure_raises_error(self, sample_services):
        """When get_states() fails, surface the error instead of returning 0 entities.

        Regression test for issue #811: ha_get_overview returned success with
        total_entities=0 when the HA connection was broken, masking the real error.
        """
        client = MockClient(entities=[], services=sample_services)
        client.get_states = AsyncMock(side_effect=ConnectionError("Connection refused"))

        with patch("ha_mcp.tools.smart_search.get_global_settings") as mock_settings:
            mock_settings.return_value.fuzzy_threshold = 60
            tools = SmartSearchTools(client=client)
            with pytest.raises(ToolError):
                await tools.get_system_overview(detail_level="minimal")

    @pytest.mark.asyncio
    async def test_services_fetch_failure_continues_with_empty_services(
        self, sample_entities, sample_services
    ):
        """When get_services() fails, the overview still succeeds with total_services=0."""
        client = MockClient(entities=sample_entities, services=sample_services)
        client.get_services = AsyncMock(side_effect=ConnectionError("Connection refused"))

        with patch("ha_mcp.tools.smart_search.get_global_settings") as mock_settings:
            mock_settings.return_value.fuzzy_threshold = 60
            tools = SmartSearchTools(client=client)
            result = await tools.get_system_overview(detail_level="minimal")

        assert result["success"] is True
        assert result["partial"] is True
        assert any("Services unavailable" in w for w in result["warnings"])
        assert result["system_summary"]["total_entities"] == 2
        assert result["system_summary"]["total_services"] == 0

    @pytest.mark.asyncio
    async def test_resolves_area_through_device_registry(self, sample_services):
        """Entities with no direct area_id inherit area from their parent device."""
        entities = [
            {
                "entity_id": "light.kitchen",
                "attributes": {"friendly_name": "Kitchen Light"},
                "state": "on",
            },
        ]
        client = MockClient(entities=entities, services=sample_services)

        # Override websocket to return entity without area_id but with device_id,
        # and a device registry that maps that device to an area.
        original_ws = client.send_websocket_message

        async def ws_with_device_registry(message: dict) -> dict:
            msg_type = message.get("type", "")
            if msg_type == "config/entity_registry/list":
                return {
                    "success": True,
                    "result": [
                        {
                            "entity_id": "light.kitchen",
                            "area_id": None,
                            "device_id": "device_1",
                        },
                    ],
                }
            if msg_type == "config/device_registry/list":
                return {
                    "success": True,
                    "result": [
                        {"id": "device_1", "area_id": "living_room"},
                    ],
                }
            return await original_ws(message)

        client.send_websocket_message = ws_with_device_registry

        with patch("ha_mcp.tools.smart_search.get_global_settings") as mock_settings:
            mock_settings.return_value.fuzzy_threshold = 60
            tools = SmartSearchTools(client=client)
            result = await tools.get_system_overview(detail_level="full")

        assert result["success"] is True
        assert result["system_summary"]["total_areas"] == 2
        area = result["area_analysis"]["living_room"]
        assert area["count"] == 1
        assert area["domains"]["light"] == 1

    @pytest.mark.asyncio
    async def test_domains_filter_returns_only_requested_domains(
        self, sample_entities, sample_services
    ):
        """domains_filter limits domain_stats to requested domains."""
        client = MockClient(entities=sample_entities, services=sample_services)
        tools = _make_tools(client)

        result = await tools.get_system_overview(
            detail_level="standard", domains_filter=["light"]
        )

        assert result["success"] is True
        # Only light domain in domain_stats
        assert "light" in result["domain_stats"]
        assert "switch" not in result["domain_stats"]
        # System-wide totals still reflect ALL entities
        assert result["system_summary"]["total_entities"] == 2
        assert result["system_summary"]["total_domains"] == 2
        assert result["system_summary"]["filtered_domains"] == ["light"]

    @pytest.mark.asyncio
    async def test_states_summary_capped_in_minimal(self):
        """Minimal mode caps states_summary to top 5."""
        # Create entities with many unique states
        entities = [
            {
                "entity_id": f"sensor.s{i}",
                "attributes": {"friendly_name": f"Sensor {i}"},
                "state": f"state_{i}",
            }
            for i in range(20)
        ]
        client = MockClient(entities=entities)
        tools = _make_tools(client)

        result = await tools.get_system_overview(detail_level="minimal")

        states = result["domain_stats"]["sensor"]["states_summary"]
        # 5 top states + _other = at most 6 entries
        assert len(states) <= 6
        assert "_other" in states
        # Total count still reflects all entities
        assert result["domain_stats"]["sensor"]["count"] == 20

    @pytest.mark.asyncio
    async def test_states_summary_uncapped_in_full(self):
        """Full mode returns all states."""
        entities = [
            {
                "entity_id": f"sensor.s{i}",
                "attributes": {"friendly_name": f"Sensor {i}"},
                "state": f"state_{i}",
            }
            for i in range(20)
        ]
        client = MockClient(entities=entities)
        tools = _make_tools(client)

        result = await tools.get_system_overview(detail_level="full")

        states = result["domain_stats"]["sensor"]["states_summary"]
        assert len(states) == 20
        assert "_other" not in states

    @pytest.mark.asyncio
    async def test_standard_returns_all_entities_by_default(self):
        """Standard mode returns all entities (no default cap)."""
        entities = [
            {
                "entity_id": f"sensor.s{i}",
                "attributes": {"friendly_name": f"Sensor {i}"},
                "state": "on",
            }
            for i in range(100)
        ]
        client = MockClient(entities=entities)
        tools = _make_tools(client)

        result = await tools.get_system_overview(detail_level="standard")

        domain = result["domain_stats"]["sensor"]
        assert domain["count"] == 100
        assert len(domain["entities"]) == 100
        assert domain["truncated"] is False

    @pytest.mark.asyncio
    async def test_max_entities_override_caps_any_level(self):
        """max_entities_per_domain caps entities on any detail level."""
        entities = [
            {
                "entity_id": f"sensor.s{i}",
                "attributes": {"friendly_name": f"Sensor {i}"},
                "state": "on",
            }
            for i in range(100)
        ]
        client = MockClient(entities=entities)
        tools = _make_tools(client)

        result = await tools.get_system_overview(
            detail_level="standard", max_entities_per_domain=25
        )

        domain = result["domain_stats"]["sensor"]
        assert domain["count"] == 100
        assert len(domain["entities"]) == 25
        assert domain["truncated"] is True

    @pytest.mark.asyncio
    async def test_max_entities_override_zero_means_no_limit(self):
        """max_entities_per_domain=0 disables entity and states caps."""
        entities = [
            {
                "entity_id": f"sensor.s{i}",
                "attributes": {"friendly_name": f"Sensor {i}"},
                "state": f"state_{i}",
            }
            for i in range(100)
        ]
        client = MockClient(entities=entities)
        tools = _make_tools(client)

        result = await tools.get_system_overview(
            detail_level="standard", max_entities_per_domain=0
        )

        domain = result["domain_stats"]["sensor"]
        assert len(domain["entities"]) == 100
        assert domain["truncated"] is False
        # states_summary also uncapped — all 100 unique states present
        assert len(domain["states_summary"]) == 100
        assert "_other" not in domain["states_summary"]

    @pytest.mark.asyncio
    async def test_pagination_default_limit_for_standard(self):
        """Standard mode defaults to 200 entity limit with pagination metadata."""
        entities = [
            {
                "entity_id": f"sensor.s{i}",
                "attributes": {"friendly_name": f"Sensor {i}"},
                "state": "on",
            }
            for i in range(300)
        ]
        client = MockClient(entities=entities)
        tools = _make_tools(client)

        result = await tools.get_system_overview(detail_level="standard")

        # Domain count is always complete
        assert result["domain_stats"]["sensor"]["count"] == 300
        # But entities are paginated to 200
        assert len(result["domain_stats"]["sensor"]["entities"]) == 200
        assert result["domain_stats"]["sensor"]["truncated"] is True
        # Pagination metadata present
        assert result["pagination"]["total_entity_results"] == 300
        assert result["pagination"]["offset"] == 0
        assert result["pagination"]["limit"] == 200
        assert result["pagination"]["entities_returned"] == 200
        assert result["pagination"]["has_more"] is True
        assert result["pagination"]["next_offset"] == 200

    @pytest.mark.asyncio
    async def test_pagination_offset_skips_entities(self):
        """Offset skips entities across domains."""
        entities = [
            {
                "entity_id": f"sensor.s{i}",
                "attributes": {"friendly_name": f"Sensor {i}"},
                "state": "on",
            }
            for i in range(300)
        ]
        client = MockClient(entities=entities)
        tools = _make_tools(client)

        result = await tools.get_system_overview(
            detail_level="standard", limit=100, offset=200
        )

        assert result["domain_stats"]["sensor"]["count"] == 300
        assert len(result["domain_stats"]["sensor"]["entities"]) == 100
        assert result["pagination"]["offset"] == 200
        assert result["pagination"]["entities_returned"] == 100
        assert result["pagination"]["has_more"] is False
        assert result["pagination"]["next_offset"] is None

    @pytest.mark.asyncio
    async def test_pagination_not_applied_to_minimal(self):
        """Minimal mode does not apply global pagination (already capped per-domain)."""
        entities = [
            {
                "entity_id": f"sensor.s{i}",
                "attributes": {"friendly_name": f"Sensor {i}"},
                "state": "on",
            }
            for i in range(50)
        ]
        client = MockClient(entities=entities)
        tools = _make_tools(client)

        result = await tools.get_system_overview(detail_level="minimal")

        # Minimal caps at 10 per domain, no global pagination
        assert len(result["domain_stats"]["sensor"]["entities"]) == 10
        assert "pagination" not in result

    @pytest.mark.asyncio
    async def test_pagination_across_multiple_domains(self):
        """Pagination distributes budget across domains in order."""
        entities = [
            {
                "entity_id": f"light.l{i}",
                "attributes": {"friendly_name": f"Light {i}"},
                "state": "on",
            }
            for i in range(150)
        ] + [
            {
                "entity_id": f"switch.s{i}",
                "attributes": {"friendly_name": f"Switch {i}"},
                "state": "off",
            }
            for i in range(150)
        ]
        client = MockClient(entities=entities)
        tools = _make_tools(client)

        result = await tools.get_system_overview(
            detail_level="standard", limit=200
        )

        # Both domains present with full counts
        assert result["domain_stats"]["light"]["count"] == 150
        assert result["domain_stats"]["switch"]["count"] == 150
        # Total entities returned is capped at 200
        total_returned = sum(
            len(ds["entities"]) for ds in result["domain_stats"].values()
        )
        assert total_returned == 200
        assert result["pagination"]["entities_returned"] == 200
        assert result["pagination"]["has_more"] is True

    @pytest.mark.asyncio
    async def test_pagination_explicit_limit_overrides_default(self):
        """Explicit limit=50 overrides the 200 default."""
        entities = [
            {
                "entity_id": f"sensor.s{i}",
                "attributes": {"friendly_name": f"Sensor {i}"},
                "state": "on",
            }
            for i in range(300)
        ]
        client = MockClient(entities=entities)
        tools = _make_tools(client)

        result = await tools.get_system_overview(
            detail_level="standard", limit=50
        )

        assert len(result["domain_stats"]["sensor"]["entities"]) == 50
        assert result["pagination"]["limit"] == 50
        assert result["pagination"]["has_more"] is True

    @pytest.mark.asyncio
    async def test_no_pagination_when_under_limit(self):
        """No pagination metadata when total entities fit within limit."""
        entities = [
            {
                "entity_id": f"sensor.s{i}",
                "attributes": {"friendly_name": f"Sensor {i}"},
                "state": "on",
            }
            for i in range(50)
        ]
        client = MockClient(entities=entities)
        tools = _make_tools(client)

        result = await tools.get_system_overview(detail_level="standard")

        # 50 entities < 200 default limit, all included
        assert len(result["domain_stats"]["sensor"]["entities"]) == 50
        assert result["pagination"]["has_more"] is False
        assert result["pagination"]["next_offset"] is None


# ---------------------------------------------------------------------------
# deep_search – outcome-based tests
# ---------------------------------------------------------------------------

class TestDeepSearchResults:
    """Test that deep_search returns correct, complete results."""

    @pytest.mark.asyncio
    async def test_finds_matching_automations(self, automation_entities):
        client = MockClient(entities=automation_entities)
        tools = _make_tools(client)

        result = await tools.deep_search(
            query="test", search_types=["automation"], limit=20,
        )

        assert result["success"] is True
        assert len(result["automations"]) == 10

    @pytest.mark.asyncio
    async def test_finds_matching_scripts(self, script_entities):
        client = MockClient(entities=script_entities)
        tools = _make_tools(client)

        result = await tools.deep_search(
            query="test", search_types=["script"], limit=20,
        )

        assert result["success"] is True
        assert len(result["scripts"]) == 5

    @pytest.mark.asyncio
    async def test_finds_matching_helpers(self):
        client = MockClient()
        tools = _make_tools(client)

        result = await tools.deep_search(
            query="test", search_types=["helper"], limit=20,
        )

        assert result["success"] is True
        assert len(result["helpers"]) > 0

    @pytest.mark.asyncio
    async def test_combined_search_returns_all_types(
        self, automation_entities, script_entities
    ):
        client = MockClient(entities=automation_entities + script_entities)
        tools = _make_tools(client)

        result = await tools.deep_search(
            query="test",
            search_types=["automation", "script", "helper"],
            limit=50,
        )

        assert result["success"] is True
        assert len(result["automations"]) > 0
        assert len(result["scripts"]) > 0
        assert len(result["helpers"]) > 0


class TestDeepSearchEfficiency:
    """Test that deep_search avoids redundant work."""

    @pytest.mark.asyncio
    async def test_get_states_called_once_for_all_types(
        self, automation_entities, script_entities
    ):
        """Regardless of how many search types, get_states is called once."""
        client = MockClient(entities=automation_entities + script_entities)
        tools = _make_tools(client)

        await tools.deep_search(
            query="test",
            search_types=["automation", "script", "helper"],
        )

        assert client.get_states_call_count == 1

    @pytest.mark.asyncio
    async def test_helper_fetch_is_parallel(self):
        """Helper types should be fetched in parallel, not sequentially."""
        client = MockClient(delay=0.02)
        tools = _make_tools(client)

        import time
        start = time.time()
        result = await tools.deep_search(
            query="test", search_types=["helper"],
        )
        elapsed = time.time() - start

        assert result["success"] is True
        # 6 helper types at 0.02s each: sequential = 0.12s, parallel ≈ 0.02s
        assert elapsed < 0.1, f"Expected parallel speedup, took {elapsed:.2f}s"


class TestDeepSearchResilience:
    """Test that deep_search handles failures gracefully."""

    @pytest.mark.asyncio
    async def test_rest_failure_still_returns_results(self, automation_entities):
        """If REST bulk fails, search should fall back and still return results."""
        client = MockClient(entities=automation_entities)

        original_request = client._request

        async def failing_bulk_request(method: str, path: str):
            # Fail bulk endpoint, allow individual endpoints through
            if path in ("/config/automation/config", "/config/script/config"):
                raise Exception("REST bulk endpoint unavailable")
            return await original_request(method, path)

        client._request = failing_bulk_request
        tools = _make_tools(client)

        result = await tools.deep_search(
            query="test", search_types=["automation"],
        )

        # Should succeed via fallback path (WS or individual fetch)
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_all_fetch_methods_fail_still_succeeds(self, automation_entities):
        """If all config fetch methods fail, search still returns name-only results."""
        client = MockClient(entities=automation_entities)

        async def fail_request(method: str, path: str):
            raise Exception("All REST endpoints down")

        async def fail_websocket(message: dict):
            raise Exception("WebSocket down")

        client._request = fail_request
        client.send_websocket_message = fail_websocket
        tools = _make_tools(client)

        result = await tools.deep_search(
            query="test", search_types=["automation"],
        )

        # Should succeed — name-matching still works without configs
        assert result["success"] is True
        # Results may have match_in_name only (no config match possible)
        for item in result["automations"]:
            assert "entity_id" in item
            assert "friendly_name" in item

    @pytest.mark.asyncio
    async def test_one_search_type_failing_does_not_break_others(
        self, automation_entities, script_entities
    ):
        """Failure in automation fetch should not prevent scripts from returning."""
        client = MockClient(entities=automation_entities + script_entities)

        original_request = client._request

        async def selective_failure(method: str, path: str):
            if "automation" in path:
                raise Exception("Automation endpoint down")
            return await original_request(method, path)

        client._request = selective_failure

        original_ws_send = client.send_websocket_message

        async def fail_ws(message: dict):
            msg_type = message.get("type", "")
            if "automation" in msg_type:
                raise Exception("WS automation down")
            return await original_ws_send(message)

        client.send_websocket_message = fail_ws
        tools = _make_tools(client)

        result = await tools.deep_search(
            query="test",
            search_types=["automation", "script"],
            limit=20,
        )

        assert result["success"] is True
        assert len(result["scripts"]) == 5
