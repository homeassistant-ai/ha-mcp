"""
MCP Tools Component installer for Home Assistant.

This module provides the ha_install_mcp_tools tool which installs the
ha_mcp_tools custom component via HACS. This enables additional services
that are not available through standard Home Assistant APIs.

Feature Flag: Set HAMCP_ENABLE_MCP_TOOLS_INSTALLER=true to enable this tool.
"""

import logging
import os
from typing import Annotated, Any

from pydantic import Field

from .helpers import exception_to_structured_error, log_tool_usage, raise_tool_error
from .util_helpers import add_timezone_metadata

logger = logging.getLogger(__name__)

# Feature flag - disabled by default for silent launch
FEATURE_FLAG = "HAMCP_ENABLE_CUSTOM_COMPONENT_INTEGRATION"


def is_custom_component_integration_enabled() -> bool:
    """Check if the custom component integration feature is enabled."""
    value = os.getenv(FEATURE_FLAG, "").lower()
    return value in ("true", "1", "yes", "on")


# Constants for ha_mcp_tools custom component
# TODO: Switch to "homeassistant-ai/ha-mcp" after hacs.json is on default branch
MCP_TOOLS_REPO = "julienld/ha-mcp-test-custom-component"
MCP_TOOLS_DOMAIN = "ha_mcp_tools"


