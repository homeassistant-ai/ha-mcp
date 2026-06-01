"""
Managed YAML configuration editing tools for Home Assistant MCP Server.

Provides a structured, validated tool for editing YAML configuration files
(configuration.yaml and package files) for Home Assistant features that exist
only in YAML and have no REST/WebSocket API equivalent.

**Dependency:** Requires the ha_mcp_tools custom component to be installed.
The tools will gracefully fail with installation instructions if the component is not available.

Feature Flag: Set ENABLE_YAML_CONFIG_EDITING=true to enable.
"""

import fnmatch
import logging
import os
from typing import Annotated, Any

from fastmcp.exceptions import ToolError
from fastmcp.tools import tool
from pydantic import Field

from ..config import get_global_settings
from ..errors import ErrorCode, create_error_response
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)
from .tools_config_dashboards import fetch_dashboards_list
from .tools_filesystem import (
    _assert_mcp_tools_available,
    call_mcp_tools_service,
)
from .util_helpers import (
    attach_skill_content,
    augment_error_dict_with_skill_content,
    augment_tool_error_with_skill_content,
    unwrap_service_response,
)

# YAML packages frequently include template sensors, command_line entities,
# and mqtt templates — exactly where template misuse causes the most
# subtle bugs. template-guidelines.md is the relevant default.
_YAML_SKILL_FILES: tuple[str, ...] = ("references/template-guidelines.md",)

logger = logging.getLogger(__name__)

_LOVELACE_DASHBOARD_PREFIX = "lovelace.dashboards."

# Maps a per-key Settings flag onto the yaml_path top-level segment it
# gates. Keep in lockstep with the custom component's
# PACKAGES_ONLY_YAML_KEYS — every key in that frozenset must appear
# here, otherwise a key would silently be unreachable through the
# wrapper. The parity invariant (keys == PACKAGES_ONLY_YAML_KEYS, and
# every value is a real Settings field) is enforced by
# test_yaml_config_tool.py::test_flag_map_matches_packages_only_keys.
_YAML_PACKAGES_FLAG_BY_KEY = {
    "automation": "enable_yaml_packages_automation",
    "script": "enable_yaml_packages_script",
    "scene": "enable_yaml_packages_scene",
}


def _disabled_packages_keys(settings: Any) -> list[str]:
    """Return the sorted list of PACKAGES_ONLY_YAML_KEYS whose Settings
    flag is currently False.

    Sorted so the value is deterministic in service payloads and test
    assertions; the custom component treats it as a set so order is
    irrelevant on the wire.

    Uses ``getattr`` without a default so a future rename that breaks the
    ``_YAML_PACKAGES_FLAG_BY_KEY`` → ``Settings`` mapping raises loudly
    instead of silently treating the key as disabled (a dead toggle).
    """
    return sorted(
        key
        for key, flag in _YAML_PACKAGES_FLAG_BY_KEY.items()
        if not getattr(settings, flag)
    )


