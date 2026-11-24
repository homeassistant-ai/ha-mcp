"""The community store tools, on Home Assistant's built-in Marketplace.

Home Assistant 2026.11 replaced HACS with the Marketplace; the tools keep
their HACS names and drive its ``marketplace/*`` WebSocket API.
"""

from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from pydantic import Field

from ha_mcp._vendor.fastmcp import Context
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool

from ..errors import ErrorCode, create_error_response
from . import hacs_github, hacs_reads, hacs_source, hacs_writes
from .coercion import JSON_STRING_COERCION, parse_string_list_param
from .hacs_backend import CATEGORY_HELP
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
    validate_identifier_not_empty,
)
from .response_helpers import add_timezone_metadata
from .tool_hints import read_only_hints, write_hints

_REPOSITORY_ID_HELP = (
    "The repository's numeric Marketplace id or its 'owner/repo' name, "
    "which is looked up in the store's list"
)

# Each ha_manage_hacs action's handler and the parameters it reads; any other
# one given is a caller mixing actions, which silently ignoring would let a
# call do something other than its arguments describe.
_MANAGE_ACTIONS: dict[
    str, tuple[Callable[..., Awaitable[dict[str, Any]]], set[str]]
] = {
    "download": (
        hacs_writes.download,
        {"repository_id", "version", "confirm_replace_built_in"},
    ),
    "remove": (hacs_writes.remove, {"repository_id"}),
    "add_repository": (hacs_writes.add_repository, {"repository", "category"}),
    "remove_repository": (hacs_writes.remove_repository, {"repository_id"}),
    "update_information": (hacs_writes.update_information, {"repository_id"}),
    "set_version": (hacs_writes.set_version, {"repository_id", "version"}),
    "set_beta": (hacs_writes.set_beta, {"repository_id", "show_beta"}),
    "ignore": (hacs_writes.ignore, {"repository_id"}),
    "clear_new": (hacs_writes.clear_new, {"repository_id", "categories"}),
    "acknowledge_critical": (hacs_writes.acknowledge_critical, {"repository"}),
    "connect_github": (hacs_github.connect_github, {"flow_id", "abort"}),
    "set_download_source": (
        hacs_source.set_download_source,
        {"config", "flow_id", "abort"},
    ),
    "accept_warning": (hacs_writes.accept_warning, {"confirm"}),
}
# The entry flows' steps go over Core's REST flow API.
_FLOW_ACTIONS = {"connect_github", "set_download_source"}


def _reject_foreign_params(action: str, supplied: dict[str, Any]) -> None:
    foreign = sorted(
        name
        for name, value in supplied.items()
        if value is not None and name not in _MANAGE_ACTIONS[action][1]
    )
    if foreign:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Parameter(s) {foreign} do not apply to action='{action}' and "
                "would be ignored — remove them or switch action.",
                context={"action": action, "foreign_parameters": foreign},
            )
        )


