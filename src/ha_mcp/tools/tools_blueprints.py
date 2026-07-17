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

from ..client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
)
from ..client.websocket_client import get_websocket_client
from ..errors import ErrorCode, create_error_response
from .component_api import (
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)
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
    def _format_blueprint_list(
        blueprints_data: dict[str, Any], domain: str
    ) -> dict[str, Any]:
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
                "name": metadata.get(
                    "name", bp_path.split("/")[-1].replace(".yaml", "")
                ),
            }

            # Add optional metadata if available
            if "metadata" in metadata:
                meta = metadata["metadata"]
                blueprint_info.update(
                    {
                        "description": meta.get("description"),
                        "source_url": meta.get("source_url"),
                        "author": meta.get("author"),
                    }
                )

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
        annotations={
            "openWorldHint": True,
            "idempotentHint": True,
            "readOnlyHint": True,
            "title": "Get Blueprint",
        },
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
        With a path: Returns the blueprint's metadata and input definitions. The
        full body (triggers/conditions/actions for automations, sequence for
        scripts) is included under `config` ONLY when the ha_mcp_tools custom
        component is installed — core's blueprint API exposes metadata alone, so
        without the component the body cannot be read.

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
        - `config`: the full parsed blueprint body (only with the ha_mcp_tools
          component; `!input` substitution points appear as `{"__input__": name}`)
        """
        try:
            # Validate domain
            valid_domains = ["automation", "script"]
            if domain not in valid_domains:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"Invalid domain '{domain}'. Must be one of: {', '.join(valid_domains)}",
                        context={"domain": domain, "valid_domains": valid_domains},
                    )
                )

            # Get list of blueprints
            list_response = await self._client.send_websocket_message(
                {"type": "blueprint/list", "domain": domain}
            )

            if not list_response.get("success"):
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        list_response.get("error", "Failed to query blueprints"),
                        context={"domain": domain},
                    )
                )

            blueprints_data = list_response.get("result", {})

            # If no path provided, return list of all blueprints
            if path is None:
                return self._format_blueprint_list(blueprints_data, domain)

            # Path provided - get specific blueprint details
            if path not in blueprints_data:
                available_paths = list(blueprints_data.keys())[:10]
                raise_tool_error(
                    create_error_response(
                        ErrorCode.RESOURCE_NOT_FOUND,
                        f"Blueprint not found: {path}",
                        context={
                            "path": path,
                            "domain": domain,
                            "available_blueprints": available_paths,
                        },
                        suggestions=[
                            "Use ha_get_blueprint() without path to see all available blueprints",
                            "Check the path format (e.g., 'homeassistant/motion_light.yaml')",
                        ],
                    )
                )

            # Get the blueprint details from the list response
            blueprint_data = blueprints_data[path]

            # Extract and format blueprint information
            result = {
                "success": True,
                "path": path,
                "domain": domain,
                "name": blueprint_data.get(
                    "name", path.split("/")[-1].replace(".yaml", "")
                ),
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

            # Core's blueprint/list returns metadata only (never a body), so the
            # full triggers/conditions/actions/sequence come from the ha_mcp_tools
            # component when installed. Merge it additively under `config`; without
            # the component the response stays metadata + inputs.
            await self._merge_blueprint_config(result, domain, path)

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
            return None  # unreachable: exception_to_structured_error always raises

    async def _merge_blueprint_config(
        self, result: dict[str, Any], domain: str, path: str
    ) -> None:
        """Fetch the component-served blueprint body and merge it into ``result``.

        Adds ``config`` when the body was read, or a top-level ``warnings`` entry
        when a present component returned an unreadable body; a metadata-only
        outcome (no component / capability) leaves ``result`` untouched.
        """
        config, config_warning = await self._blueprint_config_via_component(
            domain, path
        )
        if config is not None:
            result["config"] = config
        elif config_warning is not None:
            result.setdefault("warnings", []).append(config_warning)

    async def _blueprint_config_via_component(
        self, domain: str, path: str
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Fetch a blueprint's full parsed body via the component.

        core's ``blueprint/list`` returns only ``{metadata}`` (no body), so
        without the component ``ha_get_blueprint`` can serve metadata + inputs
        only. When the component advertises ``blueprint_get`` it reads the on-disk
        blueprint file (path-jailed, executor-offloaded) and returns the full
        parsed body, merged additively under ``config``. Returns
        ``(config, warning)``:

        - ``(dict, None)`` — the parsed body was read.
        - ``(None, None)`` — metadata-only is the expected outcome: the component
          is absent / lacks the capability, was downgraded (``unknown_command`` →
          invalidate the cached caps), or errored (logged).
        - ``(None, warning)`` — the component is present and the server has already
          confirmed the path is a real installed blueprint, yet it returned a null
          ``config`` (corrupt / unparseable file, read error). Metadata-only would
          otherwise be indistinguishable from component-not-installed, so a
          top-level warning is surfaced instead.
        """
        caps = await get_component_caps(self._client)
        if not component_supports(caps, "blueprint_get"):
            return None, None
        try:
            ws = await get_websocket_client(
                url=self._client.base_url, token=self._client.token
            )
            raw = await ws.send_command(
                "ha_mcp_tools/blueprint_get", domain=domain, path=path
            )
        except (HomeAssistantCommandError, HomeAssistantCommandTimeout) as exc:
            if is_unknown_command(exc):
                invalidate_caps(self._client)
            else:
                logger.warning(
                    "ha_mcp_tools/blueprint_get failed; served metadata-only: %r",
                    exc,
                )
            return None, None
        result = raw.get("result") or {}
        config = result.get("config")
        if isinstance(config, dict):
            return config, None
        return None, (
            "Blueprint body could not be read or parsed by the ha_mcp_tools "
            "component; returning metadata only"
        )

    async def _save_blueprint(
        self,
        url: str,
        domain: str,
        path: str,
        yaml_data: str,
        overwrite: bool,
    ) -> dict[str, Any]:
        """Persist a validated blueprint via blueprint/save, raising on failure.

        Returns the blueprint/save result payload (contains overrides_existing).
        """
        save_message: dict[str, Any] = {
            "type": "blueprint/save",
            "domain": domain,
            "path": path,
            "yaml": yaml_data,
            "source_url": url,
        }
        # allow_override only exists on HA >= 2023.12 and the WS schema
        # rejects unknown keys - only send it when actually overwriting
        if overwrite:
            save_message["allow_override"] = True

        save_response = await self._client.send_websocket_message(save_message)

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

            # Reachable despite the early exists check: a race between
            # import and save, or an installed file that failed to load
            # (core reports exists=false for those)
            already_exists = "already exists" in save_error.lower()
            if already_exists:
                suggestions.insert(
                    0,
                    "A blueprint with this path already exists - pass overwrite=true to re-import it",
                )

            raise_tool_error(
                create_error_response(
                    ErrorCode.RESOURCE_ALREADY_EXISTS
                    if already_exists
                    else ErrorCode.SERVICE_CALL_FAILED,
                    save_error,
                    context={"url": url, "path": path},
                    suggestions=suggestions,
                )
            )

        return save_response.get("result") or {}

    @tool(
        name="ha_import_blueprint",
        tags={"Blueprints"},
        annotations={
            "openWorldHint": True,
            "destructiveHint": True,
            "title": "Import Blueprint",
        },
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
        overwrite: Annotated[
            bool,
            Field(
                description="Overwrite the blueprint if it is already installed (re-import). "
                "Home Assistant reloads all automations/scripts using the blueprint.",
                default=False,
            ),
        ] = False,
    ) -> dict[str, Any]:
        """
        Import a blueprint from a URL.

        Imports a blueprint from GitHub, Home Assistant Community forums,
        or any direct URL to a blueprint YAML file. Set overwrite=true to
        re-import a blueprint that is already installed (equivalent to the
        UI's "Re-import blueprint" action) - Home Assistant then reloads all
        automations/scripts that use it.

        EXAMPLES:
        - Import from GitHub: ha_import_blueprint("https://github.com/user/repo/blob/main/blueprint.yaml")
        - Import from HA Community: ha_import_blueprint("https://community.home-assistant.io/t/motion-light/123456")
        - Import direct YAML: ha_import_blueprint("https://example.com/my-blueprint.yaml")
        - Re-import an installed blueprint: ha_import_blueprint("https://example.com/my-blueprint.yaml", overwrite=True)

        SUPPORTED SOURCES:
        - GitHub repository URLs (will be converted to raw URLs)
        - Home Assistant Community forum posts with blueprint code
        - Direct URLs to YAML blueprint files

        RETURNS:
        - Import result with the blueprint path where it was saved
        - Blueprint metadata (name, domain, description)
        - overrides_existing: true when an installed blueprint was overwritten
        - Error details if import fails
        """
        try:
            # Validate URL format
            if not url.startswith(("http://", "https://")):
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "Invalid URL format. URL must start with http:// or https://",
                        context={"url": url},
                    )
                )

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
                    suggestions.insert(
                        0,
                        "Blueprint already exists - use ha_get_blueprint() to see installed blueprints",
                    )

                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        error_msg,
                        context={"url": url},
                        suggestions=suggestions,
                    )
                )

            # Extract import result (blueprint/import only validates, does not save)
            result_data = response.get("result", {})
            suggested_filename = result_data.get("suggested_filename", "")
            raw_data = result_data.get("raw_data", "")
            blueprint_meta = result_data.get("blueprint", {}).get("metadata", {})
            domain = blueprint_meta.get("domain", "automation")

            if not suggested_filename or not raw_data:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        "Blueprint validated but no filename or YAML data was returned",
                        context={"url": url},
                        suggestions=[
                            "This may indicate an incompatible blueprint format",
                            "Try a different blueprint URL",
                        ],
                    )
                )

            # Ensure the path has a .yaml extension — HA's blueprint/import returns
            # suggested_filename without the extension (e.g. "user/blueprint_name")
            if not suggested_filename.endswith((".yaml", ".yml")):
                suggested_filename = suggested_filename + ".yaml"

            # blueprint/save does not re-run these checks (currently the
            # blueprint's min Home Assistant version) - without this gate an
            # unsupported blueprint saves cleanly and reports success
            validation_errors = result_data.get("validation_errors")
            if validation_errors:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_FAILED,
                        "Blueprint failed validation: "
                        + "; ".join(str(e) for e in validation_errors),
                        context={"url": url, "validation_errors": validation_errors},
                        suggestions=[
                            "The blueprint is not compatible with this Home Assistant installation",
                            "Update Home Assistant to satisfy the blueprint's minimum version requirement",
                        ],
                    )
                )

            # blueprint/import reports whether the target path is already
            # installed - fail early with a re-import hint instead of letting
            # blueprint/save reject the write
            if result_data.get("exists") and not overwrite:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.RESOURCE_ALREADY_EXISTS,
                        f"Blueprint already exists at '{suggested_filename}'. "
                        "Pass overwrite=true to re-import it.",
                        context={
                            "url": url,
                            "path": suggested_filename,
                            "domain": domain,
                        },
                        suggestions=[
                            "Call ha_import_blueprint with overwrite=true to update the installed blueprint",
                            "Use ha_get_blueprint() to inspect the currently installed version",
                        ],
                    )
                )

            # Save the blueprint to disk (blueprint/import only validates)
            save_result = await self._save_blueprint(
                url, domain, suggested_filename, raw_data, overwrite
            )
            overrides_existing = save_result.get("overrides_existing", False)

            return {
                "success": True,
                "url": url,
                "imported_blueprint": {
                    "path": suggested_filename,
                    "domain": domain,
                    "name": blueprint_meta.get("name"),
                    "description": blueprint_meta.get("description"),
                },
                "overrides_existing": overrides_existing,
                "message": (
                    "Blueprint re-imported successfully. Automations/scripts using it were reloaded."
                    if overrides_existing
                    else "Blueprint imported successfully. Use ha_get_blueprint() to see all installed blueprints."
                ),
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
            return None  # unreachable: exception_to_structured_error always raises


def register_blueprint_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant blueprint management tools."""
    register_tool_methods(mcp, BlueprintTools(client))