async def _check_storage_mode_dashboard_collision(client: Any, yaml_path: str) -> None:
    """Raise a ToolError if a storage-mode dashboard already owns the requested
    url_path; otherwise return without doing anything.

    Only runs for yaml_path values starting with 'lovelace.dashboards.'.
    A WebSocket failure or unexpected response shape warns and skips the check
    (fail-open) so that a transient HA outage doesn't block dashboard creation.
    """
    if not yaml_path.startswith(_LOVELACE_DASHBOARD_PREFIX):
        return
    url_path = yaml_path[len(_LOVELACE_DASHBOARD_PREFIX) :]
    try:
        dashboards = await fetch_dashboards_list(client)
    except Exception as exc:
        logger.warning(
            "lovelace/dashboards/list WS query failed (%s); skipping collision check",
            exc,
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


class YamlConfigTools:
    def __init__(self, client: Any) -> None:
        self._client = client

    @tool(
        name="ha_config_set_yaml",
        tags={"System", "beta"},
        annotations={
            "destructiveHint": True,
            "idempotentHint": False,
            "title": "Raw YAML Config Edit",
        },
    )
    @log_tool_usage
    async def ha_config_set_yaml(
        self,
        yaml_path: Annotated[
            str,
            Field(
                description=(
                    "Top-level YAML key to modify. Only a narrow allowlist of "
                    "YAML-only or YAML-heavy integration keys is accepted (e.g., "
                    "'command_line', 'rest', 'shell_command', 'notify', 'knx'). "
                    "For YAML-mode dashboards, "
                    "use the dotted form 'lovelace.dashboards.<url_path>' where "
                    "<url_path> is lowercase, hyphenated, and not a reserved HA "
                    "route. No other multi-segment paths are supported. "
                    "'automation', 'script', and 'scene' are accepted only when "
                    "file is under packages/*.yaml; in configuration.yaml use "
                    "the dedicated storage-mode tools "
                    "(ha_config_set_automation, ha_config_set_script, "
                    "ha_config_set_scene). Not for template sensors or "
                    "input_* helpers — those have dedicated tools."
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
            bool,
            Field(
                default=True,
                description=(
                    "Create a backup before editing. Defaults to True. "
                    "Backups are saved to .ha_mcp_tools_backups/."
                ),
            ),
        ] = True,
        MandatoryBPS: Annotated[
            bool,
            Field(default=True),
        ] = True,
    ) -> dict[str, Any]:
        """Update raw YAML configuration in configuration.yaml or packages/*.yaml (LAST RESORT). MUST call ha_get_skill_guide first.

        **WARNING:** Destructive, disabled by default. Dedicated tools exist for
        almost every use case and should be preferred:

        - Template sensors (state-based or trigger-based) ->
          ha_config_set_helper(helper_type='template')
        - Automations (storage-mode) -> ha_config_set_automation
        - Scripts (storage-mode) -> ha_config_set_script
        - Scenes (storage-mode) -> ha_config_set_scene
        - All 28 helper types (input_*, counter, timer, schedule, zone, person,
          tag, group, min_max, threshold, derivative, statistics, utility_meter,
          trend, filter, switch_as_x, etc.) -> ha_config_set_helper

        Intended for YAML-only integrations with no config-flow or API
        equivalent (command_line, rest, shell_command, notify platforms),
        for integrations with significant YAML-only configuration (knx
        entities in package files), and for registering YAML-mode dashboards via
        ``lovelace.dashboards.<url_path>`` (no other ``lovelace.*`` keys).
        Also accepts ``automation``, ``script``, and ``scene`` keys when
        ``file`` is a ``packages/*.yaml`` — for git-managed YAML configs
        that track these alongside templates and other YAML items. Writes
        to ``configuration.yaml`` for those three keys remain rejected so
        storage-mode and YAML-mode collections don't collide; use the
        dedicated storage-mode tools instead.
        Check ``post_action`` in the response: most keys need a full HA
        restart; template, mqtt, group, automation, script, and scene
        support reload. Preserves YAML comments and HA tags (``!include``,
        ``!secret``) on round-trip; ``replace`` swaps the subtree as-is.

        ``template-guidelines.md`` ships in this response under ``skill_content``
        by default — YAML packages frequently include
        template sensors / command_line entities / mqtt templates, exactly where
        template misuse causes the subtlest bugs. For deeper routing guidance
        beyond what ships here, use ha_get_skill_guide.
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

            # Per-key gate: reject before the custom-component round
            # trip when the yaml_path top-level segment matches a
            # disabled PACKAGES_ONLY key AND the target file is under
            # packages/. The keys (automation / script / scene) are
            # only ACCEPTED in packages/*.yaml in the first place, so
            # writes to configuration.yaml must fall through here and
            # let the component-side reject with its own message that
            # lists the storage-mode tools to use instead.
            settings = get_global_settings()
            disabled_keys = _disabled_packages_keys(settings)
            top_key = yaml_path.split(".", 1)[0] if yaml_path else ""
            # Classify the target exactly like the custom component does
            # (os.path.normpath + fnmatch against "packages/*.yaml") so the
            # wrapper's early reject fires for precisely the paths the
            # component treats as a package — e.g. "./packages/x.yaml" and
            # "packages/sub/x.yaml" both normalise/match. Any other target
            # (configuration.yaml, a non-package path) falls through to the
            # component, which rejects these keys with its own storage-mode-
            # tools advisory.
            # os.path.normpath is a pure string transform (no I/O), so the
            # ASYNC240 blocking-call lint doesn't apply — same suppression the
            # component uses on its identical normpath classification.
            normalized_target = os.path.normpath(file)  # noqa: ASYNC240
            is_packages_target = fnmatch.fnmatch(normalized_target, "packages/*.yaml")
            if is_packages_target and top_key in disabled_keys:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        (
                            f"yaml_path key {top_key!r} is disabled. Enable "
                            f"'Allow {top_key} in packages/*.yaml' under "
                            f"YAML config editing in Server Settings to use "
                            f"this key, or use the storage-mode tool "
                            f"(ha_config_set_{top_key})."
                        ),
                        suggestions=[
                            f"Enable 'Allow {top_key} in packages/*.yaml' under "
                            "YAML config editing in the ha-mcp Server Settings "
                            "panel, then retry.",
                            f"Or use the storage-mode tool ha_config_set_{top_key} "
                            "instead of editing packages YAML directly.",
                        ],
                        context={
                            "yaml_path": yaml_path,
                            "disabled_key": top_key,
                            "file": file,
                        },
                    )
                )

            # Storage-mode dashboard collision check (only for lovelace.dashboards.*).
            # Skip on `remove` so users can clean up YAML entries that conflict
            # with a storage-mode dashboard (e.g., during a migration).
            if action in ("add", "replace"):
                await _check_storage_mode_dashboard_collision(self._client, yaml_path)

            # Check if custom component is available
            await _assert_mcp_tools_available(self._client)

            # Build service data
            service_data: dict[str, Any] = {
                "file": file,
                "action": action,
                "yaml_path": yaml_path,
                "backup": backup,
                "disabled_packages_keys": disabled_keys,
            }
            if content is not None:
                service_data["content"] = content

            # Call the custom component service (token injected by helper)
            result = await call_mcp_tools_service(
                self._client,
                "edit_yaml_config",
                service_data,
            )

            if isinstance(result, dict):
                result = unwrap_service_response(result)
                if not result.get("success", True):
                    raise_tool_error(result)
                attach_skill_content(
                    result,
                    MandatoryBPS=MandatoryBPS,
                    canonical_files=_YAML_SKILL_FILES,
                    referenced_files=None,
                )
                return result

            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Unexpected response format from YAML config service",
                    context={"file": file},
                )
            )

        except ToolError as te:
            raise augment_tool_error_with_skill_content(te, bp_warnings=None) from None
        except Exception as e:
            error = exception_to_structured_error(
                e,
                context={
                    "tool": "ha_config_set_yaml",
                    "file": file,
                    "action": action,
                    "yaml_path": yaml_path,
                },
                raise_error=False,
            )
            augment_error_dict_with_skill_content(error, bp_warnings=None)
            raise_tool_error(error)


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
    register_tool_methods(mcp, YamlConfigTools(client))