class HacsTools:
    """The Marketplace tools, split along the read/write boundary so the read
    tool keeps ``readOnlyHint``."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def _socket(self) -> Any:
        """The current request's connection, OAuth users included."""
        from ..client.websocket_client import get_websocket_client

        return await get_websocket_client(
            url=self._client.base_url,
            token=self._client.token,
            verify_ssl=self._client.verify_ssl,
        )

    async def _answer(self, data: dict[str, Any]) -> dict[str, Any]:
        return {"success": True, **await add_timezone_metadata(self._client, data)}

    @tool(
        name="ha_get_hacs_info",
        tags={"HACS"},
        annotations=read_only_hints("Get Marketplace Info", open_world=True),
    )
    @log_tool_usage
    async def ha_get_hacs_info(
        self,
        action: Annotated[
            Literal[
                "search", "info", "removed", "critical", "detect", "download_source"
            ],
            Field(
                description=(
                    "'search' the store's repositories, 'info' for one repository, "
                    "'removed' for repositories the catalog removed, 'critical' "
                    "for those it marks critical, 'detect' for the categories a "
                    "GitHub repository's content fits, 'download_source' for the "
                    "sources the Marketplace can download from and the one in use"
                )
            ),
        ],
        query: Annotated[
            str,
            Field(
                description=(
                    "Words that must each appear in a row (action='search': name, "
                    "full_name, description, authors, topics, category, domain, "
                    "status and versions; action='removed': repository, reason, "
                    "removal_type), case-insensitive"
                )
            ),
        ] = "",
        category: Annotated[
            str | None,
            Field(description=f"Only this category (action='search'). {CATEGORY_HELP}"),
        ] = None,
        status: Annotated[
            list[str] | str | None,
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Only rows whose store status is one of these (action='search'), "
                    "e.g. 'installed', 'pending-upgrade', 'pending-restart', 'new'; "
                    "an installed row waiting for an update or a restart has that "
                    "status, so use installed_only for every installed row"
                )
            ),
        ] = None,
        source: Annotated[
            Literal["catalog", "custom"] | None,
            Field(
                description=(
                    "Only catalog repositories or only custom ones added by link "
                    "(action='search')"
                )
            ),
        ] = None,
        installed_only: Annotated[
            bool,
            Field(description="Only installed repositories (action='search')"),
        ] = False,
        max_results: Annotated[
            int,
            Field(
                ge=1,
                le=100,
                description="Rows per page (action='search' / 'removed')",
            ),
        ] = 10,
        offset: Annotated[
            int,
            Field(ge=0, description="Rows to skip (action='search' / 'removed')"),
        ] = 0,
        repository_id: Annotated[
            str | None,
            Field(description=f"{_REPOSITORY_ID_HELP} (action='info')"),
        ] = None,
        include_releases: Annotated[
            bool,
            Field(
                description=(
                    "Add the repository's GitHub releases, and with an upgrade "
                    "pending the release notes of its update entity (action='info')"
                )
            ),
        ] = False,
        repository: Annotated[
            str | None,
            Field(description="'owner/repo' or a GitHub URL (action='detect')"),
        ] = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Get data from Home Assistant's Marketplace, what HACS became in 2026.11.

        This tool only reads; to install, update, add or forget repositories
        use ``ha_manage_hacs``. Use ``search`` to browse, filter or list
        installed repositories: it filters and pages the store's own list, in
        the order Core lists it. Use ``info`` for one
        repository's full record as the store sends it (the README is
        ``additional_info``).

        **DASHBOARD TIP:** ``action="search", installed_only=True, category="lovelace"``
        finds installed custom cards to wire into ``ha_config_set_dashboard()``.

        **Examples:**
        - Search: ha_get_hacs_info(action="search", query="mushroom", category="lovelace")
        - Updates waiting: ha_get_hacs_info(action="search", status=["pending-upgrade"])
        - Details before an update: ha_get_hacs_info(action="info", repository_id="owner/repo", include_releases=True)
        - Before adding a repository: ha_get_hacs_info(action="detect", repository="owner/repo")

        **Caveats:** ``info`` marks the repository as seen (Core clears its
        ``new`` flag) and may fetch it from GitHub. ``detect`` needs the
        accepted first-run warning and a connected GitHub account.
        ``download_source`` opens and ends the Marketplace entry's options
        flow, as the panel's "Download source" does.
        """
        try:
            socket = await self._socket()
            if action == "search":
                data = await hacs_reads.search(
                    socket,
                    query=query,
                    category=category,
                    statuses=parse_string_list_param(status, "status", allow_csv=True),
                    source=source,
                    installed_only=installed_only,
                    max_results=max_results,
                    offset=offset,
                    ctx=ctx,
                )
            elif action == "info":
                repository_id = validate_identifier_not_empty(
                    repository_id,
                    "repository_id",
                    message="repository_id is required for action='info'",
                    suggestions=["Find it with ha_get_hacs_info(action='search')"],
                )
                data = await hacs_reads.info(socket, repository_id, include_releases)
            elif action == "removed":
                data = await hacs_reads.removed(socket, query, max_results, offset)
            elif action == "critical":
                data = await hacs_reads.critical(socket)
            elif action == "download_source":
                data = await hacs_source.download_source(socket, self._client)
            else:
                repository = validate_identifier_not_empty(
                    repository,
                    "repository",
                    message="repository is required for action='detect'",
                )
                data = await hacs_reads.detect(socket, repository)
            return await self._answer(data)
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            exception_to_structured_error(
                e, context={"tool": "ha_get_hacs_info", "action": action}
            )
            return None  # unreachable: exception_to_structured_error raises

    @tool(
        name="ha_manage_hacs",
        tags={"HACS"},
        annotations=write_hints(
            "Manage Marketplace", destructive=True, idempotent=False, open_world=True
        ),
    )
    @log_tool_usage
    async def ha_manage_hacs(
        self,
        action: Annotated[
            Literal[
                "download",
                "remove",
                "add_repository",
                "remove_repository",
                "update_information",
                "set_version",
                "set_beta",
                "ignore",
                "clear_new",
                "acknowledge_critical",
                "connect_github",
                "set_download_source",
                "accept_warning",
            ],
            Field(
                description=(
                    "'download' installs or updates; 'remove' uninstalls; "
                    "'add_repository' adds a custom repository; "
                    "'remove_repository' forgets a custom one that is not "
                    "installed; 'update_information' re-reads a repository from "
                    "GitHub; 'set_version' selects the tag or branch the "
                    "Marketplace reads the repository at and installs nothing "
                    "(the default branch clears the selection, and the next "
                    "download without version resets it; to install a version "
                    "pass version to 'download'); 'set_beta' shows or hides "
                    "prereleases; 'ignore' hides a repository's removal notice "
                    "(the store has no un-ignore); 'clear_new' clears the new "
                    "flag; 'acknowledge_critical' confirms a critical removal "
                    "was seen; 'connect_github' runs the GitHub sign-in one "
                    "step per call; 'set_download_source' picks where the "
                    "Marketplace downloads from, one step per call; "
                    "'accept_warning' accepts the first-run warning for the "
                    "user this server connects as"
                )
            ),
        ],
        repository_id: Annotated[
            str | None,
            Field(
                description=(
                    f"{_REPOSITORY_ID_HELP} (action='download', 'remove', "
                    "'remove_repository', 'update_information', 'set_version', "
                    "'set_beta', 'ignore'; 'clear_new' instead of categories)"
                )
            ),
        ] = None,
        version: Annotated[
            str | None,
            Field(
                description=(
                    "A release tag or branch (action='download', default the "
                    "latest; action='set_version')"
                )
            ),
        ] = None,
        repository: Annotated[
            str | None,
            Field(
                description=(
                    "'owner/repo' or a GitHub URL (action='add_repository'); the "
                    "full name of a critical repository (action='acknowledge_critical')"
                )
            ),
        ] = None,
        category: Annotated[
            str | None,
            Field(
                description=(
                    f"action='add_repository'; without it the Marketplace "
                    f"detects it. {CATEGORY_HELP}"
                )
            ),
        ] = None,
        categories: Annotated[
            list[str] | str | None,
            JSON_STRING_COERCION,
            Field(description="Whole categories to clear (action='clear_new')"),
        ] = None,
        show_beta: Annotated[
            bool | None,
            Field(description="True shows prereleases (action='set_beta')"),
        ] = None,
        confirm_replace_built_in: Annotated[
            bool | None,
            Field(
                description=(
                    "action='download': install a repository that replaces the "
                    "built-in integration of the same domain. Set it only after "
                    "the user agrees."
                )
            ),
        ] = None,
        confirm: Annotated[
            bool | None,
            Field(
                description=(
                    "action='accept_warning': True once the user has said they "
                    "read the Marketplace's first-run warning and accept it"
                )
            ),
        ] = None,
        config: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                description=(
                    "action='set_download_source': the answer to the form, "
                    "sent to Home Assistant as given, e.g. {'profile': "
                    "'<one of its options>'}. A mirror is run by a third party "
                    "(Home Assistant's text for each option names who): the "
                    "user picks one, not you."
                )
            ),
        ] = None,
        flow_id: Annotated[
            str | None,
            Field(
                description=(
                    "action='connect_github' / 'set_download_source': the flow "
                    "a previous call opened; without it a new one opens"
                )
            ),
        ] = None,
        abort: Annotated[
            bool | None,
            Field(
                description=(
                    "action='connect_github' / 'set_download_source': True "
                    "ends flow_id's flow"
                )
            ),
        ] = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Manage Home Assistant's Marketplace (what HACS became in 2026.11): install, update, remove and add repositories.

        This tool writes; to search the store or read a repository use
        ``ha_get_hacs_info``. Use ``update_information`` to surface a
        just-published release: on its own the Marketplace re-reads an
        installed custom repository only every 48 hours, and only with a
        connected GitHub account; catalog repositories follow the catalog,
        which it refreshes every 6 hours without one.

        **Examples:**
        - Install the latest: ha_manage_hacs(action="download", repository_id="441028036")
        - Install a version: ha_manage_hacs(action="download", repository_id="piitaya/lovelace-mushroom", version="v4.0.0")
        - Add a custom repository: ha_manage_hacs(action="add_repository", repository="owner/repo")
        - Read a repository at another version, installing nothing: ha_manage_hacs(action="set_version", repository_id="owner/repo", version="v1.2.0")
        - Accept the warning: ha_manage_hacs(action="accept_warning", confirm=True)
        - Connect GitHub: ha_manage_hacs(action="connect_github"), then again with
          the flow_id it returns for the URL and code the user enters on GitHub,
          then again with flow_id once they did, for the outcome
        - Change the download source: ha_manage_hacs(action="set_download_source")
          returns the form and flow_id; once the user chose, call again with
          flow_id and config={"profile": "<their choice>"}

        **Caveats:** An installed integration loads after a Home Assistant
        restart, and a new dashboard card after a browser cache clear. The
        Marketplace refuses to uninstall an integration while it has config
        entries. Installs, version and beta changes, and adding or detecting
        a repository wait for the first-run warning, which Home Assistant
        keeps per user, so the user's own acceptance in the UI does not cover
        this server: ask whether they read it (Settings > Marketplace) before
        ``accept_warning``. Adding and detecting a repository also need a
        connected GitHub account. A saved download source reloads the
        Marketplace, and the call waits for it.
        """
        supplied = {
            "repository_id": repository_id,
            "version": version,
            "repository": repository,
            "category": category,
            "categories": categories,
            "show_beta": show_beta,
            "confirm_replace_built_in": confirm_replace_built_in,
            "confirm": confirm,
            "config": config,
            "flow_id": flow_id,
            "abort": abort,
        }
        try:
            _reject_foreign_params(action, supplied)
            handler, parameters = _MANAGE_ACTIONS[action]
            arguments: dict[str, Any] = {name: supplied[name] for name in parameters}
            if action in _FLOW_ACTIONS:
                arguments["client"] = self._client
            elif action == "download":
                arguments["ctx"] = ctx
            data = await handler(await self._socket(), **arguments)
            return await self._answer(data)
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            exception_to_structured_error(
                e, context={"tool": "ha_manage_hacs", "action": action}
            )
            return None  # unreachable: exception_to_structured_error raises


def register_hacs_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register the Marketplace tools with the MCP server."""
    register_tool_methods(mcp, HacsTools(client))
