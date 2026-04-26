"""
HACS (Home Assistant Community Store) integration tools for Home Assistant MCP server.

This module provides tools to interact with HACS via the WebSocket API, enabling AI agents
to discover custom integrations, Lovelace cards, themes, and more.
"""

import logging
from typing import Annotated, Any, Literal

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
    error_message = error.get("message", "") if isinstance(error, dict) else str(error)

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


class HacsTools:
    """HACS integration tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @tool(
        name="ha_get_hacs",
        tags={"HACS"},
        annotations={
            "readOnlyHint": True,
            "title": "Get HACS",
        },
    )
    @log_tool_usage
    async def ha_get_hacs(
        self,
        action: Annotated[
            Literal["search", "info"],
            Field(description="The action to perform"),
        ],
        query: Annotated[
            str, Field(description="Search query (for 'search' action)")
        ] = "",
        category: Annotated[
            Literal["integration", "lovelace", "theme", "appdaemon", "python_script"]
            | None,
            Field(description="Filter by category (for 'search' action)"),
        ] = None,
        installed_only: Annotated[
            bool | str,
            Field(description="Only return installed repositories (for 'search' action)"),
        ] = False,
        max_results: Annotated[
            int | str,
            Field(description="Maximum number of results to return (for 'search' action)"),
        ] = 10,
        offset: Annotated[
            int | str,
            Field(description="Number of results to skip for pagination (for 'search' action)"),
        ] = 0,
        repository_id: Annotated[
            str | None,
            Field(description="Repository numeric ID or GitHub path like 'owner/repo' (for 'info' action)"),
        ] = None,
    ) -> dict[str, Any]:
        """Get HACS (Home Assistant Community Store) information.

        Use this tool to search the store or get detailed repository info.

        Do NOT use this tool for general Home Assistant configuration or entity control;
        use domain-specific tools instead.

        **Actions:**
        1. `search`: Search for repositories or list installed ones.
           - Provide `query`, `category`, `installed_only`, `max_results`, `offset`.
        2. `info`: Get detailed repository information including README.
           - Requires `repository_id` (numeric ID or 'owner/repo').

        **Examples:**
        - Search: ha_get_hacs(action='search', query='mushroom', category='lovelace')
        - List installed: ha_get_hacs(action='search', installed_only=True)
        - Get Info: ha_get_hacs(action='info', repository_id='441028036')
        """
        try:
            await _assert_hacs_available()

            if action == "search":
                return await self._hacs_search(
                    query, category, installed_only, max_results, offset
                )
            elif action == "info":
                if not repository_id:
                    raise ValueError("repository_id is required for 'info' action")
                return await self._hacs_info(repository_id)
            else:
                raise ValueError(f"Unknown action: {action}")

        except ValueError as e:
            raise_tool_error(
                create_error_response(
                    ErrorCode.INVALID_REQUEST,
                    str(e),
                )
            )
        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={
                    "tool": "ha_get_hacs",
                    "action": action,
                },
                suggestions=[
                    "Verify HACS is installed: https://hacs.xyz/",
                    "Ensure you provide the correct parameters for the chosen action.",
                ],
            )
            return {}  # unreachable

    @tool(
        name="ha_manage_hacs",
        tags={"HACS"},
        annotations={
            "destructiveHint": True,
            "title": "Set HACS",
        },
    )
    @log_tool_usage
    async def ha_manage_hacs(
        self,
        action: Annotated[
            Literal["download", "add_repository"],
            Field(description="The action to perform"),
        ],
        repository_id: Annotated[
            str | None,
            Field(description="Repository numeric ID or GitHub path like 'owner/repo' (for 'download' action)"),
        ] = None,
        version: Annotated[
            str | None,
            Field(description="Specific version to install (for 'download' action)"),
        ] = None,
        repository: Annotated[
            str | None,
            Field(description="GitHub repository in format 'owner/repo' (for 'add_repository' action)"),
        ] = None,
        category: Annotated[
            Literal["integration", "lovelace", "theme", "appdaemon", "python_script"]
            | None,
            Field(description="Repository category (for 'add_repository' action)"),
        ] = None,
    ) -> dict[str, Any]:
        """Manage HACS (Home Assistant Community Store) installations.

        Use this tool to download/install packages or add custom repositories.
        This tool handles both installation and updates of repositories.

        Do NOT use this tool for general Home Assistant configuration or entity control;
        use domain-specific tools instead.

        **Actions:**
        1. `download`: Install or update a repository.
           - Requires `repository_id`, optionally `version`.
        2. `add_repository`: Add a custom GitHub repository.
           - Requires `repository` (e.g. 'owner/repo') and `category`.

        **Caveats:**
        - For integrations, a restart of Home Assistant may be required after installation.
        - For Lovelace cards, clear your browser cache to see the new card.

        **Examples:**
        - Install/Download: ha_manage_hacs(action='download', repository_id='piitaya/lovelace-mushroom')
        - Add Custom Repo: ha_manage_hacs(action='add_repository', repository='owner/my-card', category='lovelace')
        """
        try:
            await _assert_hacs_available()

            if action == "download":
                if not repository_id:
                    raise ValueError("repository_id is required for 'download' action")
                return await self._hacs_download(repository_id, version)
            elif action == "add_repository":
                if not repository or not category:
                    raise ValueError(
                        "repository and category are required for 'add_repository' action"
                    )
                return await self._hacs_add_repository(repository, category)
            else:
                raise ValueError(f"Unknown action: {action}")

        except ValueError as e:
            raise_tool_error(
                create_error_response(
                    ErrorCode.INVALID_REQUEST,
                    str(e),
                )
            )
        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={
                    "tool": "ha_manage_hacs",
                    "action": action,
                },
                suggestions=[
                    "Verify HACS is installed: https://hacs.xyz/",
                    "Ensure you provide the correct parameters for the chosen action.",
                ],
            )
            return {}  # unreachable

    async def _hacs_search(
        self,
        query: str,
        category: str | None,
        installed_only: Any,
        max_results: Any,
        offset: Any,
    ) -> dict[str, Any]:
        installed_only_bool = coerce_bool_param(
            installed_only, "installed_only", default=False
        )
        max_results_int = coerce_int_param(
            max_results, "max_results", default=10, min_value=1, max_value=100
        )
        offset_int = coerce_int_param(offset, "offset", default=0, min_value=0)

        from ..client.websocket_client import get_websocket_client

        ws_client = await get_websocket_client()

        kwargs_cmd: dict[str, Any] = {}
        if category:
            kwargs_cmd["categories"] = [CATEGORY_MAP.get(category, category)]

        response = await ws_client.send_command("hacs/repositories/list", **kwargs_cmd)

        if not response.get("success"):
            raise_tool_error(
                create_error_response(
                    ErrorCode.INVALID_REQUEST,
                    f"HACS search request failed: {response.get('error', response)}",
                )
            )

        all_repositories = response.get("result", [])
        matches = _filter_and_score_repos(all_repositories, query, installed_only_bool)

        limited_matches = matches[offset_int : offset_int + max_results_int]
        has_more = (offset_int + len(limited_matches)) < len(matches)

        return await add_timezone_metadata(
            self._client,
            {
                "success": True,
                "query": query if query.strip() else None,
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

    async def _hacs_info(self, repository_id: str) -> dict[str, Any]:
        from ..client.websocket_client import get_websocket_client

        ws_client = await get_websocket_client()

        actual_id, _ = await _resolve_hacs_repo_id(ws_client, repository_id)
        response = await ws_client.send_command(
            "hacs/repository/info", repository_id=actual_id
        )

        if not response.get("success"):
            raise_tool_error(
                create_error_response(
                    ErrorCode.INVALID_REQUEST,
                    f"HACS repository info request failed: {response.get('error', response)}",
                )
            )

        result = response.get("result", {})
        return await add_timezone_metadata(
            self._client,
            {
                "success": True,
                "repository_id": repository_id,
                "name": result.get("name"),
                "full_name": result.get("full_name"),
                "description": result.get("description"),
                "category": result.get("category"),
                "authors": result.get("authors", []),
                "domain": result.get("domain"),
                "installed": result.get("installed", False),
                "installed_version": result.get("installed_version"),
                "available_version": result.get("available_version"),
                "pending_update": result.get("pending_upgrade", False),
                "stars": result.get("stars", 0),
                "downloads": result.get("downloads", 0),
                "topics": result.get("topics", []),
                "releases": result.get("releases", []),
                "default_branch": result.get("default_branch"),
                "readme": result.get("readme"),
                "data": result,
            },
        )

    async def _hacs_download(
        self, repository_id: str, version: str | None
    ) -> dict[str, Any]:
        from ..client.websocket_client import get_websocket_client

        ws_client = await get_websocket_client()

        actual_id, repo_name = await _resolve_hacs_repo_id(ws_client, repository_id)

        download_kwargs: dict[str, Any] = {"repository": actual_id}
        if version:
            download_kwargs["version"] = version

        response = await ws_client.send_command(
            "hacs/repository/download", **download_kwargs
        )

        if not response.get("success"):
            raise_tool_error(
                create_error_response(
                    ErrorCode.INVALID_REQUEST,
                    f"HACS download request failed: {response.get('error', response)}",
                )
            )

        return await add_timezone_metadata(
            self._client,
            {
                "success": True,
                "repository_id": actual_id,
                "repository": repo_name,
                "version": version or "latest",
                "message": f"Successfully installed {repo_name}"
                + (f" version {version}" if version else ""),
                "note": "For integrations, restart HA. For UI cards, clear browser cache.",
                "data": response.get("result", {}),
            },
        )

    async def _hacs_add_repository(
        self, repository: str, category: str
    ) -> dict[str, Any]:
        if "/" not in repository:
            raise ValueError("Invalid repository format. Must be 'owner/repo'")

        from ..client.websocket_client import get_websocket_client

        ws_client = await get_websocket_client()

        hacs_category = CATEGORY_MAP.get(category, category)
        response = await ws_client.send_command(
            "hacs/repositories/add",
            repository=repository,
            category=hacs_category,
        )

        if not response.get("success"):
            raise_tool_error(
                create_error_response(
                    ErrorCode.INVALID_REQUEST,
                    f"HACS add repository request failed: {response.get('error', response)}",
                )
            )

        return await add_timezone_metadata(
            self._client,
            {
                "success": True,
                "repository": repository,
                "category": category,
                "repository_id": response.get("result", {}).get("id"),
                "message": f"Successfully added {repository} to HACS",
                "data": response.get("result", {}),
            },
        )


def register_hacs_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    register_tool_methods(mcp, HacsTools(client))


def _filter_and_score_repos(
    all_repositories: list[dict[str, Any]],
    query: str,
    installed_only: bool | None,
) -> list[dict[str, Any]]:
    query_lower = query.lower().strip()
    matches = []

    for repo in all_repositories:
        if installed_only and not repo.get("installed", False):
            continue

        name = (repo.get("name") or "").lower()
        description = (repo.get("description") or "").lower()
        full_name = (repo.get("full_name") or "").lower()
        authors_list = repo.get("authors") or []
        authors = " ".join(authors_list).lower()

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
            score = 0

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

    if query_lower:
        matches.sort(key=lambda x: x.get("score", 0), reverse=True)
    else:
        matches.sort(key=lambda x: (x.get("name") or "").lower())

    return matches


async def _resolve_hacs_repo_id(ws_client: Any, repository_id: str) -> tuple[str, str]:
    if "/" not in repository_id:
        return repository_id, repository_id

    list_response = await ws_client.send_command("hacs/repositories/list")
    if list_response.get("success"):
        repos = list_response.get("result", [])
        for repo in repos:
            if repo.get("full_name", "").lower() == repository_id.lower():
                return str(repo.get("id")), repo.get("name") or repository_id

    raise_tool_error(
        create_error_response(
            ErrorCode.RESOURCE_NOT_FOUND,
            f"Repository '{repository_id}' not found in HACS",
            suggestions=[
                "Use ha_manage_hacs(action='search') to find the repository",
                "Check the repository name is correct (case-insensitive)",
                "The repository may need to be added to HACS first",
            ],
        )
    )
    return repository_id, repository_id
