"""
HACS (Home Assistant Community Store) integration tools for Home Assistant MCP server.

This module provides tools to interact with HACS via the WebSocket API, enabling AI agents
to discover custom integrations, Lovelace cards, themes, and more.
"""

import logging
from typing import Annotated, Any, Literal

from pydantic import Field

from .helpers import exception_to_structured_error, log_tool_usage, raise_tool_error
from .util_helpers import add_timezone_metadata, coerce_int_param

logger = logging.getLogger(__name__)

# HACS uses different category names internally vs what users expect
# User-friendly name -> HACS internal name
CATEGORY_MAP = {
    "lovelace": "plugin",  # HACS calls Lovelace cards "plugin"
    "integration": "integration",
    "theme": "theme",
    "appdaemon": "appdaemon",
    "python_script": "python_script",
    "template": "template",
}

# Reverse mapping for display
CATEGORY_DISPLAY = {v: k for k, v in CATEGORY_MAP.items()}
CATEGORY_DISPLAY["plugin"] = "lovelace"  # Display as lovelace for users


async def _check_hacs_available(client: Any) -> tuple[bool, str | None]:
    """
    Check if HACS is installed and available via WebSocket.

    Returns:
        Tuple of (is_available, error_message)
    """
    try:
        from ..client.websocket_client import get_websocket_client
        ws_client = await get_websocket_client()

        # Try to get HACS info to verify it's installed
        response = await ws_client.send_command("hacs/info")

        if response.get("success"):
            return True, None
        else:
            return False, "HACS is installed but returned an error"
    except Exception as e:
        error_str = str(e).lower()
        if "unknown command" in error_str or "not found" in error_str:
            return False, "HACS is not installed or not loaded. Please install HACS from https://hacs.xyz/"
        return False, f"Failed to connect to HACS: {str(e)}"


