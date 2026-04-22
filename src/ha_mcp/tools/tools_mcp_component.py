"""
MCP Tools Component installer for Home Assistant.

This module provides the ha_install_mcp_tools tool which installs the
ha_mcp_tools custom component via HACS. This enables additional services
that are not available through standard Home Assistant APIs.

Feature Flag: Set HAMCP_ENABLE_CUSTOM_COMPONENT_INTEGRATION=true to enable this tool.
"""

import logging
import os
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
from .util_helpers import add_timezone_metadata

logger = logging.getLogger(__name__)

# Feature flag - disabled by default for silent launch
FEATURE_FLAG = "HAMCP_ENABLE_CUSTOM_COMPONENT_INTEGRATION"


def is_custom_component_integration_enabled() -> bool:
    """Check if the custom component integration feature is enabled."""
    value = os.getenv(FEATURE_FLAG, "").lower()
    return value in ("true", "1", "yes", "on")


# Constants for ha_mcp_tools custom component
MCP_TOOLS_REPO = "homeassistant-ai/ha-mcp"
MCP_TOOLS_DOMAIN = "ha_mcp_tools"


class McpComponentTools:
    """MCP component installation tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @staticmethod
    def _find_existing_repo(repos: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Find the ha_mcp_tools repository in the HACS repository list."""
        for repo in repos:
            if repo.get("full_name", "").lower() == MCP_TOOLS_REPO.lower():
                return repo
        return None

    @staticmethod
    async def _poll_for_repo_id(ws_client: Any) -> str | None:
        """Poll HACS repository list until the MCP tools repo ID is available."""
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
                    return repo_id

            if attempt < max_attempts - 1:
                await asyncio.sleep(poll_interval)

        return None

    @staticmethod
    async def _resolve_repo_id(ws_client: Any, existing_repo: dict[str, Any] | None) -> str:
        """Resolve the HACS repository ID, polling if necessary."""
        repo_id = str(existing_repo.get("id")) if existing_repo else None

        if not repo_id:
            repo_id = await McpComponentTools._poll_for_repo_id(ws_client)

        if not repo_id:
            raise_tool_error(create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                "Could not find repository ID after adding (timed out after 10 attempts)",
                suggestions=[
                    "HACS may be processing the request - try again in a few seconds",
                    "Check HACS logs for errors",
                    f"Verify the repository exists: https://github.com/{MCP_TOOLS_REPO}",
                ],
            ))

        return repo_id

    @staticmethod
    async def _ensure_hacs_ready(ws_client: Any) -> None:
        """Verify HACS is functional and not disabled."""
        info_response = await ws_client.send_command("hacs/info")
        if info_response.get("success"):
            hacs_info = info_response.get("result", {})
            disabled_reason = hacs_info.get("disabled_reason")
            if disabled_reason:
                raise_tool_error(create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"HACS is disabled: {disabled_reason}",
                    context={"disabled_reason": disabled_reason},
                    suggestions=[
                        "HACS requires a valid GitHub token to manage repositories",
                        "Configure a GitHub Personal Access Token in HACS settings",
                        "Ensure HACS has completed initial setup",
                    ],
                ))

    @staticmethod
    async def _add_repo_to_hacs(ws_client: Any) -> dict[str, Any]:
        """Add the MCP tools repository to HACS. Returns the repo info dict."""
        from .tools_hacs import CATEGORY_MAP

        logger.info(f"Adding {MCP_TOOLS_REPO} to HACS")
        hacs_category = CATEGORY_MAP.get("integration", "integration")
        add_response = await ws_client.send_command(
            "hacs/repositories/add",
            repository=MCP_TOOLS_REPO,
            category=hacs_category,
        )

        if not add_response.get("success"):
            raise_tool_error(create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Failed to add repository to HACS: {add_response}",
                suggestions=[
                    f"Verify the repository exists: https://github.com/{MCP_TOOLS_REPO}",
                    "Check HACS logs for errors",
                ],
            ))

        result: dict[str, Any] = add_response.get("result", {})
        return result

    @staticmethod
    def _handle_restart(result: dict[str, Any], restart_error: Exception) -> None:
        """Handle restart errors, distinguishing expected connection drops from real failures."""
        if any(
            pattern in str(restart_error).lower()
            for pattern in ("connect", "closed", "504")
        ):
            result["restarted"] = True
            result["message"] += ". Home Assistant is restarting."
            result["note"] = (
                "Wait 1-5 minutes for Home Assistant to restart."
            )
        else:
            result["restart_error"] = str(restart_error)
            result["message"] += ". Restart failed - please restart manually."

    @tool(
        name="ha_install_mcp_tools",
        tags={"Utilities", "beta"},
        annotations={
            "destructiveHint": True,
            "title": "Install MCP Tools Component"
        }
    )
    @log_tool_usage
    async def ha_install_mcp_tools(
        self,
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
            # Late import: tools_hacs may not be loaded when this module is imported
            from .tools_hacs import _assert_hacs_available

            await _assert_hacs_available()

            from ..client.websocket_client import get_websocket_client

            ws_client = await get_websocket_client()
            await self._ensure_hacs_ready(ws_client)
            list_response = await ws_client.send_command("hacs/repositories/list")
            if not list_response.get("success"):
                raise_tool_error(create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Failed to get HACS repository list",
                ))

            repos = list_response.get("result", [])
            existing_repo = self._find_existing_repo(repos)

            # If already installed, return success
            if existing_repo and existing_repo.get("installed"):
                return await add_timezone_metadata(
                    self._client,
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
                existing_repo = await self._add_repo_to_hacs(ws_client)

            # Now download/install the repository
            repo_id = await self._resolve_repo_id(ws_client, existing_repo)

            logger.info(f"Installing {MCP_TOOLS_REPO} (ID: {repo_id})")
            download_response = await ws_client.send_command(
                "hacs/repository/download",
                repository=repo_id,
            )

            if not download_response.get("success"):
                raise_tool_error(create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Failed to download repository: {download_response}",
                    suggestions=[
                        "Check HACS logs for errors",
                        "Verify GitHub is accessible",
                    ],
                ))

            result: dict[str, Any] = {
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
                    await self._client.call_service("homeassistant", "restart", {})
                    result["restarted"] = True
                    result["message"] += ". Home Assistant is restarting."
                    result["note"] = "Wait 1-5 minutes for Home Assistant to restart."
                except Exception as restart_error:
                    # Connection/proxy errors during restart are expected
                    # (HA closes connections, proxies may return 504)
                    self._handle_restart(result, restart_error)
            else:
                result["note"] = "Restart Home Assistant for the integration to load."

            return await add_timezone_metadata(self._client, result)

        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={"tool": "ha_install_mcp_tools", "restart": restart},
                suggestions=[
                    "Verify HACS is installed: https://hacs.xyz/",
                    "Check Home Assistant logs for errors",
                    "Ensure GitHub is accessible",
                ],
            )


def register_mcp_component_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
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
    register_tool_methods(mcp, McpComponentTools(client))
