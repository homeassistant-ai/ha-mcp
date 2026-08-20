"""Unit tests for area-filtered entity search (issue #504).

Tests that get_entities_by_area uses HA registries for accurate area resolution
instead of fuzzy-matching on friendly names, which caused false positives.

Bug: ha_search(area_filter="salon") returned entities from unrelated
areas because the old implementation used fuzzy matching on entity friendly
names instead of consulting the entity/device/area registries.
"""

import pytest

from ha_mcp.tools.smart_search import SmartSearchTools


def _ws_success(result):
    """Wrap a result list in a successful WebSocket response."""
    return {"success": True, "result": result}


class MockClientWithRegistries:
    """Mock Home Assistant client that returns configurable registry data."""

    def __init__(
        self,
        entities: list[dict],
        areas: list[dict] | None = None,
        floors: list[dict] | None = None,
        entity_registry: list[dict] | None = None,
        device_registry: list[dict] | None = None,
    ):
        """Initialize configurable entity, area, floor, and device registries."""
        self.entities = entities
        self.areas = areas or []
        self.floors = floors or []
        self.entity_registry = entity_registry or []
        self.device_registry = device_registry or []
        self.base_url = "http://localhost:8123"

    async def get_states(self) -> list[dict]:
        return self.entities

    async def get_config(self) -> dict:
        return {"version": "2026.1.0"}

    async def send_websocket_message(self, message: dict) -> dict:
        """Return the configured registry payload for a WebSocket request."""
        msg_type = message.get("type", "")
        if msg_type == "config/area_registry/list":
            return _ws_success(self.areas)
        elif msg_type == "config/floor_registry/list":
            return _ws_success(self.floors)
        elif msg_type == "config/entity_registry/list":
            return _ws_success(self.entity_registry)
        elif msg_type == "config/device_registry/list":
            return _ws_success(self.device_registry)
        return {"success": False, "error": {"code": "unknown_command"}}


@pytest.fixture
def two_area_setup():
    """Setup with two areas: 'salon' and 'abc', each with distinct entities.

    This reproduces the exact scenario from issue #504:
    - Area 'salon' has a light entity
    - Area 'abc' has a climate entity (assigned via device)
    - Searching for area 'salon' must NOT return climate.abc
    """
    areas = [
        {"area_id": "salon", "name": "Salon"},
        {"area_id": "abc", "name": "ABC"},
    ]

    entities = [
        {
            "entity_id": "light.salon_lamp",
            "attributes": {"friendly_name": "Salon Lamp"},
            "state": "on",
        },
        {
            "entity_id": "climate.abc",
            "attributes": {"friendly_name": "Climatisation ABC"},
            "state": "heat",
        },
        {
            "entity_id": "sensor.outdoor_temp",
            "attributes": {"friendly_name": "Outdoor Temperature"},
            "state": "15.2",
        },
    ]

    entity_registry = [
        # light.salon_lamp directly assigned to salon area
        {"entity_id": "light.salon_lamp", "area_id": "salon", "device_id": None},
        # climate.abc assigned to area via device (no direct area_id)
        {
            "entity_id": "climate.abc",
            "area_id": None,
            "device_id": "device_abc_thermostat",
        },
        # sensor has no area at all
        {"entity_id": "sensor.outdoor_temp", "area_id": None, "device_id": None},
    ]

    device_registry = [
        {"id": "device_abc_thermostat", "area_id": "abc"},
    ]

    return {
        "areas": areas,
        "entities": entities,
        "entity_registry": entity_registry,
        "device_registry": device_registry,
    }


