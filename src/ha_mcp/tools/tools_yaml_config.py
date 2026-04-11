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
            "title": "Raw YAML Config Edit (Escape Hatch)",
        },
    )
    @log_tool_usage
    async def ha_config_set_yaml(
        yaml_path: Annotated[
            str,
            Field(
                description=(
                    "Top-level YAML key to modify. Only a narrow allowlist of "
                    "YAML-only integration keys is accepted (e.g., 'command_line', "
                    "'rest', 'shell_command', 'notify'). "
                    "STOP before using this for 'template' — the Template "
                    "config-flow helper (ha_set_config_entry_helper with "
                    "helper_type='template') supports state AND trigger-based template "
                    "sensors since HA 2024.x and is the correct path. Do NOT use "
                    "this tool for automations, scripts, scenes, or input_* "
                    "helpers — they have dedicated tools "
                    "(ha_config_set_automation, ha_config_set_script, "
                    "ha_config_set_scene, ha_config_set_helper)."
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
        justification: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Required. Explain in one or two sentences why no dedicated "
                    "tool (ha_set_config_entry_helper for templates, "
                    "ha_config_set_automation, ha_config_set_script, "
                    "ha_config_set_scene, ha_config_set_helper) can accomplish "
                    "this task. Logged for auditing. If you cannot write a "
                    "concrete justification, you probably want a different tool."
                ),
            ),
        ] = None,
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
        """Update raw YAML configuration (Escape Hatch) — use only when NO dedicated tool fits.

        This tool is the WRONG answer for almost everything. Before calling it,
        confirm that NONE of these apply:

        - Template sensors (state-based OR trigger-based) ->
          ha_set_config_entry_helper with helper_type='template'. The Template
          config-flow helper supports triggers, availability, attributes, device
          class, unit of measurement, and state templates since HA 2024.x. It is
          the correct path even for complex trigger-based sensors. Do NOT edit
          the 'template:' YAML block for this.
        - Automations -> ha_config_set_automation
        - Scripts -> ha_config_set_script
        - Scenes -> ha_config_set_scene
        - Input helpers (input_boolean, input_number, input_text, input_select,
          input_datetime, input_button, counter, timer, schedule) ->
          ha_config_set_helper
        - Groups, min/max, threshold, derivative, statistics, utility_meter,
          trend, filter, switch_as_x, and other config-flow helpers ->
          ha_set_config_entry_helper

        This tool is intended for YAML-only integrations that have no config-flow
        or API equivalent: command_line sensors, REST sensors defined in
        packages, shell_command entries, platform-based notify services, and
        similar edge cases. If you are reaching for this to edit 'template:',
        'automation:', 'script:', 'scene:', or 'input_*:', stop and use the
        dedicated tool instead.

        A ``justification`` is REQUIRED on every call and is logged for auditing.
        The justification must explain why no dedicated tool fits. If you cannot
        write a concrete justification, you are using the wrong tool.

        Safeguards: file backup, YAML validation, top-level key allowlist,
        path traversal blocking, post-edit config check.

        Check 'post_action' in the response. Most keys require a full HA restart
        ('restart_required'). Only template, mqtt, and group support reload
        ('reload_available' with 'reload_service').

        Preserves YAML comments on sibling keys, file-level comments, and Home
        Assistant tags (!include, !secret, etc.). The 'replace' action
        substitutes the subtree as-is, so comments from the old subtree do not
        carry over.
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

            # Require a non-empty justification. This is the primary friction
            # that discourages reaching for this escape hatch — if the caller
            # cannot articulate why no dedicated tool fits, they should use a
            # dedicated tool instead. Mirrors the pattern from tools_code.py
            # (ha_manage_custom_tool).
            if not justification or not justification.strip():
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "justification is required for ha_config_set_yaml",
                        suggestions=[
                            "Explain why no dedicated tool can accomplish this "
                            "task (Template sensors -> "
                            "ha_set_config_entry_helper with "
                            "helper_type='template'; automations -> "
                            "ha_config_set_automation; scripts -> "
                            "ha_config_set_script; helpers -> "
                            "ha_config_set_helper)",
                            "If yaml_path is 'template', 'automation', "
                            "'script', 'scene', or 'input_*', use the "
                            "dedicated tool instead of this escape hatch",
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

            logger.info(
                "ha_config_set_yaml invoked (yaml_path=%s, action=%s, file=%s) — justification: %s",
                yaml_path,
                action,
                file,
                justification[:200],
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
