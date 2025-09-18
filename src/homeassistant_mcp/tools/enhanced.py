"""
Enhanced documentation and domain information for Home Assistant MCP Tools.

This module provides domain information and documentation helpers.
All MCP tools are now consolidated in tools_registry.py to eliminate duplication.
"""

from typing import Any

# Top 25 Most Critical Home Assistant Domains
TOP_25_DOMAINS = [
    # Critical control domains (8)
    "light",
    "switch",
    "climate",
    "media_player",
    "lock",
    "cover",
    "fan",
    "vacuum",
    # Essential monitoring (4)
    "sensor",
    "binary_sensor",
    "device_tracker",
    "weather",
    # Critical input helpers (5)
    "input_number",
    "input_boolean",
    "input_text",
    "input_datetime",
    "input_select",
    # Automation core (3)
    "automation",
    "script",
    "scene",
    # High importance (5)
    "camera",
    "alarm_control_panel",
    "button",
    "siren",
    "timer",
]


class EnhancedToolsMixin:
    """Mixin class to add enhanced documentation and domain information to MCP server."""

    def register_enhanced_tools(self) -> None:
        """Enhanced tools are now consolidated in tools_registry.py to eliminate duplication."""
        # This mixin now focuses on domain information and documentation helpers
        pass

    def get_domain_info(self, domain: str) -> dict[str, Any]:
        """Get domain-specific information for enhanced documentation."""
        return self._get_domain_info(domain)

    def get_domain_insights(
        self, domain: str, entity_state: dict[str, Any]
    ) -> dict[str, Any]:
        """Get domain-specific insights for entity state."""
        return self._get_domain_insights(domain, entity_state)

    def get_domain_actions(self, domain: str) -> list[str]:
        """Get available actions for domain."""
        return self._get_domain_actions(domain)

    def get_parameter_guidance(
        self, domain: str, entity_state: dict[str, Any]
    ) -> dict[str, Any]:
        """Get parameter guidance for domain."""
        return self._get_parameter_guidance(domain, entity_state)

    # Helper methods for enhanced functionality

    def _get_domain_info(self, domain: str) -> dict[str, Any]:
        """Get domain-specific information."""
        domain_info = {
            "light": {
                "type": "control",
                "complexity": "high",
                "parameters": ["brightness", "color", "temperature"],
            },
            "switch": {
                "type": "control",
                "complexity": "low",
                "parameters": ["basic_on_off"],
            },
            "climate": {
                "type": "control",
                "complexity": "high",
                "parameters": ["temperature", "mode", "preset"],
            },
            "input_boolean": {
                "type": "input",
                "complexity": "low",
                "parameters": ["toggle"],
            },
            "input_number": {
                "type": "input",
                "complexity": "medium",
                "parameters": ["value", "min", "max"],
            },
            "sensor": {
                "type": "monitoring",
                "complexity": "low",
                "parameters": ["read_only"],
            },
            "automation": {
                "type": "system",
                "complexity": "medium",
                "parameters": ["trigger", "enable"],
            },
        }

        return domain_info.get(
            domain, {"type": "unknown", "complexity": "medium", "parameters": ["basic"]}
        )

    def _get_domain_insights(
        self, domain: str, entity_state: dict[str, Any]
    ) -> dict[str, Any]:
        """Get domain-specific insights for entity state."""
        insights: dict[str, Any] = {"domain": domain, "recommendations": []}

        if domain == "light":
            if entity_state.get("state") == "on":
                insights["recommendations"].append(
                    "Can adjust brightness, color, or turn off"
                )
            else:
                insights["recommendations"].append(
                    "Can turn on with optional brightness/color"
                )

        elif domain == "input_boolean":
            current_state = entity_state.get("state")
            insights["recommendations"].append(
                f"Can toggle from {current_state} to {'off' if current_state == 'on' else 'on'}"
            )

        elif domain == "input_number":
            current_value = entity_state.get("state", 0)
            min_val = entity_state.get("attributes", {}).get("min", 0)
            max_val = entity_state.get("attributes", {}).get("max", 100)
            insights["recommendations"].append(
                f"Can set value between {min_val}-{max_val} (current: {current_value})"
            )

        return insights

    def _get_domain_actions(self, domain: str) -> list[str]:
        """Get available actions for domain."""
        actions = {
            "light": ["turn_on", "turn_off", "toggle"],
            "switch": ["turn_on", "turn_off", "toggle"],
            "climate": ["set_temperature", "set_hvac_mode", "set_preset_mode"],
            "input_boolean": ["turn_on", "turn_off", "toggle"],
            "input_number": ["set_value", "increment", "decrement"],
            "input_text": ["set_value"],
            "automation": ["trigger", "turn_on", "turn_off"],
            "scene": ["turn_on"],
        }

        return actions.get(domain, ["turn_on", "turn_off"])

    def _get_parameter_guidance(
        self, domain: str, entity_state: dict[str, Any]
    ) -> dict[str, Any]:
        """Get parameter guidance for domain."""
        guidance = {}

        if domain == "light":
            guidance = {
                "brightness_pct": "0-100 percentage (user-friendly)",
                "color_temp_kelvin": "2000-6500K (warm to cool)",
                "rgb_color": "[red, green, blue] values 0-255 each",
            }
        elif domain == "climate":
            attributes = entity_state.get("attributes", {})
            guidance = {
                "temperature": f"Range: {attributes.get('min_temp', 'unknown')}-{attributes.get('max_temp', 'unknown')}",
                "hvac_mode": f"Options: {attributes.get('hvac_modes', 'unknown')}",
                "preset_mode": f"Options: {attributes.get('preset_modes', 'unknown')}",
            }
        elif domain == "input_number":
            attributes = entity_state.get("attributes", {})
            guidance = {
                "value": f"Range: {attributes.get('min', 0)}-{attributes.get('max', 100)}, Step: {attributes.get('step', 1)}"
            }

        return guidance