def register_hacs_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register HACS integration tools with the MCP server."""

    @mcp.tool(annotations={"idempotentHint": True, "readOnlyHint": True, "tags": ["hacs", "info"], "title": "Get HACS Info"})
    @log_tool_usage
    async def ha_hacs_info() -> dict[str, Any]:
        """Get HACS status, version, and enabled categories.

        Returns information about the HACS installation including:
        - Version number
        - Enabled categories (integration, lovelace, theme, etc.)
        - Stage (running, startup, etc.)
        - Lovelace mode
        - Disabled reason (if any)

        This is useful for validating that HACS is installed and operational
        before using other HACS tools.

        **HACS Installation:**
        If HACS is not installed, visit https://hacs.xyz/ for installation instructions.

        Returns:
            Dictionary with HACS status information or error if HACS is not available.
        """
        try:
            # Check if HACS is available
            is_available, error_msg = await _check_hacs_available(client)
            if not is_available:
                return await add_timezone_metadata(client, {
                    "success": False,
                    "error": error_msg,
                    "error_code": "HACS_NOT_AVAILABLE",
                    "suggestions": [
                        "Install HACS from https://hacs.xyz/",
                        "Ensure Home Assistant has been restarted after HACS installation",
                        "Check Home Assistant logs for HACS errors",
                    ],
                })

            # Get HACS info via WebSocket
            from ..client.websocket_client import get_websocket_client
            ws_client = await get_websocket_client()
            response = await ws_client.send_command("hacs/info")

            if not response.get("success"):
                exception_to_structured_error(
                    Exception(f"HACS info request failed: {response}"),
                    context={"command": "hacs/info"},
                    raise_error=True,
                )

            result = response.get("result", {})

            return await add_timezone_metadata(client, {
                "success": True,
                "version": result.get("version"),
                "categories": result.get("categories", []),
                "stage": result.get("stage"),
                "lovelace_mode": result.get("lovelace_mode"),
                "disabled_reason": result.get("disabled_reason"),
                "data": result,
            })

        except Exception as e:
            error_response = exception_to_structured_error(
                e,
                context={"tool": "ha_hacs_info"},
                raise_error=False,
            )
            if "error" in error_response and isinstance(error_response["error"], dict):
                error_response["error"]["suggestions"] = [
                    "Verify HACS is installed: https://hacs.xyz/",
                    "Check Home Assistant connection",
                    "Restart Home Assistant if HACS was recently installed",
                ]
            error_with_tz = await add_timezone_metadata(client, error_response)
            raise_tool_error(error_with_tz)

    @mcp.tool(annotations={"idempotentHint": True, "readOnlyHint": True, "tags": ["hacs", "search"], "title": "List HACS Installed"})
    @log_tool_usage
    async def ha_hacs_list_installed(
        category: Annotated[
            Literal["integration", "lovelace", "theme", "appdaemon", "python_script"] | None,
            Field(
                default=None,
                description=(
                    "Filter by category: 'integration', 'lovelace', 'theme', "
                    "'appdaemon', or 'python_script'. Use None for all categories."
                ),
            ),
        ] = None,
    ) -> dict[str, Any]:
        """List installed HACS repositories with focused, small response.

        **DASHBOARD TIP:** Use `category="lovelace"` to discover installed custom cards
        for use with `ha_config_set_dashboard()`.

        Returns a list of installed repositories with key information:
        - name: Repository name
        - full_name: Full GitHub repository name (owner/repo)
        - category: Type of repository (integration, lovelace, theme, etc.)
        - installed_version: Currently installed version
        - available_version: Latest available version
        - pending_update: Whether an update is available
        - description: Repository description

        **Categories:**
        - `integration`: Custom integrations and components
        - `lovelace`: Custom dashboard cards and panels
        - `theme`: Custom themes for the UI
        - `appdaemon`: AppDaemon apps
        - `python_script`: Python scripts

        Args:
            category: Filter results by category (default: all categories)

        Returns:
            List of installed HACS repositories or error if HACS is not available.
        """
        try:
            # Check if HACS is available
            is_available, error_msg = await _check_hacs_available(client)
            if not is_available:
                return await add_timezone_metadata(client, {
                    "success": False,
                    "error": error_msg,
                    "error_code": "HACS_NOT_AVAILABLE",
                    "suggestions": [
                        "Install HACS from https://hacs.xyz/",
                        "Ensure Home Assistant has been restarted after HACS installation",
                        "Check Home Assistant logs for HACS errors",
                    ],
                })

            # Get installed repositories via WebSocket
            from ..client.websocket_client import get_websocket_client
            ws_client = await get_websocket_client()

            # Build command parameters - map user-friendly category to HACS internal name
            kwargs_cmd: dict[str, Any] = {}
            if category:
                hacs_category = CATEGORY_MAP.get(category, category)
                kwargs_cmd["categories"] = [hacs_category]

            response = await ws_client.send_command("hacs/repositories/list", **kwargs_cmd)

            if not response.get("success"):
                exception_to_structured_error(
                    Exception(f"HACS repositories list request failed: {response}"),
                    context={"command": "hacs/repositories/list", "category": category},
                    raise_error=True,
                )

            repositories = response.get("result", [])

            # Filter to only installed repositories and extract key info
            installed = []
            for repo in repositories:
                if repo.get("installed", False):
                    # Map HACS internal category back to user-friendly name
                    repo_category = repo.get("category", "")
                    display_category = CATEGORY_DISPLAY.get(repo_category, repo_category)
                    installed.append({
                        "name": repo.get("name"),
                        "full_name": repo.get("full_name"),
                        "category": display_category,
                        "id": repo.get("id"),  # Include numeric ID for repository_info
                        "installed_version": repo.get("installed_version"),
                        "available_version": repo.get("available_version"),
                        "pending_update": repo.get("pending_upgrade", False),
                        "description": repo.get("description"),
                        "authors": repo.get("authors", []),
                        "domain": repo.get("domain"),  # For integrations
                        "stars": repo.get("stars", 0),
                    })

            return await add_timezone_metadata(client, {
                "success": True,
                "category_filter": category,
                "total_installed": len(installed),
                "repositories": installed,
            })

        except Exception as e:
            error_response = exception_to_structured_error(
                e,
                context={"tool": "ha_hacs_list_installed", "category": category},
                raise_error=False,
            )
            if "error" in error_response and isinstance(error_response["error"], dict):
                error_response["error"]["suggestions"] = [
                    "Verify HACS is installed: https://hacs.xyz/",
                    "Check category name is valid: integration, lovelace, theme, appdaemon, python_script",
                    "Check Home Assistant connection",
                ]
            error_with_tz = await add_timezone_metadata(client, error_response)
            raise_tool_error(error_with_tz)

    @mcp.tool(annotations={"idempotentHint": True, "readOnlyHint": True, "tags": ["hacs", "search"], "title": "Search HACS Store"})
    @log_tool_usage
    async def ha_hacs_search(
        query: str,
        category: Annotated[
            Literal["integration", "lovelace", "theme", "appdaemon", "python_script"] | None,
            Field(
                default=None,
                description="Filter by category (optional)",
            ),
        ] = None,
        max_results: Annotated[
            int | str,
            Field(
                default=10,
                description="Maximum number of results to return (default: 10, max: 100)",
            ),
        ] = 10,
        offset: Annotated[
            int | str,
            Field(
                default=0,
                description="Number of results to skip for pagination (default: 0)",
            ),
        ] = 0,
    ) -> dict[str, Any]:
        """Search HACS store for repositories by keyword with pagination.

        Searches the HACS store for repositories matching the query string.
        Returns repository information including stars, downloads, and descriptions.

        **Use Cases:**
        - Find custom cards: `ha_hacs_search("mushroom", category="lovelace")`
        - Find integrations: `ha_hacs_search("nest", category="integration")`
        - Browse themes: `ha_hacs_search("dark", category="theme")`

        Results include:
        - name: Repository name
        - full_name: Full GitHub repository name
        - description: Repository description
        - category: Type of repository
        - stars: GitHub stars count
        - downloads: Number of HACS installations
        - authors: Repository authors
        - installed: Whether already installed

        Args:
            query: Search query (repository name, description, author)
            category: Filter by category (optional)
            max_results: Maximum results to return (default: 10, max: 100)
            offset: Number of results to skip for pagination (default: 0)

        Returns:
            Search results from HACS store or error if HACS is not available.
        """
        try:
            # Coerce max_results and offset to int
            max_results_int = coerce_int_param(
                max_results,
                "max_results",
                default=10,
                min_value=1,
                max_value=100,
            ) or 10
            offset_int = coerce_int_param(
                offset,
                "offset",
                default=0,
                min_value=0,
            ) or 0

            # Check if HACS is available
            is_available, error_msg = await _check_hacs_available(client)
            if not is_available:
                return await add_timezone_metadata(client, {
                    "success": False,
                    "error": error_msg,
                    "error_code": "HACS_NOT_AVAILABLE",
                    "suggestions": [
                        "Install HACS from https://hacs.xyz/",
                        "Ensure Home Assistant has been restarted after HACS installation",
                        "Check Home Assistant logs for HACS errors",
                    ],
                })

            # Get all repositories via WebSocket
            from ..client.websocket_client import get_websocket_client
            ws_client = await get_websocket_client()

            # Build command parameters - map user-friendly category to HACS internal name
            kwargs_cmd: dict[str, Any] = {}
            if category:
                hacs_category = CATEGORY_MAP.get(category, category)
                kwargs_cmd["categories"] = [hacs_category]

            response = await ws_client.send_command("hacs/repositories/list", **kwargs_cmd)

            if not response.get("success"):
                exception_to_structured_error(
                    Exception(f"HACS search request failed: {response}"),
                    context={"command": "hacs/repositories/list", "query": query, "category": category},
                    raise_error=True,
                )

            all_repositories = response.get("result", [])

            # Simple search: filter by query string in name, description, or authors
            query_lower = query.lower().strip()
            matches = []

            for repo in all_repositories:
                # Handle None values safely
                name = (repo.get("name") or "").lower()
                description = (repo.get("description") or "").lower()
                full_name = (repo.get("full_name") or "").lower()
                authors_list = repo.get("authors") or []
                authors = " ".join(authors_list).lower()

                # Calculate relevance score
                score = 0
                if query_lower in name:
                    score += 100
                if query_lower in full_name:
                    score += 50
                if query_lower in description:
                    score += 30
                if query_lower in authors:
                    score += 20

                if score > 0:
                    # Map HACS internal category back to user-friendly name
                    repo_category = repo.get("category", "")
                    display_category = CATEGORY_DISPLAY.get(repo_category, repo_category)
                    matches.append({
                        "name": repo.get("name"),
                        "full_name": repo.get("full_name"),
                        "description": repo.get("description"),
                        "category": display_category,
                        "id": repo.get("id"),  # Include numeric ID for repository_info
                        "stars": repo.get("stars", 0),
                        "downloads": repo.get("downloads", 0),
                        "authors": authors_list,
                        "installed": repo.get("installed", False),
                        "installed_version": repo.get("installed_version") if repo.get("installed") else None,
                        "available_version": repo.get("available_version"),
                        "score": score,
                    })

            # Sort by score (descending) and apply offset + limit
            matches.sort(key=lambda x: x["score"], reverse=True)
            limited_matches = matches[offset_int:offset_int + max_results_int]
            has_more = (offset_int + len(limited_matches)) < len(matches)

            return await add_timezone_metadata(client, {
                "success": True,
                "query": query,
                "category_filter": category,
                "total_matches": len(matches),
                "offset": offset_int,
                "limit": max_results_int,
                "count": len(limited_matches),
                "has_more": has_more,
                "next_offset": offset_int + max_results_int if has_more else None,
                "results": limited_matches,
            })

        except Exception as e:
            error_response = exception_to_structured_error(
                e,
                context={"tool": "ha_hacs_search", "query": query, "category": category},
                raise_error=False,
            )
            if "error" in error_response and isinstance(error_response["error"], dict):
                error_response["error"]["suggestions"] = [
                    "Verify HACS is installed: https://hacs.xyz/",
                    "Try a simpler search query",
                    "Check category name is valid: integration, lovelace, theme, appdaemon, python_script",
                ]
            error_with_tz = await add_timezone_metadata(client, error_response)
            raise_tool_error(error_with_tz)

    @mcp.tool(annotations={"idempotentHint": True, "readOnlyHint": True, "tags": ["hacs", "info"], "title": "Get HACS Repository Info"})
    @log_tool_usage
    async def ha_hacs_repository_info(repository_id: str) -> dict[str, Any]:
        """Get detailed repository information including README and documentation.

        Returns comprehensive information about a HACS repository:
        - Basic info (name, description, category, authors)
        - Installation status and versions
        - README content (useful for configuration examples)
        - Available releases and versions
        - GitHub stats (stars, issues)
        - Configuration examples (if available)

        **Use Cases:**
        - Get card configuration examples: `ha_hacs_repository_info("441028036")`
        - Check integration setup instructions
        - Find theme customization options

        **Note:** The repository_id is the numeric ID from HACS, not the GitHub path.
        Use `ha_hacs_list_installed()` or `ha_hacs_search()` to find the numeric ID.

        Args:
            repository_id: Repository numeric ID (e.g., "441028036") or GitHub path (e.g., "dvd-dev/hilo")

        Returns:
            Detailed repository information or error if not found.
        """
        try:
            # Check if HACS is available
            is_available, error_msg = await _check_hacs_available(client)
            if not is_available:
                return await add_timezone_metadata(client, {
                    "success": False,
                    "error": error_msg,
                    "error_code": "HACS_NOT_AVAILABLE",
                    "suggestions": [
                        "Install HACS from https://hacs.xyz/",
                        "Ensure Home Assistant has been restarted after HACS installation",
                        "Check Home Assistant logs for HACS errors",
                    ],
                })

            from ..client.websocket_client import get_websocket_client
            ws_client = await get_websocket_client()

            # If repository_id contains a slash, it's a GitHub path - need to look up numeric ID
            actual_id = repository_id
            if "/" in repository_id:
                # Look up the numeric ID from the repository list
                list_response = await ws_client.send_command("hacs/repositories/list")
                if list_response.get("success"):
                    repos = list_response.get("result", [])
                    for repo in repos:
                        if repo.get("full_name", "").lower() == repository_id.lower():
                            actual_id = str(repo.get("id"))
                            break
                    else:
                        return await add_timezone_metadata(client, {
                            "success": False,
                            "error": f"Repository '{repository_id}' not found in HACS",
                            "error_code": "REPOSITORY_NOT_FOUND",
                            "suggestions": [
                                "Use ha_hacs_search() to find the repository",
                                "Check the repository name is correct (case-insensitive)",
                                "The repository may need to be added to HACS first",
                            ],
                        })

            # Get repository info via WebSocket using numeric ID
            response = await ws_client.send_command("hacs/repository/info", repository_id=actual_id)

            if not response.get("success"):
                exception_to_structured_error(
                    Exception(f"HACS repository info request failed: {response}"),
                    context={"command": "hacs/repository/info", "repository_id": repository_id},
                    raise_error=True,
                )

            result = response.get("result", {})

            # Extract and structure the most useful information
            return await add_timezone_metadata(client, {
                "success": True,
                "repository_id": repository_id,
                "name": result.get("name"),
                "full_name": result.get("full_name"),
                "description": result.get("description"),
                "category": result.get("category"),
                "authors": result.get("authors", []),
                "domain": result.get("domain"),  # For integrations
                "installed": result.get("installed", False),
                "installed_version": result.get("installed_version"),
                "available_version": result.get("available_version"),
                "pending_update": result.get("pending_upgrade", False),
                "stars": result.get("stars", 0),
                "downloads": result.get("downloads", 0),
                "topics": result.get("topics", []),
                "releases": result.get("releases", []),
                "default_branch": result.get("default_branch"),
                "readme": result.get("readme"),  # Full README content
                "data": result,  # Full response for advanced use
            })

        except Exception as e:
            error_response = exception_to_structured_error(
                e,
                context={"tool": "ha_hacs_repository_info", "repository_id": repository_id},
                raise_error=False,
            )
            if "error" in error_response and isinstance(error_response["error"], dict):
                error_response["error"]["suggestions"] = [
                    "Verify HACS is installed: https://hacs.xyz/",
                    "Check repository ID format (e.g., 'hacs/integration' or 'owner/repo')",
                    "Use ha_hacs_search() to find the correct repository ID",
                ]
            error_with_tz = await add_timezone_metadata(client, error_response)
            raise_tool_error(error_with_tz)

    @mcp.tool(annotations={"destructiveHint": True, "tags": ["hacs", "management"], "title": "Add HACS Repository"})
    @log_tool_usage
    async def ha_hacs_add_repository(
        repository: str,
        category: Annotated[
            Literal["integration", "lovelace", "theme", "appdaemon", "python_script"],
            Field(
                description="Repository category (required)",
            ),
        ],
    ) -> dict[str, Any]:
        """Add a custom GitHub repository to HACS.

        Allows adding custom repositories that are not in the default HACS store.
        This is useful for:
        - Adding custom integrations from GitHub
        - Installing custom Lovelace cards
        - Adding custom themes
        - Installing beta/development versions

        **Requirements:**
        - Repository must be a valid GitHub repository
        - Repository must follow HACS structure guidelines
        - Category must match the repository type

        **Examples:**
        ```python
        # Add custom integration
        ha_hacs_add_repository("owner/custom-integration", category="integration")

        # Add custom card
        ha_hacs_add_repository("owner/custom-card", category="lovelace")

        # Add custom theme
        ha_hacs_add_repository("owner/custom-theme", category="theme")
        ```

        Args:
            repository: GitHub repository in format "owner/repo"
            category: Repository category (integration, lovelace, theme, appdaemon, python_script)

        Returns:
            Success status and repository ID if added successfully.
        """
        try:
            # Check if HACS is available
            is_available, error_msg = await _check_hacs_available(client)
            if not is_available:
                return await add_timezone_metadata(client, {
                    "success": False,
                    "error": error_msg,
                    "error_code": "HACS_NOT_AVAILABLE",
                    "suggestions": [
                        "Install HACS from https://hacs.xyz/",
                        "Ensure Home Assistant has been restarted after HACS installation",
                        "Check Home Assistant logs for HACS errors",
                    ],
                })

            # Validate repository format
            if "/" not in repository:
                return await add_timezone_metadata(client, {
                    "success": False,
                    "error": "Invalid repository format. Must be 'owner/repo'",
                    "error_code": "INVALID_REPOSITORY_FORMAT",
                    "suggestions": [
                        "Use format: 'owner/repo' (e.g., 'hacs/integration')",
                        "Check the repository exists on GitHub",
                    ],
                })

            # Add repository via WebSocket
            from ..client.websocket_client import get_websocket_client
            ws_client = await get_websocket_client()

            # Map user-friendly category to HACS internal name
            hacs_category = CATEGORY_MAP.get(category, category)

            response = await ws_client.send_command(
                "hacs/repositories/add",
                repository=repository,
                category=hacs_category,
            )

            if not response.get("success"):
                exception_to_structured_error(
                    Exception(f"HACS add repository request failed: {response}"),
                    context={
                        "command": "hacs/repositories/add",
                        "repository": repository,
                        "category": category,
                    },
                    raise_error=True,
                )

            result = response.get("result", {})

            return await add_timezone_metadata(client, {
                "success": True,
                "repository": repository,
                "category": category,
                "repository_id": result.get("id"),
                "message": f"Successfully added {repository} to HACS",
                "data": result,
            })

        except Exception as e:
            error_response = exception_to_structured_error(
                e,
                context={
                    "tool": "ha_hacs_add_repository",
                    "repository": repository,
                    "category": category,
                },
                raise_error=False,
            )
            if "error" in error_response and isinstance(error_response["error"], dict):
                error_response["error"]["suggestions"] = [
                    "Verify HACS is installed: https://hacs.xyz/",
                    "Check repository format: 'owner/repo'",
                    "Verify the repository exists on GitHub",
                    "Ensure category matches repository type",
                    "Check repository follows HACS guidelines: https://hacs.xyz/docs/publish/start",
                ]
            error_with_tz = await add_timezone_metadata(client, error_response)
            raise_tool_error(error_with_tz)

    @mcp.tool(annotations={"destructiveHint": True, "tags": ["hacs", "management"], "title": "Download/Install HACS Repository"})
    @log_tool_usage
    async def ha_hacs_download(
        repository_id: str,
        version: Annotated[
            str | None,
            Field(
                default=None,
                description="Specific version to install (e.g., 'v1.2.3'). If not specified, installs the latest version.",
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Download and install a HACS repository.

        This installs a repository from HACS to your Home Assistant instance.
        For integrations, a restart of Home Assistant may be required after installation.

        **Prerequisites:**
        - The repository must already be in HACS (either from the default store or added via `ha_hacs_add_repository`)
        - Use `ha_hacs_search()` or `ha_hacs_list_installed()` to find the repository ID

        **Examples:**
        ```python
        # Install latest version of a repository
        ha_hacs_download("441028036")

        # Install specific version
        ha_hacs_download("441028036", version="v2.0.0")

        # Install by GitHub path (will look up the numeric ID)
        ha_hacs_download("piitaya/lovelace-mushroom", version="v4.0.0")
        ```

        **Note:** For integrations, you may need to restart Home Assistant after installation.
        For Lovelace cards, clear your browser cache to see the new card.

        Args:
            repository_id: Repository numeric ID or GitHub path (e.g., "441028036" or "owner/repo")
            version: Specific version to install (optional, defaults to latest)

        Returns:
            Success status and installation details.
        """
        try:
            # Check if HACS is available
            is_available, error_msg = await _check_hacs_available(client)
            if not is_available:
                return await add_timezone_metadata(client, {
                    "success": False,
                    "error": error_msg,
                    "error_code": "HACS_NOT_AVAILABLE",
                    "suggestions": [
                        "Install HACS from https://hacs.xyz/",
                        "Ensure Home Assistant has been restarted after HACS installation",
                        "Check Home Assistant logs for HACS errors",
                    ],
                })

            from ..client.websocket_client import get_websocket_client
            ws_client = await get_websocket_client()

            # If repository_id contains a slash, it's a GitHub path - need to look up numeric ID
            actual_id = repository_id
            repo_name = repository_id
            if "/" in repository_id:
                # Look up the numeric ID from the repository list
                list_response = await ws_client.send_command("hacs/repositories/list")
                if list_response.get("success"):
                    repos = list_response.get("result", [])
                    for repo in repos:
                        if repo.get("full_name", "").lower() == repository_id.lower():
                            actual_id = str(repo.get("id"))
                            repo_name = repo.get("name") or repository_id
                            break
                    else:
                        return await add_timezone_metadata(client, {
                            "success": False,
                            "error": f"Repository '{repository_id}' not found in HACS",
                            "error_code": "REPOSITORY_NOT_FOUND",
                            "suggestions": [
                                "Use ha_hacs_add_repository() to add the repository first",
                                "Use ha_hacs_search() to find available repositories",
                                "Check the repository name is correct (case-insensitive)",
                            ],
                        })

            # Build download command parameters
            download_kwargs: dict[str, Any] = {"repository": actual_id}
            if version:
                download_kwargs["version"] = version

            # Download/install the repository
            response = await ws_client.send_command("hacs/repository/download", **download_kwargs)

            if not response.get("success"):
                exception_to_structured_error(
                    Exception(f"HACS download request failed: {response}"),
                    context={
                        "command": "hacs/repository/download",
                        "repository_id": repository_id,
                        "version": version,
                    },
                    raise_error=True,
                )

            result = response.get("result", {})

            return await add_timezone_metadata(client, {
                "success": True,
                "repository_id": actual_id,
                "repository": repo_name,
                "version": version or "latest",
                "message": f"Successfully installed {repo_name}" + (f" version {version}" if version else ""),
                "note": "For integrations, restart Home Assistant to activate. For Lovelace cards, clear browser cache.",
                "data": result,
            })

        except Exception as e:
            error_response = exception_to_structured_error(
                e,
                context={
                    "tool": "ha_hacs_download",
                    "repository_id": repository_id,
                    "version": version,
                },
                raise_error=False,
            )
            if "error" in error_response and isinstance(error_response["error"], dict):
                error_response["error"]["suggestions"] = [
                    "Verify HACS is installed: https://hacs.xyz/",
                    "Check repository ID is valid (use ha_hacs_search() to find it)",
                    "Ensure the repository is in HACS (use ha_hacs_add_repository() if needed)",
                    "Check version format (e.g., 'v1.2.3' or '1.2.3')",
                ]
            error_with_tz = await add_timezone_metadata(client, error_response)
            raise_tool_error(error_with_tz)
