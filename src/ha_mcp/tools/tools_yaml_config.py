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

_LOVELACE_DASHBOARD_PREFIX = "lovelace.dashboards."


async def _check_storage_mode_dashboard_collision(
    client: Any, yaml_path: str
) -> None:
    """Raise a ToolError if a storage-mode dashboard already owns the requested
    url_path; otherwise return without doing anything.

    Only runs for yaml_path values starting with 'lovelace.dashboards.'.
    A WebSocket failure or unexpected response shape warns and skips the check
    (fail-open) so that a transient HA outage doesn't block dashboard creation.
    """
    if not yaml_path.startswith(_LOVELACE_DASHBOARD_PREFIX):
        return
    url_path = yaml_path[len(_LOVELACE_DASHBOARD_PREFIX):]
    try:
        result = await client.send_websocket_message(
            {"type": "lovelace/dashboards/list"}
        )
    except Exception as exc:
        logger.warning(
            "lovelace/dashboards/list WS query failed (%s); skipping collision check",
            exc,
        )
        return

    if isinstance(result, dict) and "result" in result:
        dashboards = result["result"]
    elif isinstance(result, list):
        dashboards = result
    else:
        logger.warning(
            "lovelace/dashboards/list returned unexpected shape (%s); "
            "skipping collision check",
            type(result).__name__,
        )
        return

    for entry in dashboards or []:
        if (
            isinstance(entry, dict)
            and entry.get("url_path") == url_path
            and entry.get("mode") == "storage"
        ):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    (
                        f"A storage-mode dashboard already owns url_path "
                        f"'{url_path}'. Delete it via ha_config_delete_dashboard "
                        "or pick a different url_path before registering a "
                        "YAML-mode dashboard."
                    ),
                    context={"url_path": url_path, "existing_id": entry.get("id")},
                    suggestions=[
                        f"ha_config_delete_dashboard(url_path='{url_path}')",
                        "Pick a different url_path for your YAML-mode dashboard.",
                    ],
                )
            )


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
        tags={"System", "beta"},
        annotations={
            "destructiveHint": True,
            "idempotentHint": False,
            "title": "Raw YAML Config Edit",
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
                    "'rest', 'shell_command', 'notify'). For YAML-mode dashboards, "
                    "use the dotted form 'lovelace.dashboards.<url_path>' where "
                    "<url_path> is lowercase, hyphenated, and not a reserved HA "
                    "route. No other dotted paths are supported. Not for template "
                    "sensors (use ha_config_set_helper), automations, scripts, "
                    "scenes, or input_* helpers — those have dedicated tools."
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
        """Update raw YAML configuration in configuration.yaml or packages/*.yaml (LAST RESORT).

        **WARNING:** Destructive, disabled by default. Dedicated tools exist for
        almost every use case and should be preferred:

        - Template sensors (state-based or trigger-based) ->
          ha_config_set_helper(helper_type='template')
        - Automations -> ha_config_set_automation
        - Scripts -> ha_config_set_script
        - Scenes -> ha_config_set_scene
        - All 27 helper types (input_*, counter, timer, schedule, zone, person,
          tag, group, min_max, threshold, derivative, statistics, utility_meter,
          trend, filter, switch_as_x, etc.) -> ha_config_set_helper

        Intended for YAML-only integrations with no config-flow or API
        equivalent (command_line, rest, shell_command, notify platforms),
        and for registering YAML-mode dashboards via
        ``lovelace.dashboards.<url_path>`` (no other ``lovelace.*`` keys).
        Check ``post_action`` in the response: most keys need a full HA
        restart; template, mqtt, and group support reload. Preserves YAML
        comments and HA tags (``!include``, ``!secret``) on round-trip;
        ``replace`` swaps the subtree as-is.

        For detailed routing guidance, use ha_get_skill_home_assistant_best_practices.
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

            # Storage-mode dashboard collision check (only for lovelace.dashboards.*).
            # Skip on `remove` so users can clean up YAML entries that conflict
            # with a storage-mode dashboard (e.g., during a migration).
            if action in ("add", "replace"):
                await _check_storage_mode_dashboard_collision(client, yaml_path)

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
