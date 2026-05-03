"""
Filesystem access tools for Home Assistant MCP Server.

This module provides tools for reading and managing files within the Home Assistant
configuration directory, enabling AI assistants to:
- Read configuration files, logs, and other allowed files
- List files in allowed directories
- Write/delete files in restricted directories (www/, themes/, custom_templates/)

**Dependency:** Requires the ha_mcp_tools custom component to be installed.
The tools will gracefully fail with installation instructions if the component is not available.

Feature Flag: Set HAMCP_ENABLE_FILESYSTEM_TOOLS=true to enable these tools.
"""

import json
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
from .util_helpers import coerce_bool_param, coerce_int_param, unwrap_service_response

logger = logging.getLogger(__name__)

# Feature flag - disabled by default for safety
FEATURE_FLAG = "HAMCP_ENABLE_FILESYSTEM_TOOLS"

# Domain for the custom component
MCP_TOOLS_DOMAIN = "ha_mcp_tools"

# Security constants - mirrors the custom component config
READABLE_PATTERNS = [
    "configuration.yaml",
    "automations.yaml",
    "scripts.yaml",
    "scenes.yaml",
    "secrets.yaml",  # Content will be masked by the custom component
    "packages/*.yaml",
    "home-assistant.log",
    "www/**",
    "themes/**",
    "custom_templates/**",
    "dashboards/**",
    "custom_components/**/*.py",
]

WRITABLE_DIRS = ["www", "themes", "custom_templates", "dashboards"]


def is_filesystem_tools_enabled() -> bool:
    """Check if the filesystem tools feature is enabled."""
    value = os.getenv(FEATURE_FLAG, "").lower()
    return value in ("true", "1", "yes", "on")


async def _is_mcp_tools_available(client: Any) -> bool:
    """Return True if the ha_mcp_tools custom component is registered in HA services.

    Raises if the services API call fails — callers handle API errors via
    their own exception_to_structured_error blocks.
    """
    # HA /api/services returns a list of {"domain": str, "services": {...}} objects.
    # This format has been stable since before HA 0.7 (the first public release).
    services = await client.get_services()
    return any(
        isinstance(s, dict) and s.get("domain") == MCP_TOOLS_DOMAIN
        for s in services
    )


async def _assert_mcp_tools_available(client: Any) -> None:
    """Raise ToolError if ha_mcp_tools is not available.

    Must be called within a try block that handles API errors via
    exception_to_structured_error, so connection failures are classified
    correctly rather than masked as COMPONENT_NOT_INSTALLED.
    """
    if not await _is_mcp_tools_available(client):
        raise_tool_error(create_error_response(
            ErrorCode.COMPONENT_NOT_INSTALLED,
            f"The {MCP_TOOLS_DOMAIN} custom component is not installed. "
            "Use ha_install_mcp_tools() to install it via HACS, then restart Home Assistant.",
        ))