class TestAreaFilterAccuracy:
    """Test that area filtering uses registries and returns only correct entities."""

    @pytest.mark.asyncio
    async def test_area_filter_excludes_other_areas(self, two_area_setup):
        """Issue #504: searching for 'salon' must NOT return entities from area 'abc'."""
        client = MockClientWithRegistries(
            entities=two_area_setup["entities"],
            areas=two_area_setup["areas"],
            entity_registry=two_area_setup["entity_registry"],
            device_registry=two_area_setup["device_registry"],
        )
        tools = SmartSearchTools(client=client, fuzzy_threshold=60)

        result = await tools.get_entities_by_area("salon")

        assert result["total_areas_found"] == 1
        assert result["total_entities"] == 1

        # Should only contain salon area
        assert "salon" in result["areas"]
        assert "abc" not in result["areas"]

        # Should only contain the salon lamp, not climate.abc
        salon_area = result["areas"]["salon"]
        all_entity_ids = []
        for domain_entities in salon_area["entities"].values():
            all_entity_ids.extend([e["entity_id"] for e in domain_entities])

        assert "light.salon_lamp" in all_entity_ids
        assert "climate.abc" not in all_entity_ids

    @pytest.mark.asyncio
    async def test_area_filter_returns_abc_area(self, two_area_setup):
        """Searching for 'abc' returns only entities in area 'abc'."""
        client = MockClientWithRegistries(
            entities=two_area_setup["entities"],
            areas=two_area_setup["areas"],
            entity_registry=two_area_setup["entity_registry"],
            device_registry=two_area_setup["device_registry"],
        )
        tools = SmartSearchTools(client=client, fuzzy_threshold=60)

        result = await tools.get_entities_by_area("abc")

        assert result["total_areas_found"] == 1
        assert "abc" in result["areas"]
        assert "salon" not in result["areas"]

        abc_area = result["areas"]["abc"]
        all_entity_ids = []
        for domain_entities in abc_area["entities"].values():
            all_entity_ids.extend([e["entity_id"] for e in domain_entities])

        assert "climate.abc" in all_entity_ids
        assert "light.salon_lamp" not in all_entity_ids

    @pytest.mark.asyncio
    async def test_device_area_inheritance(self, two_area_setup):
        """Entities inherit area from their device when no direct area assignment."""
        client = MockClientWithRegistries(
            entities=two_area_setup["entities"],
            areas=two_area_setup["areas"],
            entity_registry=two_area_setup["entity_registry"],
            device_registry=two_area_setup["device_registry"],
        )
        tools = SmartSearchTools(client=client, fuzzy_threshold=60)

        result = await tools.get_entities_by_area("abc")

        # climate.abc should be found via device_abc_thermostat -> area "abc"
        assert result["total_entities"] == 1
        abc_area = result["areas"]["abc"]
        all_entity_ids = []
        for domain_entities in abc_area["entities"].values():
            all_entity_ids.extend([e["entity_id"] for e in domain_entities])
        assert "climate.abc" in all_entity_ids

    @pytest.mark.asyncio
    async def test_unassigned_entities_excluded(self, two_area_setup):
        """Entities without any area assignment are not returned."""
        client = MockClientWithRegistries(
            entities=two_area_setup["entities"],
            areas=two_area_setup["areas"],
            entity_registry=two_area_setup["entity_registry"],
            device_registry=two_area_setup["device_registry"],
        )
        tools = SmartSearchTools(client=client, fuzzy_threshold=60)

        # Check salon
        salon_result = await tools.get_entities_by_area("salon")
        salon_ids = []
        for domain_entities in salon_result["areas"]["salon"]["entities"].values():
            salon_ids.extend([e["entity_id"] for e in domain_entities])
        assert "sensor.outdoor_temp" not in salon_ids

        # Check abc
        abc_result = await tools.get_entities_by_area("abc")
        abc_ids = []
        for domain_entities in abc_result["areas"]["abc"]["entities"].values():
            abc_ids.extend([e["entity_id"] for e in domain_entities])
        assert "sensor.outdoor_temp" not in abc_ids


