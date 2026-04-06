"""
HACS (Home Assistant Community Store) integration tools for Home Assistant MCP server.

This module provides tools to interact with HACS via the WebSocket API, enabling AI agents
to discover custom integrations, Lovelace cards, themes, and more.
"""

import logging
from typing import Annotated, Any, Literal

from fastmcp.exceptions import ToolError
from pydantic import Field

from ..errors import ErrorCode, create_error_response
from .helpers import exception_to_structured_error, log_tool_usage, raise_tool_error
from .util_helpers import add_timezone_metadata, coerce_bool_param, coerce_int_param

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


async def _assert_hacs_available() -> None:
    """Raise ToolError if HACS is not installed or not responding.

    Distinguishes "unknown command" (HACS not installed) from other failures
    (HACS installed but broken) so the error message is accurate.

    Must be called within a try block that handles API errors via
    exception_to_structured_error, so connection failures are classified
    correctly rather than masked as COMPONENT_NOT_INSTALLED.
    """
    from ..client.websocket_client import get_websocket_client

    ws_client = await get_websocket_client()
    response = await ws_client.send_command("hacs/info")
    if response.get("success"):
        return

    error = response.get("error", {})
    error_code = error.get("code") if isinstance(error, dict) else None
    error_message = (
        error.get("message", "") if isinstance(error, dict) else str(error)
    )

    # "unknown_command" means HACS is not installed at all
    if error_code == "unknown_command" or "unknown command" in error_message.lower():
        raise_tool_error(
            create_error_response(
                ErrorCode.COMPONENT_NOT_INSTALLED,
                "HACS is not installed.",
                suggestions=[
                    "Install HACS from https://hacs.xyz/",
                    "Restart Home Assistant after HACS installation",
                ],
            )
        )

    # HACS is installed but not responding correctly
    raise_tool_error(
        create_error_response(
            ErrorCode.COMPONENT_NOT_INSTALLED,
            f"HACS is installed but not responding: {error_message or 'unknown error'}",
            suggestions=[
                "Restart Home Assistant",
                "Check Home Assistant logs for HACS errors",
                "Verify HACS is up to date",
            ],
        )
    )


