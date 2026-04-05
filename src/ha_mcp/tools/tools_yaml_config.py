"""
Managed YAML configuration editing tools for Home Assistant MCP Server.

Provides a structured, validated tool for editing YAML configuration files
(configuration.yaml and package files) for Home Assistant features that exist
only in YAML and have no REST/WebSocket API equivalent.

**Dependency:** Requires the ha_mcp_tools custom component to be installed.
The tools will gracefully fail with installation instructions if the component is not available.

Feature Flag: Set ENABLE_YAML_CONFIG_EDITING=true to enable.
"""

import logging
from typing import Annotated, Any

from fastmcp.exceptions import ToolError
from pydantic import Field

from ..config import get_global_settings
from ..errors import ErrorCode, create_error_response
from .helpers import exception_to_structured_error, log_tool_usage, raise_tool_error
from .tools_filesystem import (
    MCP_TOOLS_DOMAIN,
    _assert_mcp_tools_available,
)
from .util_helpers import coerce_bool_param, unwrap_service_response

logger = logging.getLogger(__name__)


def register_yaml_config_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register YAML config editing tools with the MCP server.

    Requires ENABLE_YAML_CONFIG_EDITING=true.
    """
    settings = get_global_settings()
    if not settings.enable_yaml_config_editing:
        logger.debug(
            "YAML config tools disabled (set ENABLE_YAML_CONFIG_EDITING=true to enable)"
        )
        return

    logger.info("YAML config editing tools enabled")

    @mcp.tool(
        tags={"System"},
        annotations={
            "destructiveHint": True,
            "idempotentHint": False,
            "title": "Set YAML Config",
        },
    )
    @log_tool_usage
    async def ha_config_set_yaml(
        yaml_path: Annotated[
            str,
            Field(
                description=(
                    "Top-level YAML key to modify (e.g., 'template', 'sensor', "
                    "'input_boolean'). Only whitelisted keys are allowed."
                ),
            ),
        ],
        action: Annotated[
            str,
            Field(
                description=(
                    "Action to perform: 'add' (insert/merge content under key), "
                    "'replace' (overwrite key with new content), or "
                    "'remove' (delete the key entirely)."
                ),
            ),
        ],
        content: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "YAML content for the value under yaml_path. Required for "
                    "'add' and 'replace' actions. Must be valid YAML."
                ),
            ),
        ] = None,
        file: Annotated[
            str,
            Field(
                default="configuration.yaml",
                description=(
                    "Relative path to the YAML config file. Defaults to "
                    "'configuration.yaml'. Also supports 'packages/*.yaml'."
                ),
            ),
        ] = "configuration.yaml",
        backup: Annotated[
            bool | str,
            Field(
                default=True,
                description=(
                    "Create a backup before editing. Defaults to True. "
                    "Backups are saved to www/yaml_backups/."
                ),
            ),
        ] = True,
    ) -> dict[str, Any]:
        """Add, replace, or remove a top-level key in configuration.yaml or package files.

        IMPORTANT: Only use when NO UI or API alternative exists. Prefer:
        - Template sensors -> ha_config_set_helper (Template Helper)
        - Automations -> ha_config_set_automation
        - Scripts -> ha_config_set_script
        - Input helpers -> ha_config_set_helper
        - Scenes -> ha_config_set_scene

        This tool is for YAML-only features with no UI/API path (e.g.,
        command_line sensors, platform-based MQTT sensors in YAML, rest
        sensors defined in packages).

        Safeguards: file backup, YAML validation, top-level key whitelist,
        path traversal blocking, post-edit config check.

        IMPORTANT: Check 'post_action' in the response. Most keys require
        a full HA restart ('restart_required'). Only template, mqtt, and
        group support reload ('reload_available' with 'reload_service').

        Note: YAML comments are not preserved after editing. The backup
        retains the original file with comments intact.
        """
        try:
            # Validate action
            valid_actions = ("add", "replace", "remove")
            if action not in valid_actions:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"Invalid action '{action}'. Must be one of: {', '.join(valid_actions)}",
                        suggestions=[
                            "Use action='add' to insert content under a key",
                            "Use action='replace' to overwrite a key's content",
                            "Use action='remove' to delete a key entirely",
                        ],
                    )
                )

            # Validate content is provided for add/replace
            if action in ("add", "replace") and not content:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"'content' is required for action '{action}'.",
                        suggestions=[
                            "Provide valid YAML content to insert or replace."
                        ],
                    )
                )

            # Coerce boolean parameter
            backup_bool = coerce_bool_param(backup, "backup", default=True)

            # Check if custom component is available
            await _assert_mcp_tools_available(client)

            # Build service data
            service_data: dict[str, Any] = {
                "file": file,
                "action": action,
                "yaml_path": yaml_path,
                "backup": backup_bool,
            }
            if content is not None:
                service_data["content"] = content

            # Call the custom component service
            result = await client.call_service(
                MCP_TOOLS_DOMAIN,
                "edit_yaml_config",
                service_data,
                return_response=True,
            )

            if isinstance(result, dict):
                result = unwrap_service_response(result)
                if not result.get("success", True):
                    raise_tool_error(result)
                return result

            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Unexpected response format from YAML config service",
                    context={"file": file},
                )
            )

        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={
                    "tool": "ha_config_set_yaml",
                    "file": file,
                    "action": action,
                    "yaml_path": yaml_path,
                },
            )
