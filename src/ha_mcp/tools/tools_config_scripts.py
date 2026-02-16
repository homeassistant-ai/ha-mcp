"""
Configuration management tools for Home Assistant scripts.

This module provides tools for retrieving, creating, updating, and removing
Home Assistant script configurations.
"""

import logging
from typing import Annotated, Any, cast

from pydantic import Field

from .helpers import log_tool_usage
from .util_helpers import (
    coerce_bool_param,
    parse_json_param,
    validate_guide_response,
    wait_for_entity_registered,
    wait_for_entity_removed,
)

logger = logging.getLogger(__name__)


def _strip_empty_script_fields(config: dict[str, Any]) -> dict[str, Any]:
    """
    Strip empty sequence array from script config.

    Blueprint-based scripts should not have a sequence field since this comes
    from the blueprint itself. If an empty array is present, it overrides the
    blueprint's configuration and breaks the script.

    Args:
        config: Script configuration dict

    Returns:
        Configuration with empty sequence array removed
    """
    cleaned = config.copy()

    # Remove empty sequence array for blueprint scripts
    if "sequence" in cleaned and cleaned["sequence"] == []:
        del cleaned["sequence"]

    return cleaned


def register_config_script_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant script configuration tools."""

    @mcp.tool(
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "tags": ["script"],
            "title": "Get Script Config",
        }
    )
    @log_tool_usage
    async def ha_config_get_script(
        script_id: Annotated[
            str, Field(description="Script identifier (e.g., 'morning_routine')")
        ],
    ) -> dict[str, Any]:
        """
        Retrieve Home Assistant script configuration.

        Returns the complete configuration for a script, including sequence, mode, fields, and other settings.

        EXAMPLES:
        - Get script: ha_config_get_script("morning_routine")
        - Get script: ha_config_get_script("backup_script")

        For detailed script configuration help, use: ha_get_domain_docs("script")
        """
        try:
            config_result = await client.get_script_config(script_id)
            return {
                "success": True,
                "action": "get",
                "script_id": script_id,
                "config": config_result,
            }
        except Exception as e:
            logger.error(f"Error getting script: {e}")
            return {
                "success": False,
                "action": "get",
                "script_id": script_id,
                "error": str(e),
                "suggestions": [
                    "Verify script_id exists using ha_search_entities(domain_filter='script')",
                    "Check Home Assistant connection",
                    "Use ha_get_domain_docs('script') for configuration help",
                ],
            }

    @mcp.tool(
        annotations={
            "destructiveHint": True,
            "tags": ["script"],
            "title": "Create or Update Script",
        }
    )
    @log_tool_usage
    async def ha_config_set_script(
        script_id: str,
        config: str | dict[str, Any],
        guide_response: Annotated[
            str | dict[str, Any],
            Field(
                description="REQUIRED: Output from ha_get_tool_guide('script')"
            ),
        ],
        wait: Annotated[
            bool | str,
            Field(
                default=True,
            ),
        ] = True,
    ) -> dict[str, Any]:
        """Create or update a script. Config must include 'sequence' or 'use_blueprint'.

        REQUIRED: You MUST call ha_get_tool_guide("script") before using this tool.
        The guide contains examples, field details, and critical
        warnings about native actions vs templates.
        Optional: alias, description, icon, mode (single/restart/queued/parallel), max, fields.
        Also: ha_get_domain_docs("script") for HA action/sequence reference."""
        try:
            # Validate guide_response - enforces ha_get_tool_guide() was called first
            try:
                validate_guide_response(guide_response, "script")
            except ValueError as e:
                return {"success": False, "error": str(e)}

            # Parse JSON config if provided as string
            try:
                parsed_config = parse_json_param(config, "config")
            except ValueError as e:
                return {
                    "success": False,
                    "error": f"Invalid config parameter: {e}",
                    "provided_config_type": type(config).__name__,
                }

            # Ensure config is a dict
            if parsed_config is None or not isinstance(parsed_config, dict):
                return {
                    "success": False,
                    "error": "Config parameter must be a JSON object",
                    "provided_type": type(parsed_config).__name__,
                }

            config_dict = cast(dict[str, Any], parsed_config)

            # Validate required fields based on script type
            # Blueprint scripts only need use_blueprint, regular scripts need sequence
            if "use_blueprint" in config_dict:
                # Strip empty sequence array that would override blueprint
                config_dict = _strip_empty_script_fields(config_dict)
            elif "sequence" not in config_dict:
                return {
                    "success": False,
                    "error": "config must include either 'sequence' field (for regular scripts) or 'use_blueprint' field (for blueprint-based scripts)",
                    "required_fields": ["sequence OR use_blueprint"],
                }

            result = await client.upsert_script_config(config_dict, script_id)

            # Wait for script to be queryable
            wait_bool = coerce_bool_param(wait, "wait", default=True)
            entity_id = f"script.{script_id}"
            if wait_bool:
                try:
                    registered = await wait_for_entity_registered(client, entity_id)
                    if not registered:
                        result["warning"] = f"Script created but {entity_id} not yet queryable. It may take a moment to become available."
                except Exception as e:
                    result["warning"] = f"Script created but verification failed: {e}"

            return {
                "success": True,
                **result,
            }

        except Exception as e:
            logger.error(f"Error upserting script: {e}")
            return {
                "success": False,
                "script_id": script_id,
                "error": str(e),
                "suggestions": [
                    "Ensure config includes either 'sequence' field (regular scripts) or 'use_blueprint' field (blueprint-based scripts)",
                    "For blueprint scripts, use ha_get_blueprint(domain='script') to list available blueprints",
                    "Validate sequence actions syntax for regular scripts",
                    "Check entity_ids exist if using service calls",
                    "Use ha_search_entities(domain_filter='script') to find scripts",
                    "Use ha_get_domain_docs('script') for configuration help",
                ],
            }

    @mcp.tool(
        annotations={
            "destructiveHint": True,
            "idempotentHint": True,
            "tags": ["script"],
            "title": "Remove Script",
        }
    )
    @log_tool_usage
    async def ha_config_remove_script(
        script_id: Annotated[
            str, Field(description="Script identifier to delete (e.g., 'old_script')")
        ],
        wait: Annotated[
            bool | str,
            Field(
                description="Wait for script to be fully removed before returning. Default: True.",
                default=True,
            ),
        ] = True,
    ) -> dict[str, Any]:
        """
        Delete a Home Assistant script.

        EXAMPLES:
        - Delete script: ha_config_remove_script("old_script")
        - Delete script: ha_config_remove_script("temporary_script")

        **IMPORTANT LIMITATION:**
        This tool can only delete scripts created via the Home Assistant UI.
        Scripts defined in YAML configuration files (scripts.yaml or configuration.yaml)
        cannot be deleted through the API and will return a 405 Method Not Allowed error.

        To remove YAML-defined scripts, you must edit the configuration file directly.

        **WARNING:** Deleting a script that is used by automations may cause those automations to fail.
        """
        try:
            result = await client.delete_script_config(script_id)

            # Wait for script to be removed
            wait_bool = coerce_bool_param(wait, "wait", default=True)
            entity_id = f"script.{script_id}"
            if wait_bool:
                try:
                    removed = await wait_for_entity_removed(client, entity_id)
                    if not removed:
                        result["warning"] = f"Deletion confirmed by API but {entity_id} may still appear briefly."
                except Exception as e:
                    result["warning"] = f"Deletion confirmed but removal verification failed: {e}"

            return {"success": True, "action": "delete", **result}
        except Exception as e:
            logger.error(f"Error deleting script: {e}")
            return {
                "success": False,
                "action": "delete",
                "script_id": script_id,
                "error": str(e),
                "suggestions": [
                    "Verify script_id exists using ha_search_entities(domain_filter='script')",
                    "Check if script is being used by automations",
                    "Use ha_get_domain_docs('script') for configuration help",
                ],
            }
