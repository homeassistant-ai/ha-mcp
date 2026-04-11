"""
Blueprint management tools for Home Assistant.

This module provides tools for discovering, retrieving, and importing
Home Assistant blueprints for automations and scripts.
"""

import logging
from typing import Annotated, Any

from fastmcp.exceptions import ToolError
from fastmcp.tools import tool
from pydantic import Field

from ..errors import ErrorCode, create_error_response
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)

logger = logging.getLogger(__name__)


class BlueprintTools:
    """Blueprint management tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @staticmethod
    def _format_blueprint_list(blueprints_data: dict[str, Any], domain: str) -> dict[str, Any]:
        """Format blueprint data into list response structure.

        Args:
            blueprints_data: Raw blueprint data from WebSocket API
            domain: Blueprint domain (automation or script)

        Returns:
            Formatted response with blueprints list, count, and domain
        """
        blueprints = []
        for bp_path, metadata in blueprints_data.items():
            blueprint_info = {
                "path": bp_path,
                "domain": domain,
                "name": metadata.get("name", bp_path.split("/")[-1].replace(".yaml", "")),
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

    @tool(
        name="ha_get_blueprint",
        tags={"Blueprints"},
        annotations={"idempotentHint": True, "readOnlyHint": True, "title": "Get Blueprint"},
    )
    @log_tool_usage
    async def ha_get_blueprint(
        self,
        path: Annotated[
            str | None,
            Field(
                description="Blueprint path to get details for (e.g., 'homeassistant/motion_light.yaml'). "
                "If omitted, lists all blueprints in the domain.",
                default=None,
            ),
        ] = None,
        domain: Annotated[
            str,
            Field(
                description="Blueprint domain: 'automation' or 'script'",
                default="automation",
            ),
        ] = "automation",
    ) -> dict[str, Any]:
        """
        Get blueprint information - list all blueprints or get details for a specific one.

        Without a path: Lists all installed blueprints for the specified domain.
        With a path: Retrieves full blueprint configuration including inputs, triggers,
        conditions, and actions.

        EXAMPLES:
        - List all automation blueprints: ha_get_blueprint(domain="automation")
        - List script blueprints: ha_get_blueprint(domain="script")
        - Get specific blueprint: ha_get_blueprint(path="homeassistant/motion_light.yaml", domain="automation")

        RETURNS (when listing):
        - List of blueprints with path, name, and domain information
        - Count of blueprints found

        RETURNS (when getting specific blueprint):
        - Blueprint metadata (name, description, author, source_url)
        - Input definitions with selectors and defaults
        - Blueprint configuration (triggers, conditions, actions for automations; sequence for scripts)
        """
        try:
            # Validate domain
            valid_domains = ["automation", "script"]
            if domain not in valid_domains:
                raise_tool_error(create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Invalid domain '{domain}'. Must be one of: {', '.join(valid_domains)}",
                    context={"domain": domain, "valid_domains": valid_domains},
                ))

            # Get list of blueprints
            list_response = await self._client.send_websocket_message(
                {"type": "blueprint/list", "domain": domain}
            )

            if not list_response.get("success"):
                raise_tool_error(create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    list_response.get("error", "Failed to query blueprints"),
                    context={"domain": domain},
                ))

            blueprints_data = list_response.get("result", {})

            # If no path provided, return list of all blueprints
            if path is None:
                return self._format_blueprint_list(blueprints_data, domain)

            # Path provided - get specific blueprint details
            if path not in blueprints_data:
                available_paths = list(blueprints_data.keys())[:10]
                raise_tool_error(create_error_response(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    f"Blueprint not found: {path}",
                    context={"path": path, "domain": domain, "available_blueprints": available_paths},
                    suggestions=[
                        "Use ha_get_blueprint() without path to see all available blueprints",
                        "Check the path format (e.g., 'homeassistant/motion_light.yaml')",
                    ],
                ))

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

        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={"path": path, "domain": domain},
                suggestions=[
                    "Verify the blueprint path is correct",
                    "Use ha_get_blueprint() without path to see available blueprints",
                    "Check Home Assistant connection",
                ],
            )

    @tool(
        name="ha_import_blueprint",
        tags={"Blueprints"},
        annotations={"destructiveHint": True, "title": "Import Blueprint"},
    )
    @log_tool_usage
    async def ha_import_blueprint(
        self,
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
                raise_tool_error(create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "Invalid URL format. URL must start with http:// or https://",
                    context={"url": url},
                ))

            # Send WebSocket command to import blueprint
            response = await self._client.send_websocket_message(
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
                    suggestions.insert(0, "Blueprint already exists - use ha_get_blueprint() to see installed blueprints")

                raise_tool_error(create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    error_msg,
                    context={"url": url},
                    suggestions=suggestions,
                ))

            # Extract import result (blueprint/import only validates, does not save)
            result_data = response.get("result", {})
            suggested_filename = result_data.get("suggested_filename", "")
            raw_data = result_data.get("raw_data", "")
            blueprint_meta = result_data.get("blueprint", {}).get("metadata", {})
            domain = blueprint_meta.get("domain", "automation")

            if not suggested_filename or not raw_data:
                raise_tool_error(create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Blueprint validated but no filename or YAML data was returned",
                    context={"url": url},
                    suggestions=[
                        "This may indicate an incompatible blueprint format",
                        "Try a different blueprint URL",
                    ],
                ))

            # Ensure the path has a .yaml extension — HA's blueprint/import returns
            # suggested_filename without the extension (e.g. "user/blueprint_name")
            if not suggested_filename.endswith((".yaml", ".yml")):
                suggested_filename = suggested_filename + ".yaml"

            # Save the blueprint to disk (blueprint/import only validates)
            save_response = await self._client.send_websocket_message(
                {
                    "type": "blueprint/save",
                    "domain": domain,
                    "path": suggested_filename,
                    "yaml": raw_data,
                    "source_url": url,
                }
            )

            if not save_response.get("success"):
                error = save_response.get("error", {})
                save_error = (
                    error.get("message", str(error))
                    if isinstance(error, dict)
                    else str(error)
                )

                suggestions = [
                    "The blueprint was validated but could not be saved to disk",
                    "Use ha_get_blueprint() to check if it already exists",
                ]

                if "already exists" in save_error.lower():
                    suggestions.insert(0, "A blueprint with this path already exists")

                raise_tool_error(create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    save_error,
                    context={"url": url, "path": suggested_filename},
                    suggestions=suggestions,
                ))

            save_result = save_response.get("result") or {}

            return {
                "success": True,
                "url": url,
                "imported_blueprint": {
                    "path": suggested_filename,
                    "domain": domain,
                    "name": blueprint_meta.get("name"),
                    "description": blueprint_meta.get("description"),
                },
                "overrides_existing": save_result.get("overrides_existing", False),
                "message": "Blueprint imported successfully. Use ha_get_blueprint() to see all installed blueprints.",
            }

        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={"url": url},
                suggestions=[
                    "Verify the URL is correct and accessible",
                    "Check if the URL points to a valid YAML blueprint file",
                    "Ensure Home Assistant has internet access",
                    "Try importing from a different source (GitHub, Community, direct URL)",
                ],
            )


def register_blueprint_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant blueprint management tools."""
    register_tool_methods(mcp, BlueprintTools(client))
