"""
E2E test: Create a "Living Room" dashboard at url_path "living-room-test".

Sections:
  1. Lighting  - All 6 lights with tile cards (toggle controls)
  2. Climate   - 3 climate entities + temperature/humidity sensors
  3. Quick Actions - "Turn Off All Lights" button (calls light.turn_off on all)

This test creates the dashboard, verifies it, and cleans up.
"""

import logging
from typing import Any

from tests.src.e2e.utilities.assertions import MCPAssertions

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dashboard configuration
# ---------------------------------------------------------------------------

ALL_LIGHTS = [
    "light.bed_light",
    "light.ceiling_lights",
    "light.kitchen_lights",
    "light.office_rgbw_lights",
    "light.living_room_rgbww_lights",
    "light.entrance_color_white_lights",
]

DASHBOARD_CONFIG: dict[str, Any] = {
    "views": [
        {
            "title": "Living Room",
            "path": "living-room",
            "icon": "mdi:sofa",
            "type": "sections",
            "sections": [
                # ── Section 1: Lighting ────────────────────────────
                {
                    "title": "Lighting",
                    "cards": [
                        {
                            "type": "heading",
                            "heading": "All Lights",
                            "heading_style": "subtitle",
                            "icon": "mdi:lightbulb-group",
                        },
                        {
                            "type": "tile",
                            "entity": "light.bed_light",
                            "name": "Bed Light",
                            "features": [
                                {"type": "light-brightness"},
                                {"type": "light-color-temp"},
                            ],
                        },
                        {
                            "type": "tile",
                            "entity": "light.ceiling_lights",
                            "name": "Ceiling Lights",
                            "features": [
                                {"type": "light-brightness"},
                                {"type": "light-color-temp"},
                            ],
                        },
                        {
                            "type": "tile",
                            "entity": "light.kitchen_lights",
                            "name": "Kitchen Lights",
                            "features": [
                                {"type": "light-brightness"},
                                {"type": "light-color-temp"},
                            ],
                        },
                        {
                            "type": "tile",
                            "entity": "light.office_rgbw_lights",
                            "name": "Office RGBW Lights",
                            "features": [{"type": "light-brightness"}],
                        },
                        {
                            "type": "tile",
                            "entity": "light.living_room_rgbww_lights",
                            "name": "Living Room RGBWW Lights",
                            "features": [{"type": "light-brightness"}],
                        },
                        {
                            "type": "tile",
                            "entity": "light.entrance_color_white_lights",
                            "name": "Entrance Lights",
                            "features": [{"type": "light-brightness"}],
                        },
                    ],
                },
                # ── Section 2: Climate & Temperature ───────────────
                {
                    "title": "Climate & Temperature",
                    "cards": [
                        {
                            "type": "heading",
                            "heading": "Climate Control",
                            "heading_style": "subtitle",
                            "icon": "mdi:thermostat",
                        },
                        {
                            "type": "tile",
                            "entity": "climate.heatpump",
                            "name": "Heat Pump",
                            "features": [
                                {"type": "climate-hvac-modes"},
                                {"type": "target-temperature"},
                            ],
                        },
                        {
                            "type": "tile",
                            "entity": "climate.hvac",
                            "name": "HVAC",
                            "features": [
                                {"type": "climate-hvac-modes"},
                                {"type": "climate-fan-modes"},
                                {"type": "target-temperature"},
                            ],
                        },
                        {
                            "type": "tile",
                            "entity": "climate.ecobee",
                            "name": "Ecobee",
                            "features": [
                                {"type": "climate-hvac-modes"},
                                {"type": "climate-preset-modes"},
                                {"type": "target-temperature"},
                            ],
                        },
                        {
                            "type": "heading",
                            "heading": "Sensors",
                            "heading_style": "subtitle",
                            "icon": "mdi:thermometer",
                        },
                        {
                            "type": "tile",
                            "entity": "sensor.outside_temperature",
                            "name": "Outside Temperature",
                            "icon": "mdi:thermometer",
                        },
                        {
                            "type": "tile",
                            "entity": "sensor.outside_humidity",
                            "name": "Outside Humidity",
                            "icon": "mdi:water-percent",
                        },
                        {
                            "type": "weather-forecast",
                            "entity": "weather.demo_weather_south",
                            "name": "Weather",
                            "forecast_type": "daily",
                        },
                    ],
                },
                # ── Section 3: Quick Actions ───────────────────────
                {
                    "title": "Quick Actions",
                    "cards": [
                        {
                            "type": "button",
                            "name": "Turn Off All Lights",
                            "icon": "mdi:lightbulb-off",
                            "show_name": True,
                            "show_icon": True,
                            "tap_action": {
                                "action": "perform-action",
                                "perform_action": "light.turn_off",
                                "target": {
                                    "entity_id": ALL_LIGHTS,
                                },
                            },
                        },
                    ],
                },
            ],
        }
    ]
}


