"""
Dashboard documentation resources for MCP.

Provides access to curated dashboard guide and card documentation
via MCP resources for AI agents using dashboard tools.
"""

import json
import logging
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# Base path for resource files
RESOURCES_DIR = Path(__file__).parent

# Card documentation base URL
CARD_DOCS_BASE_URL = (
    "https://raw.githubusercontent.com/home-assistant/home-assistant.io/"
    "refs/heads/current/source/_dashboards"
)


def register_dashboard_resources(mcp) -> None:
    """Register all dashboard-related MCP resources.

    Args:
        mcp: FastMCP server instance
    """

    @mcp.resource("ha-dashboard://guide")
    def get_dashboard_guide() -> str:
        """Curated dashboard configuration guide for AI agents.

        Covers critical validation rules, structure, view types, card categories,
        features, actions, visibility, strategy-based dashboards, and common pitfalls.
        """
        guide_path = RESOURCES_DIR / "dashboard_guide.md"
        return guide_path.read_text()

    @mcp.resource("ha-dashboard://card-types")
    def get_card_types() -> dict:
        """List of all available Home Assistant dashboard card types.

        Returns JSON with card type names and documentation URLs.
        """
        types_path = RESOURCES_DIR / "card_types.json"
        return json.loads(types_path.read_text())

    @mcp.resource("ha-dashboard://card-docs/{card_type}")
    async def get_card_documentation(card_type: str) -> str:
        """Fetch card-specific documentation from Home Assistant docs.

        Args:
            card_type: Card type name (e.g., "light", "thermostat", "entity")

        Returns:
            Raw markdown documentation for the specified card type

        Examples:
            ha-dashboard://card-docs/light
            ha-dashboard://card-docs/thermostat
            ha-dashboard://card-docs/entity
        """
        # Validate card type exists
        types_path = RESOURCES_DIR / "card_types.json"
        card_types_data = json.loads(types_path.read_text())

        if card_type not in card_types_data["card_types"]:
            available = ", ".join(card_types_data["card_types"][:10])
            return f"Error: Unknown card type '{card_type}'.\n\nAvailable types include: {available}...\n\nSee ha-dashboard://card-types for full list."

        # Fetch documentation from GitHub
        doc_url = f"{CARD_DOCS_BASE_URL}/{card_type}.markdown"

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(doc_url)
                response.raise_for_status()
                return response.text
        except httpx.HTTPStatusError as e:
            logger.error(f"Failed to fetch card docs for {card_type}: {e}")
            return f"Error: Failed to fetch documentation for '{card_type}' card.\n\nStatus: {e.response.status_code}\nURL: {doc_url}"
        except Exception as e:
            logger.error(f"Error fetching card docs for {card_type}: {e}")
            return f"Error: Failed to fetch documentation for '{card_type}' card.\n\nError: {str(e)}"

    logger.info("✅ Registered dashboard resources: guide, card-types, card-docs/{card_type}")