class TestAreaMatchingLogic:
    """Test fuzzy matching on area names/IDs."""

    @pytest.fixture
    def multi_area_setup(self):
        areas = [
            {"area_id": "living_room", "name": "Living Room"},
            {"area_id": "bedroom_main", "name": "Main Bedroom"},
            {"area_id": "kitchen", "name": "Kitchen"},
        ]
        entities = [
            {
                "entity_id": "light.living_light",
                "attributes": {"friendly_name": "Living Room Light"},
                "state": "on",
            },
            {
                "entity_id": "light.bedroom_light",
                "attributes": {"friendly_name": "Main Bedroom Light"},
                "state": "off",
            },
            {
                "entity_id": "light.kitchen_light",
                "attributes": {"friendly_name": "Kitchen Light"},
                "state": "on",
            },
        ]
        entity_registry = [
            {
                "entity_id": "light.living_light",
                "area_id": "living_room",
                "device_id": None,
            },
            {
                "entity_id": "light.bedroom_light",
                "area_id": "bedroom_main",
                "device_id": None,
            },
            {
                "entity_id": "light.kitchen_light",
                "area_id": "kitchen",
                "device_id": None,
            },
        ]
        return {
            "areas": areas,
            "entities": entities,
            "entity_registry": entity_registry,
            "device_registry": [],
        }

    @pytest.mark.asyncio
    async def test_exact_area_name_match(self, multi_area_setup):
        """Exact area name match works."""
        client = MockClientWithRegistries(
            entities=multi_area_setup["entities"],
            areas=multi_area_setup["areas"],
            entity_registry=multi_area_setup["entity_registry"],
            device_registry=multi_area_setup["device_registry"],
        )
        tools = SmartSearchTools(client=client, fuzzy_threshold=60)

        result = await tools.get_entities_by_area("Kitchen")

        assert result["total_areas_found"] == 1
        assert "kitchen" in result["areas"]

    @pytest.mark.asyncio
    async def test_case_insensitive_area_match(self, multi_area_setup):
        """Area matching is case-insensitive."""
        client = MockClientWithRegistries(
            entities=multi_area_setup["entities"],
            areas=multi_area_setup["areas"],
            entity_registry=multi_area_setup["entity_registry"],
            device_registry=multi_area_setup["device_registry"],
        )
        tools = SmartSearchTools(client=client, fuzzy_threshold=60)

        result = await tools.get_entities_by_area("kitchen")

        assert result["total_areas_found"] == 1
        assert "kitchen" in result["areas"]

    @pytest.mark.asyncio
    async def test_no_matching_area_returns_empty_with_suggestions(
        self, multi_area_setup
    ):
        """Non-matching area returns empty results with available areas."""
        client = MockClientWithRegistries(
            entities=multi_area_setup["entities"],
            areas=multi_area_setup["areas"],
            entity_registry=multi_area_setup["entity_registry"],
            device_registry=multi_area_setup["device_registry"],
        )
        tools = SmartSearchTools(client=client, fuzzy_threshold=60)

        result = await tools.get_entities_by_area("garage")

        assert result["total_areas_found"] == 0
        assert result["total_entities"] == 0
        assert result["areas"] == {}
        # Should include available areas
        available = result["available_areas"]
        area_ids = [a["area_id"] for a in available]
        assert "living_room" in area_ids
        assert "kitchen" in area_ids

    @staticmethod
    def _basement_client(*, area_aliases: list[str] | None = None):
        """Build registries where a floor name partially resembles an area name."""
        return MockClientWithRegistries(
            entities=[
                {
                    "entity_id": "light.basement_hall",
                    "attributes": {"friendly_name": "Basement Hall"},
                    "state": "on",
                }
            ],
            areas=[
                {
                    "area_id": "couloir_sous_sol",
                    "name": "Couloir Sous-Sol",
                    "aliases": area_aliases or [],
                    "floor_id": "sous_sol",
                }
            ],
            floors=[
                {
                    "floor_id": "sous_sol",
                    "name": "Sous-sol",
                    "aliases": ["Cave level"],
                }
            ],
            entity_registry=[
                {
                    "entity_id": "light.basement_hall",
                    "area_id": "couloir_sous_sol",
                    "device_id": None,
                }
            ],
        )

    @pytest.mark.asyncio
    async def test_floor_name_expands_to_all_areas_on_floor(self):
        """An exact floor name expands instead of fuzzy-matching one area."""
        client = self._basement_client()
        client.areas.extend(
            [
                {"area_id": "cave", "name": "Cave", "floor_id": "sous_sol"},
                {"area_id": "garage", "name": "Garage", "floor_id": "ground"},
            ]
        )
        tools = SmartSearchTools(client=client, fuzzy_threshold=60)

        result = await tools.get_entities_by_area("sous-sol")

        assert result["total_areas_found"] == 2
        assert set(result["areas"]) == {"couloir_sous_sol", "cave"}
        assert any("expanded to 2 area(s)" in warning for warning in result["warnings"])

    @pytest.mark.asyncio
    async def test_floor_alias_expands_to_areas_on_floor(self):
        """An exact floor alias follows the same deterministic expansion."""
        tools = SmartSearchTools(client=self._basement_client(), fuzzy_threshold=60)

        result = await tools.get_entities_by_area("Cave level")

        assert set(result["areas"]) == {"couloir_sous_sol"}
        assert any(
            "is a floor, not an area" in warning for warning in result["warnings"]
        )

    @pytest.mark.asyncio
    async def test_exact_area_id_still_works(self):
        """A literal area_id from ha_list_floors_areas remains valid."""
        tools = SmartSearchTools(client=self._basement_client(), fuzzy_threshold=60)

        result = await tools.get_entities_by_area("couloir_sous_sol")

        assert result["total_areas_found"] == 1
        assert set(result["areas"]) == {"couloir_sous_sol"}

    @pytest.mark.asyncio
    async def test_exact_area_alias_still_works(self):
        """An exact area alias remains a valid area_filter."""
        tools = SmartSearchTools(
            client=self._basement_client(area_aliases=["Hall du bas"]),
            fuzzy_threshold=60,
        )

        result = await tools.get_entities_by_area("Hall du bas")

        assert result["total_areas_found"] == 1
        assert set(result["areas"]) == {"couloir_sous_sol"}

    @pytest.mark.asyncio
    async def test_exact_area_wins_when_floor_has_same_name(self):
        """A shared exact area/floor name resolves to the area with a warning."""
        client = self._basement_client(area_aliases=["Sous-sol"])
        tools = SmartSearchTools(client=client, fuzzy_threshold=60)

        result = await tools.get_entities_by_area("Sous-sol")

        assert set(result["areas"]) == {"couloir_sous_sol"}
        assert any("matches both an area and a floor" in w for w in result["warnings"])

    @pytest.mark.asyncio
    async def test_exact_area_id_wins_when_floor_has_same_identifier(self):
        """An area ID collision also preserves exact-area precedence."""
        client = self._basement_client()
        client.areas[0]["area_id"] = "sous_sol"
        client.entity_registry[0]["area_id"] = "sous_sol"
        tools = SmartSearchTools(client=client, fuzzy_threshold=60)

        result = await tools.get_entities_by_area("sous_sol")

        assert set(result["areas"]) == {"sous_sol"}
        assert any("matches both an area and a floor" in w for w in result["warnings"])

    @pytest.mark.asyncio
    async def test_shared_floor_alias_expands_all_matching_floors(self):
        """Warnings identify every floor matched by a shared exact alias."""
        client = self._basement_client()
        client.floors.append(
            {"floor_id": "lower", "name": None, "aliases": ["Cave level"]}
        )
        client.areas.append(
            {"area_id": "lower_room", "name": "Lower room", "floor_id": "lower"}
        )
        tools = SmartSearchTools(client=client, fuzzy_threshold=60)

        result = await tools.get_entities_by_area("Cave level")

        assert set(result["areas"]) == {"couloir_sous_sol", "lower_room"}
        warning = next(w for w in result["warnings"] if "expanded" in w)
        assert "Sous-sol" in warning
        assert "lower" in warning

    @pytest.mark.asyncio
    async def test_floor_typo_expands_instead_of_returning_partial_area(self):
        """A close whole-floor spelling expands all areas on the floor."""
        client = self._basement_client()
        client.areas.append({"area_id": "cave", "name": "Cave", "floor_id": "sous_sol"})
        tools = SmartSearchTools(client=client, fuzzy_threshold=60)

        result = await tools.get_entities_by_area("sous-sl")

        assert set(result["areas"]) == {"couloir_sous_sol", "cave"}
        assert any("close spelling" in w for w in result["warnings"])

    @pytest.mark.asyncio
    async def test_ambiguous_floor_typo_does_not_expand_multiple_floors(self):
        """A tied close-spelling match requires a more specific floor query."""
        client = self._basement_client()
        client.floors = [
            {"floor_id": "east", "name": "East Wing", "aliases": []},
            {"floor_id": "west", "name": "West Wing", "aliases": []},
        ]
        client.areas = [
            {"area_id": "east_room", "name": "East room", "floor_id": "east"},
            {"area_id": "west_room", "name": "West room", "floor_id": "west"},
        ]
        tools = SmartSearchTools(client=client, fuzzy_threshold=60)

        result = await tools.get_entities_by_area("st Wing")

        assert result["total_areas_found"] == 0
        warning = next(w for w in result["warnings"] if "ambiguously" in w)
        assert "East Wing" in warning
        assert "West Wing" in warning

    @pytest.mark.asyncio
    async def test_area_typo_still_prefers_closer_area_name(self):
        """A near area name is not broadened merely because it contains a floor."""
        tools = SmartSearchTools(client=self._basement_client(), fuzzy_threshold=60)

        result = await tools.get_entities_by_area("Couloir Sous-Sl")

        assert set(result["areas"]) == {"couloir_sous_sol"}
        assert not any("is a floor" in w for w in result.get("warnings", []))