class TestLivingRoomDashboard:
    """Create, verify, and tear down the Living Room dashboard."""

    async def test_create_living_room_dashboard(self, mcp_client):
        """Create the Living Room dashboard and verify its structure."""
        mcp = MCPAssertions(mcp_client)

        # ── Step 1: Create the dashboard ──────────────────────────
        logger.info("Creating 'Living Room' dashboard at url_path='living-room-test'")
        create_data = await mcp.call_tool_success(
            "ha_config_set_dashboard",
            {
                "url_path": "living-room-test",
                "title": "Living Room",
                "icon": "mdi:sofa",
                "config": DASHBOARD_CONFIG,
            },
        )
        assert create_data["success"] is True
        dashboard_id = create_data.get("dashboard_id")
        assert dashboard_id is not None
        logger.info(f"Dashboard created: id={dashboard_id}")

        try:
            # ── Step 2: Verify dashboard appears in list ──────────
            list_data = await mcp.call_tool_success(
                "ha_config_get_dashboard", {"list_only": True}
            )
            assert any(
                d.get("url_path") == "living-room-test"
                for d in list_data.get("dashboards", [])
            ), "Dashboard 'living-room-test' not found in dashboard list"

            # ── Step 3: Verify full config ────────────────────────
            get_data = await mcp.call_tool_success(
                "ha_config_get_dashboard", {"url_path": "living-room-test"}
            )
            assert get_data["success"] is True
            config = get_data["config"]
            assert "views" in config

            view = config["views"][0]
            assert view["title"] == "Living Room"
            assert view["type"] == "sections"

            sections = view["sections"]
            assert len(sections) == 3, f"Expected 3 sections, got {len(sections)}"

            # Lighting section: heading + 6 light tiles = 7 cards
            lighting_cards = sections[0]["cards"]
            assert len(lighting_cards) == 7
            light_entities = [
                c["entity"] for c in lighting_cards if c.get("type") == "tile"
            ]
            assert set(light_entities) == set(ALL_LIGHTS)

            # Climate section: heading + 3 climate tiles + heading + 2 sensors + 1 weather = 8 cards
            climate_cards = sections[1]["cards"]
            assert len(climate_cards) == 8
            climate_entities = [
                c["entity"]
                for c in climate_cards
                if c.get("type") == "tile" and "climate." in c.get("entity", "")
            ]
            assert set(climate_entities) == {
                "climate.heatpump",
                "climate.hvac",
                "climate.ecobee",
            }

            # Quick Actions section: 1 button card
            action_cards = sections[2]["cards"]
            assert len(action_cards) == 1
            button = action_cards[0]
            assert button["type"] == "button"
            assert button["name"] == "Turn Off All Lights"
            assert (
                button["tap_action"]["perform_action"] == "light.turn_off"
            )
            assert set(button["tap_action"]["target"]["entity_id"]) == set(
                ALL_LIGHTS
            )

            logger.info("All dashboard assertions passed")

        finally:
            # ── Cleanup ───────────────────────────────────────────
            logger.info("Cleaning up dashboard")
            await mcp.call_tool_success(
                "ha_config_delete_dashboard", {"dashboard_id": dashboard_id}
            )
            logger.info("Dashboard deleted successfully")
