"""
HACS (Home Assistant Community Store) integration tools for Home Assistant MCP server.

This module provides tools to interact with HACS via the WebSocket API, enabling AI agents
to discover custom integrations, Lovelace cards, themes, and more.
"""

import asyncio
import logging
import time
from typing import Annotated, Any, Literal

from fastmcp import Context
from fastmcp.exceptions import ToolError
from fastmcp.tools import tool
from pydantic import Field

from ..errors import ErrorCode, create_error_response
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
    safe_info,
    safe_progress,
    validate_identifier_not_empty,
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


class HacsTools:
    """HACS integration tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @tool(
        name="ha_hacs_search",
        tags={"HACS"},
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "title": "Search HACS Store",
        },
    )
    @log_tool_usage
    async def ha_hacs_search(
        self,
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
        ctx: Context | None = None,
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

            await safe_info(
                ctx,
                f"ha_hacs_search starting: query={query!r} "
                f"category={category} installed_only={installed_only_bool}",
            )
            await safe_progress(
                ctx,
                progress=0,
                total=3,
                message="checking HACS availability",
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

            await safe_progress(
                ctx,
                progress=1,
                total=3,
                message="fetching HACS repository list",
            )

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
            await safe_progress(
                ctx,
                progress=2,
                total=3,
                message=f"filtering {len(all_repositories)} repositories",
            )
            matches = _filter_and_score_repos(
                all_repositories, query, installed_only_bool
            )
            await safe_progress(
                ctx,
                progress=3,
                total=3,
                message=f"matched {len(matches)} repositories",
            )

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

    @tool(
        name="ha_hacs_repository_info",
        tags={"HACS"},
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "title": "Get HACS Repository Info",
        },
    )
    @log_tool_usage
    async def ha_hacs_repository_info(self, repository_id: str) -> dict[str, Any]:
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
            actual_id, _ = await _resolve_hacs_repo_id(ws_client, repository_id)

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
                self._client,
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

    @tool(
        name="ha_hacs_add_repository",
        tags={"HACS"},
        annotations={"destructiveHint": True, "title": "Add HACS Repository"},
    )
    @log_tool_usage
    async def ha_hacs_add_repository(
        self,
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
                self._client,
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

    @tool(
        name="ha_hacs_download",
        tags={"HACS"},
        annotations={
            "destructiveHint": True,
            "title": "Download/Install HACS Repository",
        },
    )
    @log_tool_usage
    async def ha_hacs_download(
        self,
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
            # Empty/whitespace repository_id would either be passed straight
            # into ``_resolve_hacs_repo_id`` (which has no empty-check and
            # would fall through to a HACS lookup miss) or — for a numeric
            # candidate — reach ``hacs/repository/download`` with an empty
            # repository field. Same destructive-WS-call class as
            # ``ha_manage_addon``: guard up-front so the caller learns the
            # identifier was unusable before any backend call.
            validate_identifier_not_empty(
                repository_id,
                "repository_id",
                suggestions=[
                    "Use ha_hacs_search() to find valid repository IDs",
                    "Or pass a GitHub path like 'owner/repo' to install by name",
                ],
            )
            # Check if HACS is available
            await _assert_hacs_available()

            from ..client.websocket_client import get_websocket_client

            ws_client = await get_websocket_client()

            # Resolve GitHub path to numeric ID if needed
            actual_id, repo_name = await _resolve_hacs_repo_id(ws_client, repository_id)

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
                self._client,
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


def register_hacs_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register HACS integration tools with the MCP server."""
    register_tool_methods(mcp, HacsTools(client))


def _filter_and_score_repos(
    all_repositories: list[dict[str, Any]],
    query: str,
    installed_only: bool | None,
) -> list[dict[str, Any]]:
    """Filter repositories and compute relevance scores."""
    query_lower = query.lower().strip()
    matches = []

    for repo in all_repositories:
        if installed_only and not repo.get("installed", False):
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
            score = 0

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

    return matches


# HACS' dispatcher signal that fires whenever a repository is
# registered, installed, or otherwise mutates. The matching
# ``HacsDispatchEvent`` enum lives in
# ``custom_components/hacs/enums.py`` in the HACS integration source —
# using the raw string keeps ha-mcp's runtime free of a hard import on
# HACS internals.
HACS_REPOSITORY_SIGNAL = "hacs_dispatch_repository"

# Wall-clock budget for ``wait_for_repo_registration``. Generous
# because the constraint is "HACS finishes registration"; under load
# on small hosts that has been observed to take >10 s. We're not
# polling on this timer (the subscription nudges us) — the budget is
# a backstop for the case where HACS never dispatches the event.
HACS_REPO_REGISTRATION_TIMEOUT = 30.0

# Backstop poll cadence inside ``wait_for_repo_registration`` —
# between dispatcher nudges we re-check ``hacs/repositories/list``
# at this cadence so we still complete if HACS' dispatch is dropped/
# lossy for any reason. Larger than the old 1.0 s because we expect
# the nudge to do the heavy lifting; this is belt-and-braces only.
HACS_REPO_BACKSTOP_POLL_INTERVAL = 5.0