class TestAreaFilterGroupByDomain:
    """Test domain grouping in area results."""

    @pytest.mark.asyncio
    async def test_grouped_by_domain(self):
        """Results are correctly grouped by domain when requested."""
        areas = [{"area_id": "salon", "name": "Salon"}]
        entities = [
            {
                "entity_id": "light.salon_light",
                "attributes": {"friendly_name": "Salon Light"},
                "state": "on",
            },
            {
                "entity_id": "switch.salon_switch",
                "attributes": {"friendly_name": "Salon Switch"},
                "state": "off",
            },
            {
                "entity_id": "sensor.salon_temp",
                "attributes": {"friendly_name": "Salon Temperature"},
                "state": "21",
            },
        ]
        entity_registry = [
            {"entity_id": "light.salon_light", "area_id": "salon", "device_id": None},
            {"entity_id": "switch.salon_switch", "area_id": "salon", "device_id": None},
            {"entity_id": "sensor.salon_temp", "area_id": "salon", "device_id": None},
        ]
        client = MockClientWithRegistries(
            entities=entities,
            areas=areas,
            entity_registry=entity_registry,
        )
        tools = SmartSearchTools(client=client, fuzzy_threshold=60)

        result = await tools.get_entities_by_area("salon", group_by_domain=True)

        salon_entities = result["areas"]["salon"]["entities"]
        assert isinstance(salon_entities, dict)  # grouped by domain
        assert "light" in salon_entities
        assert "switch" in salon_entities
        assert "sensor" in salon_entities
        assert len(salon_entities["light"]) == 1
        assert salon_entities["light"][0]["entity_id"] == "light.salon_light"

    @pytest.mark.asyncio
    async def test_flat_list_without_grouping(self):
        """Results are a flat list when group_by_domain=False."""
        areas = [{"area_id": "salon", "name": "Salon"}]
        entities = [
            {
                "entity_id": "light.salon_light",
                "attributes": {"friendly_name": "Salon Light"},
                "state": "on",
            },
            {
                "entity_id": "switch.salon_switch",
                "attributes": {"friendly_name": "Salon Switch"},
                "state": "off",
            },
        ]
        entity_registry = [
            {"entity_id": "light.salon_light", "area_id": "salon", "device_id": None},
            {"entity_id": "switch.salon_switch", "area_id": "salon", "device_id": None},
        ]
        client = MockClientWithRegistries(
            entities=entities,
            areas=areas,
            entity_registry=entity_registry,
        )
        tools = SmartSearchTools(client=client, fuzzy_threshold=60)

        result = await tools.get_entities_by_area("salon", group_by_domain=False)

        salon_entities = result["areas"]["salon"]["entities"]
        assert isinstance(salon_entities, list)  # flat list
        entity_ids = [e["entity_id"] for e in salon_entities]
        assert "light.salon_light" in entity_ids
        assert "switch.salon_switch" in entity_ids


