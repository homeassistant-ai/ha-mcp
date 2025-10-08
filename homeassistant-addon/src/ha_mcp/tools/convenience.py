"""
Convenience tools for common Home Assistant operations.

This module provides high-level convenience tools for scenes, automations,
weather, energy monitoring, and other frequently used Home Assistant features.
"""

import logging
from datetime import datetime
from typing import Any

from ..client.rest_client import HomeAssistantClient
from ..config import get_global_settings

logger = logging.getLogger(__name__)


class ConvenienceTools:
    """High-level convenience tools for Home Assistant."""

    def __init__(self, client: HomeAssistantClient | None = None):
        """Initialize convenience tools."""
        self.settings = get_global_settings()
        self.client = client or HomeAssistantClient()

    async def activate_scene(self, scene_name: str) -> dict[str, Any]:
        """
        Activate a Home Assistant scene by name or entity ID.

        Args:
            scene_name: Scene name or entity ID (e.g., 'Evening Lights' or 'scene.evening')

        Returns:
            Scene activation result
        """
        try:
            # Get all scenes
            states = await self.client.get_states()
            scenes = [s for s in states if s["entity_id"].startswith("scene.")]

            if not scenes:
                return {
                    "success": False,
                    "error": "No scenes found in Home Assistant",
                    "suggestions": ["Create scenes in Home Assistant UI first"],
                }

            # Find matching scene
            target_scene = None
            scene_name_lower = scene_name.lower()

            for scene in scenes:
                entity_id = scene["entity_id"]
                friendly_name = (
                    scene.get("attributes", {}).get("friendly_name", "").lower()
                )

                # Check exact entity ID match
                if entity_id == scene_name or entity_id == f"scene.{scene_name}":
                    target_scene = scene
                    break

                # Check friendly name match
                if friendly_name == scene_name_lower:
                    target_scene = scene
                    break

                # Check partial match
                if scene_name_lower in friendly_name or scene_name_lower in entity_id:
                    target_scene = scene
                    break

            if not target_scene:
                scene_list = [
                    f"{s['entity_id']} ({s.get('attributes', {}).get('friendly_name', 'No name')})"
                    for s in scenes[:10]  # Show first 10 scenes
                ]

                return {
                    "scene_name": scene_name,
                    "success": False,
                    "error": f"Scene not found: {scene_name}",
                    "available_scenes": scene_list,
                    "suggestions": [
                        "Check scene name spelling",
                        "Use entity ID format like scene.evening_lights",
                        "Use smart_entity_search to find scene entity IDs",
                    ],
                }

            # Activate the scene
            entity_id = target_scene["entity_id"]
            await self.client.call_service("scene", "turn_on", {"entity_id": entity_id})

            return {
                "scene_name": scene_name,
                "entity_id": entity_id,
                "friendly_name": target_scene.get("attributes", {}).get(
                    "friendly_name"
                ),
                "success": True,
                "message": f"Scene activated: {target_scene.get('attributes', {}).get('friendly_name', entity_id)}",
                "timestamp": datetime.now().isoformat(),
            }

        except Exception as e:
            logger.error(f"Error activating scene: {e}")
            return {
                "scene_name": scene_name,
                "success": False,
                "error": f"Failed to activate scene: {str(e)}",
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify scene exists and is enabled",
                    "Check scene entity permissions",
                ],
            }

    async def trigger_automation(self, automation_name: str) -> dict[str, Any]:
        """
        Trigger a Home Assistant automation by name or entity ID.

        Args:
            automation_name: Automation name or entity ID

        Returns:
            Automation trigger result
        """
        try:
            # Get all automations
            states = await self.client.get_states()
            automations = [
                s for s in states if s["entity_id"].startswith("automation.")
            ]

            if not automations:
                return {
                    "success": False,
                    "error": "No automations found in Home Assistant",
                    "suggestions": ["Create automations in Home Assistant UI first"],
                }

            # Find matching automation
            target_automation = None
            automation_name_lower = automation_name.lower()

            for automation in automations:
                entity_id = automation["entity_id"]
                friendly_name = (
                    automation.get("attributes", {}).get("friendly_name", "").lower()
                )

                # Check exact matches
                if (
                    entity_id == automation_name
                    or entity_id == f"automation.{automation_name}"
                ):
                    target_automation = automation
                    break

                if friendly_name == automation_name_lower:
                    target_automation = automation
                    break

                # Check partial matches
                if (
                    automation_name_lower in friendly_name
                    or automation_name_lower in entity_id
                ):
                    target_automation = automation
                    break

            if not target_automation:
                automation_list = [
                    f"{a['entity_id']} ({a.get('attributes', {}).get('friendly_name', 'No name')})"
                    for a in automations[:10]  # Show first 10
                ]

                return {
                    "automation_name": automation_name,
                    "success": False,
                    "error": f"Automation not found: {automation_name}",
                    "available_automations": automation_list,
                    "suggestions": [
                        "Check automation name spelling",
                        "Use entity ID format like automation.morning_routine",
                        "Use smart_entity_search to find automation entity IDs",
                    ],
                }

            # Check if automation is enabled
            state = target_automation.get("state", "unknown")
            if state == "off":
                return {
                    "automation_name": automation_name,
                    "entity_id": target_automation["entity_id"],
                    "success": False,
                    "error": "Automation is disabled",
                    "suggestions": [
                        "Enable the automation first",
                        "Check automation configuration",
                    ],
                }

            # Trigger the automation
            entity_id = target_automation["entity_id"]
            await self.client.call_service(
                "automation", "trigger", {"entity_id": entity_id}
            )

            return {
                "automation_name": automation_name,
                "entity_id": entity_id,
                "friendly_name": target_automation.get("attributes", {}).get(
                    "friendly_name"
                ),
                "success": True,
                "message": f"Automation triggered: {target_automation.get('attributes', {}).get('friendly_name', entity_id)}",
                "timestamp": datetime.now().isoformat(),
            }

        except Exception as e:
            logger.error(f"Error triggering automation: {e}")
            return {
                "automation_name": automation_name,
                "success": False,
                "error": f"Failed to trigger automation: {str(e)}",
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify automation exists and is enabled",
                    "Check automation entity permissions",
                ],
            }

    async def get_weather_info(self, location: str | None = None) -> dict[str, Any]:
        """
        Get current weather information from Home Assistant weather entities.

        Args:
            location: Optional location filter

        Returns:
            Weather information
        """
        try:
            # Get weather entities
            states = await self.client.get_states()
            weather_entities = [
                s for s in states if s["entity_id"].startswith("weather.")
            ]

            if not weather_entities:
                return {
                    "success": False,
                    "error": "No weather entities found",
                    "suggestions": [
                        "Add a weather integration to Home Assistant",
                        "Configure weather entities in integrations",
                    ],
                }

            # Use first weather entity or find by location
            weather_entity = weather_entities[0]
            if location:
                location_lower = location.lower()
                for entity in weather_entities:
                    friendly_name = (
                        entity.get("attributes", {}).get("friendly_name", "").lower()
                    )
                    if (
                        location_lower in friendly_name
                        or location_lower in entity["entity_id"]
                    ):
                        weather_entity = entity
                        break

            attributes = weather_entity.get("attributes", {})

            return {
                "entity_id": weather_entity["entity_id"],
                "location": attributes.get("friendly_name", "Unknown"),
                "current_condition": weather_entity.get("state", "unknown"),
                "temperature": attributes.get("temperature"),
                "temperature_unit": attributes.get("temperature_unit"),
                "humidity": attributes.get("humidity"),
                "pressure": attributes.get("pressure"),
                "pressure_unit": attributes.get("pressure_unit"),
                "wind_speed": attributes.get("wind_speed"),
                "wind_speed_unit": attributes.get("wind_speed_unit"),
                "wind_bearing": attributes.get("wind_bearing"),
                "visibility": attributes.get("visibility"),
                "visibility_unit": attributes.get("visibility_unit"),
                "forecast": attributes.get("forecast", [])[:5],  # Next 5 days
                "attribution": attributes.get("attribution"),
                "timestamp": datetime.now().isoformat(),
            }

        except Exception as e:
            logger.error(f"Error getting weather info: {e}")
            return {
                "success": False,
                "error": f"Failed to get weather info: {str(e)}",
                "suggestions": [
                    "Check weather integration is configured",
                    "Verify weather entities are available",
                ],
            }

    async def get_energy_usage(self, period: str = "today") -> dict[str, Any]:
        """
        Get energy usage information from Home Assistant energy entities.

        Args:
            period: Time period ('today', 'yesterday', 'week', 'month')

        Returns:
            Energy usage information
        """
        try:
            # Get energy and power entities
            states = await self.client.get_states()
            energy_entities = []

            for state in states:
                entity_id = state["entity_id"]
                attributes = state.get("attributes", {})
                device_class = attributes.get("device_class", "")
                unit = attributes.get("unit_of_measurement", "")

                # Look for energy/power entities
                if (
                    device_class in ["energy", "power"]
                    or "energy" in entity_id
                    or "power" in entity_id
                    or unit in ["kWh", "W", "kW"]
                ):
                    energy_entities.append(state)

            if not energy_entities:
                return {
                    "success": False,
                    "error": "No energy entities found",
                    "suggestions": [
                        "Add energy monitoring devices",
                        "Configure energy dashboard in Home Assistant",
                        "Set up utility meter entities",
                    ],
                }

            # Group by type
            power_entities = []
            energy_entities_kwh = []

            for entity in energy_entities:
                device_class = entity.get("attributes", {}).get("device_class")
                unit = entity.get("attributes", {}).get("unit_of_measurement", "")

                if device_class == "power" or unit in ["W", "kW"]:
                    power_entities.append(entity)
                elif device_class == "energy" or unit == "kWh":
                    energy_entities_kwh.append(entity)

            # Calculate totals
            total_power = 0.0
            total_energy = 0.0

            for entity in power_entities:
                try:
                    value = float(entity.get("state", 0))
                    if entity.get("attributes", {}).get("unit_of_measurement") == "kW":
                        value *= 1000  # Convert to watts
                    total_power += value
                except (ValueError, TypeError):
                    continue

            for entity in energy_entities_kwh:
                try:
                    value = float(entity.get("state", 0))
                    total_energy += value
                except (ValueError, TypeError):
                    continue

            return {
                "period": period,
                "summary": {
                    "total_power_consumption_w": round(total_power, 2),
                    "total_energy_consumption_kwh": round(total_energy, 2),
                    "estimated_daily_cost": round(
                        total_energy * 0.15, 2
                    ),  # Rough estimate
                },
                "power_entities": [
                    {
                        "entity_id": e["entity_id"],
                        "friendly_name": e.get("attributes", {}).get("friendly_name"),
                        "current_power": e.get("state"),
                        "unit": e.get("attributes", {}).get("unit_of_measurement"),
                    }
                    for e in power_entities[:10]  # Top 10
                ],
                "energy_entities": [
                    {
                        "entity_id": e["entity_id"],
                        "friendly_name": e.get("attributes", {}).get("friendly_name"),
                        "total_energy": e.get("state"),
                        "unit": e.get("attributes", {}).get("unit_of_measurement"),
                    }
                    for e in energy_entities_kwh[:10]  # Top 10
                ],
                "timestamp": datetime.now().isoformat(),
                "note": "Energy data accuracy depends on configured monitoring devices",
            }

        except Exception as e:
            logger.error(f"Error getting energy usage: {e}")
            return {
                "success": False,
                "error": f"Failed to get energy usage: {str(e)}",
                "suggestions": [
                    "Check energy monitoring setup",
                    "Verify power/energy entities exist",
                    "Configure energy dashboard in HA",
                ],
            }


def create_convenience_tools(
    client: HomeAssistantClient | None = None,
) -> ConvenienceTools:
    """Create convenience tools instance."""
    return ConvenienceTools(client)
