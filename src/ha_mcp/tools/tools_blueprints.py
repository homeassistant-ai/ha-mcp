"""
Blueprint management tools for Home Assistant.

This module provides tools for discovering, retrieving, and importing
Home Assistant blueprints for automations and scripts.
"""

import logging
from typing import Annotated, Any

from pydantic import Field

from .helpers import log_tool_usage

logger = logging.getLogger(__name__)


def register_blueprint_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant blueprint management tools."""

    @mcp.tool(annotations={"idempotentHint": True, "readOnlyHint": True, "tags": ["blueprint"], "title": "List Blueprints"})
    @log_tool_usage
    async def ha_list_blueprints(
        domain: Annotated[
            str,
            Field(
                description="Blueprint domain: 'automation' or 'script'",
                default="automation",
            ),
        ] = "automation",
    ) -> dict[str, Any]:
        """
        List installed blueprints for a specific domain.

        Returns all blueprints available in Home Assistant for the specified domain,
        including their paths and metadata.

        EXAMPLES:
        - List automation blueprints: ha_list_blueprints("automation")
        - List script blueprints: ha_list_blueprints("script")

        RETURNS:
        - List of blueprints with path, name, and domain information
        - Each blueprint includes its relative path for use with ha_get_blueprint
        """
        try:
            # Validate domain
            valid_domains = ["automation", "script"]
            if domain not in valid_domains:
                return {
                    "success": False,
                    "error": f"Invalid domain '{domain}'. Must be one of: {', '.join(valid_domains)}",
                    "valid_domains": valid_domains,
                }

            # Send WebSocket command to list blueprints
            response = await client.send_websocket_message(
                {"type": "blueprint/list", "domain": domain}
            )

            if not response.get("success"):
                return {
                    "success": False,
                    "error": response.get("error", "Failed to list blueprints"),
                    "domain": domain,
                }

            # Process the blueprint list
            blueprints_data = response.get("result", {})

            # Convert to a more usable format
            blueprints = []
            for path, metadata in blueprints_data.items():
                blueprint_info = {
                    "path": path,
                    "domain": domain,
                    "name": metadata.get("name", path.split("/")[-1].replace(".yaml", "")),
                }

                # Add optional metadata if available
                if "metadata" in metadata:
                    meta = metadata["metadata"]
                    blueprint_info.update({
                        "description": meta.get("description"),
                        "source_url": meta.get("source_url"),
                        "author": meta.get("author"),
                    })

                blueprints.append(blueprint_info)

            return {
                "success": True,
                "domain": domain,
                "count": len(blueprints),
                "blueprints": blueprints,
            }

        except Exception as e:
            logger.error(f"Error listing blueprints: {e}")
            return {
                "success": False,
                "domain": domain,
                "error": str(e),
                "suggestions": [
                    "Verify Home Assistant connection",
                    "Check if blueprint integration is enabled",
                    f"Use domain 'automation' or 'script' (got '{domain}')",
                ],
            }

    @mcp.tool(annotations={"idempotentHint": True, "readOnlyHint": True, "tags": ["blueprint"], "title": "Get Blueprint Details"})
    @log_tool_usage
    async def ha_get_blueprint(
        path: Annotated[
            str,
            Field(
                description="Blueprint path (e.g., 'homeassistant/motion_light.yaml' or 'custom/my_blueprint.yaml')"
            ),
        ],
        domain: Annotated[
            str,
            Field(
                description="Blueprint domain: 'automation' or 'script'",
                default="automation",
            ),
        ] = "automation",
    ) -> dict[str, Any]:
        """
        Get detailed information about a specific blueprint.

        Retrieves the full blueprint configuration including inputs, triggers,
        conditions, and actions. Use this to understand what a blueprint does
        and what inputs it requires.

        EXAMPLES:
        - Get automation blueprint: ha_get_blueprint("homeassistant/motion_light.yaml", "automation")
        - Get script blueprint: ha_get_blueprint("custom/backup_script.yaml", "script")

        RETURNS:
        - Blueprint metadata (name, description, author, source_url)
        - Input definitions with selectors and defaults
        - Blueprint configuration (triggers, conditions, actions for automations; sequence for scripts)
        """
        try:
            # Validate domain
            valid_domains = ["automation", "script"]
            if domain not in valid_domains:
                return {
                    "success": False,
                    "error": f"Invalid domain '{domain}'. Must be one of: {', '.join(valid_domains)}",
                    "valid_domains": valid_domains,
                }

            # First, list blueprints to check if path exists
            list_response = await client.send_websocket_message(
                {"type": "blueprint/list", "domain": domain}
            )

            if not list_response.get("success"):
                return {
                    "success": False,
                    "error": "Failed to query blueprints",
                    "path": path,
                    "domain": domain,
                }

            blueprints_data = list_response.get("result", {})

            # Check if blueprint exists
            if path not in blueprints_data:
                available_paths = list(blueprints_data.keys())[:10]
                return {
                    "success": False,
                    "error": f"Blueprint not found: {path}",
                    "path": path,
                    "domain": domain,
                    "available_blueprints": available_paths,
                    "suggestions": [
                        "Use ha_list_blueprints() to see available blueprints",
                        "Check the path format (e.g., 'homeassistant/motion_light.yaml')",
                    ],
                }

            # Get the blueprint details from the list response
            blueprint_data = blueprints_data[path]

            # Extract and format blueprint information
            result = {
                "success": True,
                "path": path,
                "domain": domain,
                "name": blueprint_data.get("name", path.split("/")[-1].replace(".yaml", "")),
            }

            # Add metadata if available
            if "metadata" in blueprint_data:
                meta = blueprint_data["metadata"]
                result["metadata"] = {
                    "name": meta.get("name"),
                    "description": meta.get("description"),
                    "source_url": meta.get("source_url"),
                    "author": meta.get("author"),
                    "domain": meta.get("domain"),
                    "homeassistant": meta.get("homeassistant"),
                }

                # Add input definitions
                if "input" in meta:
                    result["inputs"] = meta["input"]

            # Add blueprint configuration if available
            if "blueprint" in blueprint_data:
                result["blueprint"] = blueprint_data["blueprint"]

            return result

        except Exception as e:
            logger.error(f"Error getting blueprint: {e}")
            return {
                "success": False,
                "path": path,
                "domain": domain,
                "error": str(e),
                "suggestions": [
                    "Verify the blueprint path is correct",
                    "Use ha_list_blueprints() to find available blueprints",
                    "Check Home Assistant connection",
                ],
            }

    @mcp.tool(annotations={"destructiveHint": True, "tags": ["blueprint"], "title": "Import Blueprint"})
    @log_tool_usage
    async def ha_import_blueprint(
        url: Annotated[
            str,
            Field(
                description="URL to import blueprint from (GitHub, Home Assistant Community, or direct YAML URL)"
            ),
        ],
    ) -> dict[str, Any]:
        """
        Import a blueprint from a URL.

        Imports a blueprint from GitHub, Home Assistant Community forums,
        or any direct URL to a blueprint YAML file.

        EXAMPLES:
        - Import from GitHub: ha_import_blueprint("https://github.com/user/repo/blob/main/blueprint.yaml")
        - Import from HA Community: ha_import_blueprint("https://community.home-assistant.io/t/motion-light/123456")
        - Import direct YAML: ha_import_blueprint("https://example.com/my-blueprint.yaml")

        SUPPORTED SOURCES:
        - GitHub repository URLs (will be converted to raw URLs)
        - Home Assistant Community forum posts with blueprint code
        - Direct URLs to YAML blueprint files

        RETURNS:
        - Import result with the blueprint path where it was saved
        - Blueprint metadata (name, domain, description)
        - Error details if import fails
        """
        try:
            # Validate URL format
            if not url.startswith(("http://", "https://")):
                return {
                    "success": False,
                    "error": "Invalid URL format. URL must start with http:// or https://",
                    "url": url,
                }

            # Send WebSocket command to import blueprint
            response = await client.send_websocket_message(
                {"type": "blueprint/import", "url": url}
            )

            if not response.get("success"):
                error_msg = response.get("error", "Failed to import blueprint")

                # Provide helpful error messages based on common issues
                suggestions = [
                    "Verify the URL is accessible",
                    "Ensure the URL points to a valid blueprint YAML file",
                    "Check if the blueprint format is compatible with your Home Assistant version",
                ]

                if "already exists" in str(error_msg).lower():
                    suggestions.insert(0, "Blueprint already exists - use ha_list_blueprints() to see installed blueprints")

                return {
                    "success": False,
                    "error": error_msg,
                    "url": url,
                    "suggestions": suggestions,
                }

            # Extract import result
            result_data = response.get("result", {})

            return {
                "success": True,
                "url": url,
                "imported_blueprint": {
                    "path": result_data.get("suggested_filename") or result_data.get("path"),
                    "domain": result_data.get("blueprint", {}).get("domain", "automation"),
                    "name": result_data.get("blueprint", {}).get("name"),
                    "description": result_data.get("blueprint", {}).get("description"),
                },
                "message": "Blueprint imported successfully. Use ha_list_blueprints() to see all installed blueprints.",
            }

        except Exception as e:
            logger.error(f"Error importing blueprint: {e}")
            return {
                "success": False,
                "url": url,
                "error": str(e),
                "suggestions": [
                    "Verify the URL is correct and accessible",
                    "Check if the URL points to a valid YAML blueprint file",
                    "Ensure Home Assistant has internet access",
                    "Try importing from a different source (GitHub, Community, direct URL)",
                ],
            }