async def _find_repo_in_list_by_full_name(
    ws_client: Any, full_name_lower: str
) -> dict[str, Any] | None:
    """Return the HACS repo entry matching ``full_name_lower``, or None."""
    list_response = await ws_client.send_command("hacs/repositories/list")
    for repo in list_response.get("result", []):
        if repo.get("full_name", "").lower() == full_name_lower:
            # ``ws_client`` is ``Any`` so mypy can't narrow the result
            # entry. The HACS wire shape (``custom_components/hacs/
            # websocket/repositories.py``) always emits a dict per repo,
            # so a runtime guard would be defensive only.
            return dict(repo)
    return None


async def wait_for_repo_registration(
    ws_client: Any,
    full_name: str,
    *,
    timeout: float = HACS_REPO_REGISTRATION_TIMEOUT,
    backstop_poll_interval: float = HACS_REPO_BACKSTOP_POLL_INTERVAL,
) -> dict[str, Any] | None:
    """Wait for a HACS repo to register, using HACS' dispatch signal.

    Replaces the previous fixed 10x1s blind poll of
    ``hacs/repositories/list``. HACS dispatches
    ``HacsDispatchEvent.REPOSITORY`` whenever a repository entry
    registers / installs / mutates, exposed over the WebSocket via
    ``hacs/subscribe`` with a ``signal`` field. We subscribe before
    any wait, do a single post-subscribe sample to close the race
    with the preceding ``hacs/repositories/add``, then wait on the
    subscription queue with a wall-clock backstop.

    Args:
        ws_client: Connected HA WebSocket client.
        full_name: Repository full name in ``owner/repo`` form (case-insensitive).
        timeout: Wall-clock budget before giving up.
        backstop_poll_interval: Between dispatch nudges, re-check the
            list at this cadence to recover from a missed/lossy dispatch.

    Returns the HACS repo dict if found, or ``None`` on timeout.
    """
    full_name_lower = full_name.lower()

    try:
        sub_id, queue = await ws_client.subscribe_command(
            "hacs/subscribe", signal=HACS_REPOSITORY_SIGNAL
        )
    except Exception as e:
        # Subscription path unavailable — fall back to a single
        # post-add list lookup so we still benefit from the work the
        # caller already did before invoking us.
        logger.debug(
            "hacs/subscribe failed (%s); falling back to single list lookup", e
        )
        return await _find_repo_in_list_by_full_name(ws_client, full_name_lower)

    try:
        # Race-closer: HACS may have dispatched REPOSITORY between the
        # caller's ``hacs/repositories/add`` returning and our
        # subscribe landing. A single list check after subscribe
        # catches that.
        repo = await _find_repo_in_list_by_full_name(ws_client, full_name_lower)
        if repo is not None:
            logger.info(
                f"Found {full_name} -> id={repo.get('id')} (post-subscribe sample)"
            )
            return repo

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None

            # Wait for HACS to nudge us; if it doesn't, fall through
            # to a re-check at ``backstop_poll_interval``.
            wait_for = min(remaining, backstop_poll_interval)
            try:
                event = await asyncio.wait_for(queue.get(), timeout=wait_for)
            except TimeoutError:
                event = None
            except asyncio.QueueShutDown:
                # Connection was torn down — try one last list lookup
                # in case the repo registered before the shutdown,
                # then give up.
                return await _find_repo_in_list_by_full_name(ws_client, full_name_lower)

            if event is not None:
                payload = event.get("event") or {}
                if (
                    isinstance(payload, dict)
                    and payload.get("repository", "").lower() == full_name_lower
                ):
                    # Matching repo dispatched — fetch the full entry
                    # since the event payload only carries id +
                    # full_name and callers usually need more fields.
                    repo = await _find_repo_in_list_by_full_name(
                        ws_client, full_name_lower
                    )
                    if repo is not None:
                        logger.info(
                            f"Found {full_name} -> id={repo.get('id')} "
                            "(HACS dispatch event)"
                        )
                        return repo
                # Unrelated repo's dispatch — go back to waiting.
                # Re-listing here on every dispatch is what made the
                # old polling approach expensive (the HACS list payload
                # is 2 MB+ on busy installs); the dispatcher is the
                # signal, the list is the source of truth only when
                # the dispatcher says something happened for us.
                continue

            # event is None ⇒ the backstop poll fired without a
            # dispatch. Belt-and-braces re-check the list in case
            # HACS dropped/lost a dispatch event.
            repo = await _find_repo_in_list_by_full_name(ws_client, full_name_lower)
            if repo is not None:
                logger.info(
                    f"Found {full_name} -> id={repo.get('id')} (backstop poll sample)"
                )
                return repo
    finally:
        await ws_client.unsubscribe_command(sub_id)


async def _resolve_hacs_repo_id(ws_client: Any, repository_id: str) -> tuple[str, str]:
    """Resolve a GitHub path (owner/repo) to a HACS numeric repository ID and name.

    Returns (numeric_id, display_name). If repository_id is already numeric,
    returns (repository_id, repository_id).

    For GitHub-path identifiers, this uses the HACS dispatch-signal
    waiter so that a caller running immediately after
    ``ha_hacs_add_repository`` doesn't race against HACS' internal
    registration — the same flake class that affected
    ``ha_install_mcp_tools``.
    """
    if "/" not in repository_id:
        return repository_id, repository_id

    repo = await wait_for_repo_registration(ws_client, repository_id)

    if repo is not None:
        return str(repo.get("id")), repo.get("name") or repository_id

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
    return repository_id, repository_id  # unreachable, but satisfies type checker