class TestEntityDirectAreaOverride:
    """Test that entity-level area_id takes priority over device-level area_id."""

    @pytest.mark.asyncio
    async def test_entity_area_overrides_device_area(self):
        """Entity's direct area assignment takes priority over device area."""
        areas = [
            {"area_id": "salon", "name": "Salon"},
            {"area_id": "bedroom", "name": "Bedroom"},
        ]
        entities = [
            {
                "entity_id": "sensor.device_temp",
                "attributes": {"friendly_name": "Device Temp"},
                "state": "22",
            },
        ]
        # Entity is directly assigned to salon, but device is in bedroom
        entity_registry = [
            {
                "entity_id": "sensor.device_temp",
                "area_id": "salon",
                "device_id": "device_1",
            },
        ]
        device_registry = [
            {"id": "device_1", "area_id": "bedroom"},
        ]
        client = MockClientWithRegistries(
            entities=entities,
            areas=areas,
            entity_registry=entity_registry,
            device_registry=device_registry,
        )
        tools = SmartSearchTools(client=client, fuzzy_threshold=60)

        # Should be in salon (entity override), not bedroom (device)
        salon_result = await tools.get_entities_by_area("salon")
        assert salon_result["total_entities"] == 1

        bedroom_result = await tools.get_entities_by_area("bedroom")
        assert bedroom_result["total_entities"] == 0


class TestRegistryFailureGracefulDegradation:
    """Test behavior when registry calls fail."""

    @pytest.mark.asyncio
    async def test_all_registries_fail_returns_empty(self):
        """When all registry calls fail, returns empty results gracefully."""

        class FailingClient:
            base_url = "http://localhost:8123"

            async def get_states(self):
                return [
                    {
                        "entity_id": "light.test",
                        "attributes": {"friendly_name": "Test"},
                        "state": "on",
                    },
                ]

            async def get_config(self):
                return {}

            async def send_websocket_message(self, message):
                raise ConnectionError("WebSocket unavailable")

        client = FailingClient()
        tools = SmartSearchTools(client=client, fuzzy_threshold=60)

        result = await tools.get_entities_by_area("salon")

        # Should return empty, not crash
        assert result["total_areas_found"] == 0
        assert result["total_entities"] == 0