class FilesystemTools:
    """Filesystem access tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @tool(
        name="ha_list_files",
        tags={"Files", "beta"},
        annotations={
            "readOnlyHint": True,
            "title": "List Files",
        },
    )
    @log_tool_usage
    async def ha_list_files(
        self,
        path: Annotated[
            str,
            Field(
                description=(
                    "Relative directory path from config directory. "
                    "Allowed paths: www/, themes/, custom_templates/, dashboards/. "
                    "Example: 'www/' or 'themes/my_theme'"
                ),
            ),
        ],
        pattern: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Optional glob pattern to filter files. "
                    "Example: '*.css', '*.yaml', '*.js'"
                ),
            ),
        ] = None,
    ) -> dict[str, Any]:
        """List files in a directory within the Home Assistant config directory.

        Lists files in allowed directories (www/, themes/, custom_templates/, dashboards/) with
        optional glob pattern filtering. Returns file names, sizes, and modification times.

        **Allowed Directories:**
        - `www/` - Web assets (CSS, JS, images for dashboards)
        - `themes/` - Theme files
        - `custom_templates/` - Jinja2 template files
        - `dashboards/` - YAML-mode dashboard files

        **Security:** Only directories in the allowed list can be accessed.
        Path traversal attempts (../) are blocked.

        **Returns:**
        - success: Whether the operation succeeded
        - path: The directory path that was listed
        - files: List of file info objects with name, size, is_dir, modified
        - count: Number of files found

        **Example:**
        ```python
        # List all CSS files in www/
        result = ha_list_files(path="www/", pattern="*.css")
        ```
        """
        try:
            # Check if custom component is available
            await _assert_mcp_tools_available(self._client)

            # Build service data
            service_data: dict[str, Any] = {"path": path}
            if pattern:
                service_data["pattern"] = pattern

            # Call the custom component service
            result = await self._client.call_service(
                MCP_TOOLS_DOMAIN,
                "list_files",
                service_data,
                return_response=True,
            )

            # The service returns the response directly
            if isinstance(result, dict):
                return unwrap_service_response(result)

            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Unexpected response format from list_files service",
                    context={"path": path},
                )
            )

        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={"tool": "ha_list_files", "path": path, "pattern": pattern},
            )

    @tool(
        name="ha_read_file",
        tags={"Files", "beta"},
        annotations={
            "readOnlyHint": True,
            "title": "Read File",
        },
    )
    @log_tool_usage
    async def ha_read_file(
        self,
        path: Annotated[
            str,
            Field(
                description=(
                    "Relative path from config directory. "
                    "Examples: 'configuration.yaml', 'www/custom.css', 'home-assistant.log'"
                ),
            ),
        ],
        tail_lines: Annotated[
            int | str | None,
            Field(
                default=None,
                description=(
                    "For log files, return only the last N lines. "
                    "Recommended for home-assistant.log to avoid large responses. "
                    "Default: None (return full file, or last 1000 lines for logs)"
                ),
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Read a file from the Home Assistant config directory.

        Reads files from allowed paths within the config directory. Some files
        have special handling:
        - `secrets.yaml`: Values are masked for security
        - `home-assistant.log`: Limited to tail (last N lines) by default

        **Allowed Read Paths:**
        - `configuration.yaml`, `automations.yaml`, `scripts.yaml`, `scenes.yaml`
        - `secrets.yaml` (values masked)
        - `packages/*.yaml`
        - `home-assistant.log` (tail only)
        - `www/**`, `themes/**`, `custom_templates/**`, `dashboards/**`
        - `custom_components/**/*.py` (read-only)

        **Security:**
        - Path traversal (../) is blocked
        - Only allowed paths can be read
        - Sensitive data in secrets.yaml is masked

        **Returns:**
        - success: Whether the operation succeeded
        - content: The file content (may be truncated for logs)
        - size: File size in bytes
        - modified: Last modification timestamp
        - path: The file path that was read

        **Example:**
        ```python
        # Read configuration
        result = ha_read_file(path="configuration.yaml")

        # Read last 100 lines of log
        result = ha_read_file(path="home-assistant.log", tail_lines=100)
        ```
        """
        try:
            # Coerce tail_lines parameter
            tail_lines_int = coerce_int_param(
                tail_lines,
                "tail_lines",
                default=None,
                min_value=1,
                max_value=10000,
            )

            # Check if custom component is available
            await _assert_mcp_tools_available(self._client)

            # Build service data
            service_data: dict[str, Any] = {"path": path}
            if tail_lines_int is not None:
                service_data["tail_lines"] = tail_lines_int

            # Call the custom component service
            result = await self._client.call_service(
                MCP_TOOLS_DOMAIN,
                "read_file",
                service_data,
                return_response=True,
            )

            if isinstance(result, dict):
                return unwrap_service_response(result)

            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Unexpected response format from read_file service",
                    context={"path": path},
                )
            )

        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={"tool": "ha_read_file", "path": path},
            )

    @tool(
        name="ha_write_file",
        tags={"Files", "beta"},
        annotations={
            "destructiveHint": True,
            "title": "Write File",
        },
    )
    @log_tool_usage
    async def ha_write_file(
        self,
        path: Annotated[
            str,
            Field(
                description=(
                    "Relative path from config directory. "
                    "Must be in www/, themes/, custom_templates/, or dashboards/. "
                    "Example: 'www/custom.css', 'themes/my_theme.yaml'"
                ),
            ),
        ],
        content: Annotated[
            str,
            Field(
                description="The content to write to the file.",
            ),
        ],
        overwrite: Annotated[
            bool | str,
            Field(
                default=False,
                description=(
                    "Whether to overwrite if file exists. "
                    "Default is False to prevent accidental overwrites."
                ),
            ),
        ] = False,
        create_dirs: Annotated[
            bool | str,
            Field(
                default=True,
                description=(
                    "Whether to create parent directories if they don't exist. "
                    "Default is True."
                ),
            ),
        ] = True,
    ) -> dict[str, Any]:
        """Write a file to allowed directories in the Home Assistant config.

        Creates or updates files in restricted directories only. This is useful for:
        - Creating custom CSS/JS for dashboards
        - Adding theme files
        - Creating Jinja2 templates

        **Allowed Write Directories:**
        - `www/` - Web assets for dashboards
        - `themes/` - Theme YAML files
        - `custom_templates/` - Jinja2 template files
        - `dashboards/` - YAML-mode dashboard files

        **Security:**
        - Only the directories above allow writes
        - Configuration files (configuration.yaml, etc.) cannot be written
        - Path traversal (../) is blocked

        **Returns:**
        - success: Whether the operation succeeded
        - path: The file path that was written
        - size: Size of the written file in bytes
        - created: Whether this was a new file (vs overwrite)

        **Example:**
        ```python
        # Create a custom CSS file
        result = ha_write_file(
            path="www/custom-dashboard.css",
            content=".card { background: #333; }",
            overwrite=True
        )

        # Create a theme file
        result = ha_write_file(
            path="themes/dark_blue.yaml",
            content="Dark Blue:\\n  primary-color: '#1a237e'",
            overwrite=False
        )
        ```
        """
        try:
            # Coerce boolean parameters
            overwrite_bool = coerce_bool_param(overwrite, "overwrite", default=False)
            create_dirs_bool = coerce_bool_param(create_dirs, "create_dirs", default=True)

            # Check if custom component is available
            await _assert_mcp_tools_available(self._client)

            # Build service data
            service_data: dict[str, Any] = {
                "path": path,
                "content": content,
                "overwrite": overwrite_bool,
                "create_dirs": create_dirs_bool,
            }

            # Call the custom component service
            result = await self._client.call_service(
                MCP_TOOLS_DOMAIN,
                "write_file",
                service_data,
                return_response=True,
            )

            if isinstance(result, dict):
                return unwrap_service_response(result)

            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Unexpected response format from write_file service",
                    context={"path": path},
                )
            )

        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={"tool": "ha_write_file", "path": path},
            )

    @tool(
        name="ha_delete_file",
        tags={"Files", "beta"},
        annotations={
            "destructiveHint": True,
            "title": "Delete File",
        },
    )
    @log_tool_usage
    async def ha_delete_file(
        self,
        path: Annotated[
            str,
            Field(
                description=(
                    "Relative path from config directory. "
                    "Must be in www/, themes/, custom_templates/, or dashboards/. "
                    "Example: 'www/old-file.css'"
                ),
            ),
        ],
        confirm: Annotated[
            bool | str,
            Field(
                default=False,
                description=(
                    "Must be True to confirm deletion. "
                    "This is a safety measure to prevent accidental deletions."
                ),
            ),
        ] = False,
    ) -> dict[str, Any]:
        """Delete a file from allowed directories in the Home Assistant config.

        Permanently removes a file from the allowed directories. This action
        cannot be undone.

        **Allowed Delete Directories:**
        - `www/` - Web assets
        - `themes/` - Theme files
        - `custom_templates/` - Template files
        - `dashboards/` - YAML-mode dashboard files

        **Security:**
        - Only the directories above allow deletions
        - Configuration files cannot be deleted
        - Path traversal (../) is blocked
        - Requires confirm=True to prevent accidents

        **Returns:**
        - success: Whether the operation succeeded
        - path: The file path that was deleted
        - message: Confirmation message

        **Example:**
        ```python
        # Delete an old CSS file
        result = ha_delete_file(
            path="www/deprecated-style.css",
            confirm=True
        )
        ```
        """
        try:
            # Coerce boolean parameter
            confirm_bool = coerce_bool_param(confirm, "confirm", default=False)

            if not confirm_bool:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "Deletion not confirmed. Set confirm=True to delete a file.",
                        suggestions=[
                            f"Call ha_delete_file(path={json.dumps(path)}, confirm=True) to proceed",
                        ],
                        context={"path": path},
                    )
                )

            # Check if custom component is available
            await _assert_mcp_tools_available(self._client)

            # Build service data
            service_data: dict[str, Any] = {"path": path}

            # Call the custom component service
            result = await self._client.call_service(
                MCP_TOOLS_DOMAIN,
                "delete_file",
                service_data,
                return_response=True,
            )

            if isinstance(result, dict):
                return unwrap_service_response(result)

            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Unexpected response format from delete_file service",
                    context={"path": path},
                )
            )

        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={"tool": "ha_delete_file", "path": path},
            )


def register_filesystem_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register filesystem access tools with the MCP server.

    This function only registers tools if the feature flag is enabled.
    Set HAMCP_ENABLE_FILESYSTEM_TOOLS=true to enable.
    """
    if not is_filesystem_tools_enabled():
        logger.debug(
            f"Filesystem tools disabled (set {FEATURE_FLAG}=true to enable)"
        )
        return

    logger.info("Filesystem tools enabled via feature flag")
    register_tool_methods(mcp, FilesystemTools(client))