def register_mcp_component_tools(mcp, client, **kwargs):
    """Register MCP component installation tools.

    This function only registers tools if the feature flag is enabled.
    Set HAMCP_ENABLE_CUSTOM_COMPONENT_INTEGRATION=true to enable.
    """
    if not is_custom_component_integration_enabled():
        logger.debug(
            f"MCP tools installer disabled (set {FEATURE_FLAG}=true to enable)"
        )
        return

    logger.info("MCP tools installer enabled via feature flag")

    # Import HACS helpers - we depend on HACS functionality
    from .tools_hacs import _check_hacs_available, CATEGORY_MAP

    @mcp.tool(
        annotations={
            "destructiveHint": True,
            "tags": ["mcp", "management", "installation"],
            "title": "Install MCP Tools Component",
        }
    )
    @log_tool_usage
    async def ha_install_mcp_tools(
        restart: Annotated[
            bool,
            Field(
                default=False,
                description="Whether to restart Home Assistant after installation (required for integration to load)",
            ),
        ] = False,
    ) -> dict[str, Any]:
        """Install the ha_mcp_tools custom component via HACS.

        This tool installs the ha_mcp_tools custom component which provides
        advanced services not available through standard Home Assistant APIs:

        **Available Services (after installation):**
        - `ha_mcp_tools.list_files`: List files in allowed directories (www/, themes/)
        - More services coming soon: file write, backup cleanup, event buffer, etc.

        **Installation Process:**
        1. Checks if HACS is available
        2. Checks if ha_mcp_tools is already installed
        3. Adds the repository to HACS if not present
        4. Downloads and installs the component
        5. Optionally restarts Home Assistant

        **Note:** A restart is required for the integration to load and become available.
        Set `restart=True` to automatically restart, or manually restart later.

        Args:
            restart: Whether to restart Home Assistant after installation (default: False)

        Returns:
            Installation status and next steps.
        """
        try:
            # Check if HACS is available
            is_available, error_msg = await _check_hacs_available(client)
            if not is_available:
                return await add_timezone_metadata(
                    client,
                    {
                        "success": False,
                        "error": error_msg,
                        "error_code": "HACS_NOT_AVAILABLE",
                        "suggestions": [
                            "Install HACS from https://hacs.xyz/",
                            "Ensure Home Assistant has been restarted after HACS installation",
                            "Check Home Assistant logs for HACS errors",
                        ],
                    },
                )

            from ..client.websocket_client import get_websocket_client

            ws_client = await get_websocket_client()

            # Check if HACS is fully functional (not disabled)
            info_response = await ws_client.send_command("hacs/info")
            if info_response.get("success"):
                hacs_info = info_response.get("result", {})
                disabled_reason = hacs_info.get("disabled_reason")
                if disabled_reason:
                    return await add_timezone_metadata(
                        client,
                        {
                            "success": False,
                            "error": f"HACS is disabled: {disabled_reason}",
                            "error_code": "HACS_DISABLED",
                            "disabled_reason": disabled_reason,
                            "suggestions": [
                                "HACS requires a valid GitHub token to manage repositories",
                                "Configure a GitHub Personal Access Token in HACS settings",
                                "Ensure HACS has completed initial setup",
                            ],
                        },
                    )

            # Check if already installed by looking in the repository list
            list_response = await ws_client.send_command("hacs/repositories/list")
            if not list_response.get("success"):
                return await add_timezone_metadata(
                    client,
                    {
                        "success": False,
                        "error": "Failed to get HACS repository list",
                        "error_code": "HACS_LIST_FAILED",
                    },
                )

            repos = list_response.get("result", [])
            existing_repo = None
            for repo in repos:
                if repo.get("full_name", "").lower() == MCP_TOOLS_REPO.lower():
                    existing_repo = repo
                    break

            # If already installed, return success
            if existing_repo and existing_repo.get("installed"):
                return await add_timezone_metadata(
                    client,
                    {
                        "success": True,
                        "already_installed": True,
                        "version": existing_repo.get("installed_version"),
                        "message": f"ha_mcp_tools is already installed (version {existing_repo.get('installed_version')})",
                        "services": [
                            "ha_mcp_tools.list_files - List files in allowed directories",
                        ],
                    },
                )

            # If repo not in HACS, add it first
            if not existing_repo:
                logger.info(f"Adding {MCP_TOOLS_REPO} to HACS")
                hacs_category = CATEGORY_MAP.get("integration", "integration")
                add_response = await ws_client.send_command(
                    "hacs/repositories/add",
                    repository=MCP_TOOLS_REPO,
                    category=hacs_category,
                )

                if not add_response.get("success"):
                    return await add_timezone_metadata(
                        client,
                        {
                            "success": False,
                            "error": f"Failed to add repository to HACS: {add_response}",
                            "error_code": "HACS_ADD_FAILED",
                            "suggestions": [
                                f"Verify the repository exists: https://github.com/{MCP_TOOLS_REPO}",
                                "Check HACS logs for errors",
                            ],
                        },
                    )

                # Get the new repo info
                existing_repo = add_response.get("result", {})

            # Now download/install the repository
            repo_id = str(existing_repo.get("id")) if existing_repo else None

            if not repo_id:
                # HACS processes additions asynchronously, so poll for the repo to appear
                import asyncio
                max_attempts = 10
                poll_interval = 1.0  # seconds

                for attempt in range(max_attempts):
                    logger.debug(f"Polling for repository ID (attempt {attempt + 1}/{max_attempts})")
                    list_response = await ws_client.send_command("hacs/repositories/list")
                    repos = list_response.get("result", [])
                    for repo in repos:
                        if repo.get("full_name", "").lower() == MCP_TOOLS_REPO.lower():
                            repo_id = str(repo.get("id"))
                            logger.info(f"Found repository ID: {repo_id} after {attempt + 1} attempts")
                            break

                    if repo_id:
                        break

                    if attempt < max_attempts - 1:
                        await asyncio.sleep(poll_interval)

            if not repo_id:
                return await add_timezone_metadata(
                    client,
                    {
                        "success": False,
                        "error": "Could not find repository ID after adding (timed out after 10 attempts)",
                        "error_code": "HACS_REPO_ID_NOT_FOUND",
                        "suggestions": [
                            "HACS may be processing the request - try again in a few seconds",
                            "Check HACS logs for errors",
                            f"Verify the repository exists: https://github.com/{MCP_TOOLS_REPO}",
                        ],
                    },
                )

            logger.info(f"Installing {MCP_TOOLS_REPO} (ID: {repo_id})")
            download_response = await ws_client.send_command(
                "hacs/repository/download",
                repository=repo_id,
            )

            if not download_response.get("success"):
                return await add_timezone_metadata(
                    client,
                    {
                        "success": False,
                        "error": f"Failed to download repository: {download_response}",
                        "error_code": "HACS_DOWNLOAD_FAILED",
                        "suggestions": [
                            "Check HACS logs for errors",
                            "Verify GitHub is accessible",
                        ],
                    },
                )

            result = {
                "success": True,
                "installed": True,
                "repository": MCP_TOOLS_REPO,
                "message": "ha_mcp_tools installed successfully",
                "services": [
                    "ha_mcp_tools.list_files - List files in allowed directories",
                ],
            }

            # Optionally restart Home Assistant
            if restart:
                try:
                    await client.call_service("homeassistant", "restart", {})
                    result["restarted"] = True
                    result["message"] += ". Home Assistant is restarting."
                    result["note"] = "Wait 1-5 minutes for Home Assistant to restart."
                except Exception as restart_error:
                    # Connection errors during restart are expected
                    if "connection" in str(restart_error).lower():
                        result["restarted"] = True
                        result["message"] += ". Home Assistant is restarting."
                        result["note"] = (
                            "Wait 1-5 minutes for Home Assistant to restart."
                        )
                    else:
                        result["restart_error"] = str(restart_error)
                        result["message"] += ". Restart failed - please restart manually."
            else:
                result["note"] = "Restart Home Assistant for the integration to load."

            return await add_timezone_metadata(client, result)

        except Exception as e:
            error_response = exception_to_structured_error(
                e,
                context={"tool": "ha_install_mcp_tools", "restart": restart},
                raise_error=False,
            )
            if "error" in error_response and isinstance(error_response["error"], dict):
                error_response["error"]["suggestions"] = [
                    "Verify HACS is installed: https://hacs.xyz/",
                    "Check Home Assistant logs for errors",
                    "Ensure GitHub is accessible",
                ]
            raise_tool_error(error_response)