def register_hacs_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register HACS integration tools with the MCP server."""

    @mcp.tool(
        tags={"HACS"},
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "title": "Search HACS Store",
        },
    )
    @log_tool_usage
    async def ha_hacs_search(
        query: str = "",
        category: Annotated[
            Literal["integration", "lovelace", "theme", "appdaemon", "python_script"]
            | None,
            Field(
                default=None,
                description="Filter by category (optional)",
            ),
        ] = None,
        installed_only: Annotated[
            bool | str,
            Field(
                default=False,
                description="Only return installed repositories (default: False)",
            ),
        ] = False,
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
        """Search HACS store for repositories, or list installed repositories.

        **Search mode** (default): Searches by keyword across name, description, and authors.
        **Browse mode** (no query, `installed_only=False`): Returns all HACS store repos
        sorted alphabetically, paginated by `max_results` and `offset`.
        **Installed mode** (`installed_only=True`): Lists installed repos (no query needed).

        **DASHBOARD TIP:** Use `installed_only=True, category="lovelace"` to discover
        installed custom cards for use with `ha_config_set_dashboard()`.

        **Examples:**
        - Find custom cards: `ha_hacs_search("mushroom", category="lovelace")`
        - Find integrations: `ha_hacs_search("nest", category="integration")`
        - List installed: `ha_hacs_search(installed_only=True)`
        - Installed by category: `ha_hacs_search(installed_only=True, category="lovelace")`

        Args:
            query: Search query (repository name, description, author). Empty string with
                  installed_only=True lists all installed repos.
            category: Filter by category (optional)
            installed_only: Only return installed repositories (default: False)
            max_results: Maximum results to return (default: 10, max: 100)
            offset: Number of results to skip for pagination (default: 0)
        """
        try:
            # Coerce parameters
            installed_only_bool = coerce_bool_param(
                installed_only, "installed_only", default=False
            )
            max_results_int = coerce_int_param(
                max_results,
                "max_results",
                default=10,
                min_value=1,
                max_value=100,
            )
            offset_int = coerce_int_param(
                offset,
                "offset",
                default=0,
                min_value=0,
            )

            # Check if HACS is available
            await _assert_hacs_available()

            # Get all repositories via WebSocket
            from ..client.websocket_client import get_websocket_client

            ws_client = await get_websocket_client()

            # Build command parameters - map user-friendly category to HACS internal name
            kwargs_cmd: dict[str, Any] = {}
            if category:
                hacs_category = CATEGORY_MAP.get(category, category)
                kwargs_cmd["categories"] = [hacs_category]

            response = await ws_client.send_command(
                "hacs/repositories/list", **kwargs_cmd
            )

            if not response.get("success"):
                exception_to_structured_error(
                    Exception(f"HACS search request failed: {response}"),
                    context={
                        "command": "hacs/repositories/list",
                        "query": query,
                        "category": category,
                    },
                    raise_error=True,
                )

            all_repositories = response.get("result", [])

            query_lower = query.lower().strip()
            matches = []

            for repo in all_repositories:
                # Filter to installed only when requested
                if installed_only_bool and not repo.get("installed", False):
                    continue

                # Handle None values safely
                name = (repo.get("name") or "").lower()
                description = (repo.get("description") or "").lower()
                full_name = (repo.get("full_name") or "").lower()
                authors_list = repo.get("authors") or []
                authors = " ".join(authors_list).lower()

                # Calculate relevance score (all repos match when query is empty)
                if query_lower:
                    score = 0
                    if query_lower in name:
                        score += 100
                    if query_lower in full_name:
                        score += 50
                    if query_lower in description:
                        score += 30
                    if query_lower in authors:
                        score += 20
                    if score == 0:
                        continue
                else:
                    score = 0  # No scoring when listing all

                # Map HACS internal category back to user-friendly name
                repo_category = repo.get("category", "")
                display_category = CATEGORY_DISPLAY.get(repo_category, repo_category)
                entry: dict[str, Any] = {
                    "name": repo.get("name"),
                    "full_name": repo.get("full_name"),
                    "description": repo.get("description"),
                    "category": display_category,
                    "id": repo.get("id"),
                    "stars": repo.get("stars", 0),
                    "downloads": repo.get("downloads", 0),
                    "authors": authors_list,
                    "installed": repo.get("installed", False),
                    "installed_version": repo.get("installed_version")
                    if repo.get("installed")
                    else None,
                    "available_version": repo.get("available_version"),
                }
                if query_lower:
                    entry["score"] = score
                if repo.get("installed"):
                    entry["pending_update"] = repo.get("pending_upgrade", False)
                    entry["domain"] = repo.get("domain")
                matches.append(entry)

            # Sort by score (descending) when searching, by name when listing
            if query_lower:
                matches.sort(key=lambda x: x.get("score", 0), reverse=True)
            else:
                matches.sort(key=lambda x: (x.get("name") or "").lower())

            limited_matches = matches[offset_int : offset_int + max_results_int]
            has_more = (offset_int + len(limited_matches)) < len(matches)

            return await add_timezone_metadata(
                client,
                {
                    "success": True,
                    "query": query if query_lower else None,
                    "category_filter": category,
                    "installed_only": installed_only_bool,
                    "total_matches": len(matches),
                    "offset": offset_int,
                    "limit": max_results_int,
                    "count": len(limited_matches),
                    "has_more": has_more,
                    "next_offset": offset_int + max_results_int if has_more else None,
                    "results": limited_matches,
                },
            )

        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={
                    "tool": "ha_hacs_search",
                    "query": query,
                    "category": category,
                },
                suggestions=[
                    "Verify HACS is installed: https://hacs.xyz/",
                    "Try a simpler search query",
                    "Check category name is valid: integration, lovelace, theme, appdaemon, python_script",
                ],
            )

    @mcp.tool(
        tags={"HACS"},
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "title": "Get HACS Repository Info",
        },
    )
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
        Use `ha_hacs_search()` to find the numeric ID.

        Args:
            repository_id: Repository numeric ID (e.g., "441028036") or GitHub path (e.g., "dvd-dev/hilo")

        Returns:
            Detailed repository information or error if not found.
        """
        try:
            # Check if HACS is available
            await _assert_hacs_available()

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
                        raise_tool_error(
                            create_error_response(
                                ErrorCode.RESOURCE_NOT_FOUND,
                                f"Repository '{repository_id}' not found in HACS",
                                suggestions=[
                                    "Use ha_hacs_search() to find the repository",
                                    "Check the repository name is correct (case-insensitive)",
                                    "The repository may need to be added to HACS first",
                                ],
                            )
                        )

            # Get repository info via WebSocket using numeric ID
            response = await ws_client.send_command(
                "hacs/repository/info", repository_id=actual_id
            )

            if not response.get("success"):
                exception_to_structured_error(
                    Exception(f"HACS repository info request failed: {response}"),
                    context={
                        "command": "hacs/repository/info",
                        "repository_id": repository_id,
                    },
                    raise_error=True,
                )

            result = response.get("result", {})

            # Extract and structure the most useful information
            return await add_timezone_metadata(
                client,
                {
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
                },
            )

        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={
                    "tool": "ha_hacs_repository_info",
                    "repository_id": repository_id,
                },
                suggestions=[
                    "Verify HACS is installed: https://hacs.xyz/",
                    "Check repository ID format (e.g., 'hacs/integration' or 'owner/repo')",
                    "Use ha_hacs_search() to find the correct repository ID",
                ],
            )

    @mcp.tool(
        tags={"HACS"},
        annotations={"destructiveHint": True, "title": "Add HACS Repository"},
    )
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
            await _assert_hacs_available()

            # Validate repository format
            if "/" not in repository:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "Invalid repository format. Must be 'owner/repo'",
                        suggestions=[
                            "Use format: 'owner/repo' (e.g., 'hacs/integration')",
                            "Check the repository exists on GitHub",
                        ],
                    )
                )

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

            return await add_timezone_metadata(
                client,
                {
                    "success": True,
                    "repository": repository,
                    "category": category,
                    "repository_id": result.get("id"),
                    "message": f"Successfully added {repository} to HACS",
                    "data": result,
                },
            )

        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={
                    "tool": "ha_hacs_add_repository",
                    "repository": repository,
                    "category": category,
                },
                suggestions=[
                    "Verify HACS is installed: https://hacs.xyz/",
                    "Check repository format: 'owner/repo'",
                    "Verify the repository exists on GitHub",
                    "Ensure category matches repository type",
                    "Check repository follows HACS guidelines: https://hacs.xyz/docs/publish/start",
                ],
            )

    @mcp.tool(
        tags={"HACS"},
        annotations={
            "destructiveHint": True,
            "title": "Download/Install HACS Repository",
        },
    )
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
        - Use `ha_hacs_search()` to find the repository ID

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
            await _assert_hacs_available()

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
                        raise_tool_error(
                            create_error_response(
                                ErrorCode.RESOURCE_NOT_FOUND,
                                f"Repository '{repository_id}' not found in HACS",
                                suggestions=[
                                    "Use ha_hacs_add_repository() to add the repository first",
                                    "Use ha_hacs_search() to find available repositories",
                                    "Check the repository name is correct (case-insensitive)",
                                ],
                            )
                        )

            # Build download command parameters
            download_kwargs: dict[str, Any] = {"repository": actual_id}
            if version:
                download_kwargs["version"] = version

            # Download/install the repository
            response = await ws_client.send_command(
                "hacs/repository/download", **download_kwargs
            )

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

            return await add_timezone_metadata(
                client,
                {
                    "success": True,
                    "repository_id": actual_id,
                    "repository": repo_name,
                    "version": version or "latest",
                    "message": f"Successfully installed {repo_name}"
                    + (f" version {version}" if version else ""),
                    "note": "For integrations, restart Home Assistant to activate. For Lovelace cards, clear browser cache.",
                    "data": result,
                },
            )

        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={
                    "tool": "ha_hacs_download",
                    "repository_id": repository_id,
                    "version": version,
                },
                suggestions=[
                    "Verify HACS is installed: https://hacs.xyz/",
                    "Check repository ID is valid (use ha_hacs_search() to find it)",
                    "Ensure the repository is in HACS (use ha_hacs_add_repository() if needed)",
                    "Check version format (e.g., 'v1.2.3' or '1.2.3')",
                ],
            )
