"""Core's Marketplace WebSocket API, as the store tools send it.

Home Assistant 2026.11 builds the community store in as the ``marketplace``
integration (what HACS became). Every command goes to its ``marketplace/*``
WebSocket API; a refusal reaches the caller with Core's message, code and
translation fields, and HA-MCP adds only suggestions on top.
"""

from collections.abc import Awaitable, Callable
from typing import Any, NoReturn

from ..client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
)
from ..errors import ErrorCode, create_error_response
from .helpers import exception_to_structured_error, raise_tool_error

LIST = "marketplace/repositories/list"

# The commands that reach GitHub before they answer; a slow GitHub past the
# client's 30 s default would report a false failure.
GITHUB_COMMAND_TIMEOUT = 60.0

# HA-MCP's older name for what the Marketplace calls "plugin".
_CATEGORY_ALIASES = {"lovelace": "plugin"}
CATEGORY_HELP = (
    "One of the Marketplace's categories (search's store.categories); "
    "'lovelace' means 'plugin' (dashboard cards)."
)

_ERROR_CODES = {
    "repository_not_found": ErrorCode.RESOURCE_NOT_FOUND,
    "not_loaded": ErrorCode.COMPONENT_NOT_INSTALLED,
    "unknown_command": ErrorCode.COMPONENT_NOT_INSTALLED,
}

_NOT_AVAILABLE = [
    "The Marketplace comes with Home Assistant 2026.11 and newer: update Home "
    "Assistant, or check Settings > Devices & services > Marketplace and the "
    "Home Assistant log for why it is not loaded.",
]

# Core's message is already user-facing; each suggestion adds the next step.
_SUGGESTIONS = {
    "not_loaded": _NOT_AVAILABLE,
    "unknown_command": _NOT_AVAILABLE,
    # Core keeps the acceptance per user, and the server's user is often one
    # nobody logs in as (Supervisor for the app), so the UI cannot reach it.
    "warning_not_accepted": [
        "The Marketplace's first-run warning is accepted per user, and the user "
        "this server connects as has not accepted it (an acceptance in the UI "
        "under another account does not count). Ask the user whether they have "
        "read the warning in Settings > Marketplace and accept it; if they do, "
        "call ha_manage_hacs(action='accept_warning', confirm=True) and retry.",
    ],
    "github_not_connected": [
        "Connect a GitHub account: ha_manage_hacs(action='connect_github') "
        "runs the sign-in one step per call and returns the code the user "
        "enters on GitHub. Retry afterwards.",
    ],
    "github_rate_limited": [
        "GitHub's anonymous rate limit ran out; wait and retry, or connect a "
        "GitHub account (ha_manage_hacs(action='connect_github')) to raise it.",
    ],
    "replaces_built_in": [
        "This repository replaces a built-in integration of the same domain. "
        "Ask the user to confirm, then retry with confirm_replace_built_in=True.",
    ],
    "repository_in_use": [
        "Its config entries must go first: list them with "
        "ha_get_integration(domain=...), and once the user agrees delete them "
        "with ha_remove_helpers_integrations, then retry.",
    ],
    "repository_not_installed": [
        "ha_get_hacs_info(action='search', installed_only=True) lists the "
        "installed repositories.",
    ],
    "repository_installed": [
        "Uninstall it first with ha_manage_hacs(action='remove'), then retry.",
    ],
    "repository_busy": [
        "Read its state with ha_get_hacs_info(action='info') and retry once "
        "the install has finished.",
    ],
    "repository_exists": [
        "It is listed already: find its id with "
        "ha_get_hacs_info(action='search', query='<owner/repo>').",
    ],
    "app_repository": [
        "An app (add-on) repository is registered with "
        "ha_manage_app(action='add_repository', repository='<URL>').",
    ],
    "github_failed": [
        "The message carries GitHub's answer. ha_get_hacs_info(action='search') "
        + "shows the Marketplace's state in its store block.",
        "ha_get_system_health reports under marketplace whether GitHub and "
        + "the catalog are reachable and which download source is in use; "
        + "ha_get_hacs_info(action='download_source') shows the sources to "
        + "choose from and ha_manage_hacs(action='set_download_source') "
        + "switches it.",
    ],
    # Core gives these two one code (invalid_format); its translation key
    # tells them apart.
    "invalid_repository": [
        "repository takes 'owner/repo' or the repository's GitHub URL.",
    ],
    "invalid_category": [
        "ha_get_hacs_info(action='detect', repository=...) names the "
        "categories a repository fits; search's store.categories lists the "
        "Marketplace's.",
    ],
}

# For a refusal nothing above names: what to read before acting on it.
_DEFAULT_SUGGESTIONS = {
    ErrorCode.RESOURCE_NOT_FOUND: [
        "Find the repository's id with ha_get_hacs_info(action='search')",
    ],
    ErrorCode.COMPONENT_NOT_INSTALLED: _NOT_AVAILABLE,
    ErrorCode.SERVICE_CALL_FAILED: [
        "The message is Core's own reason. ha_get_hacs_info(action='search') "
        "shows the Marketplace's state in its store block.",
    ],
}

# Two store records claiming custom_components/ha_mcp_tools (#2993): the
# Marketplace refuses to update either, and refuses to uninstall a record while
# any HA-MCP config entry exists, disabled ones included. Both ways out were
# checked on a live instance; neither can be driven through this server.
DUPLICATE_RECORD_SUGGESTIONS = [
    "A current HA-MCP integration files a repair for this under Settings > "
    + "System > Repairs; its fix keeps homeassistant-ai/ha-mcp and marks the "
    + "other record as not installed, with no change to entries or files. Ask "
    + "the user to run it.",
    "Without that repair: the store refuses to uninstall a record while any "
    + "HA-MCP entry exists, disabled ones included, and this server cannot "
    + "drive either route below. Ask the user before suggesting them.",
    "Route 1 (no terminal): delete every HA-MCP entry under Settings > Devices "
    + "& services, uninstall the duplicate record in the store, update the kept "
    + "record, add the entries back and restart. An embedded server entry gets "
    + "a new connect URL, so MCP clients reconnect.",
    "Route 2 (keeps the entries): with Home Assistant Core stopped, set "
    + '"installed": false on the duplicate record in '
    + ".storage/marketplace.repositories, start Core, then update the kept "
    + "record.",
]

type RefusalRecords = Callable[[HomeAssistantCommandError], Awaitable[list[str] | None]]


def native_category(category: str) -> str:
    """The Marketplace's name for ``category``; Core judges whether it has it."""
    lowered = category.strip().lower()
    return _CATEGORY_ALIASES.get(lowered, lowered)


def store_state_warning(store: dict[str, Any]) -> str | None:
    """A warning when the Marketplace (``marketplace/info``) is disabled or
    not yet running: its list may then lack repositories it has."""
    if reason := store.get("disabled_reason"):
        return (
            f"The Marketplace is disabled ({reason}), so its repository list may "
            "be incomplete; ha_get_system_health reports under marketplace why."
        )
    if store.get("startup") or store.get("stage") != "running":
        return (
            f"The Marketplace is not running yet (stage {store.get('stage')!r}), "
            "so its repository list may be incomplete; search again once it is."
        )
    return None


def missing_category_warning(category: str, store: dict[str, Any]) -> str | None:
    """A warning when the Marketplace (``marketplace/info``) has no such
    category; Core answers one it lacks as if nothing matched."""
    offered = store.get("categories") or []
    if native_category(category) in offered:
        return None
    return f"The Marketplace has no '{category}' category; it has: " + ", ".join(
        offered
    )


def _owned_directory_suggestions(owner: str) -> list[str]:
    """The store refused an install because another installed record owns the
    integration's directory: the owner is the record to install or update."""
    return [
        f"{owner} already provides this integration. Update or reinstall it "
        f'there: ha_manage_hacs(action="download", repository_id="{owner}").',
        "To switch repositories instead, uninstall the owner first; the store "
        "refuses that while any of the integration's config entries exist, so "
        "ask the user before deleting entries under Settings > Devices & "
        "services.",
    ]


def _directory_owner(err: HomeAssistantCommandError) -> str | None:
    """The installed record that owns the integration's directory, when the
    Marketplace refused for that (``integration_owned``)."""
    if err.translation_key != "integration_owned":
        return None
    owner = (err.translation_placeholders or {}).get("owner")
    return owner if isinstance(owner, str) else None


def is_owned_directory_refusal(err: HomeAssistantCommandError) -> bool:
    return _directory_owner(err) is not None


def raise_store_error(
    err: HomeAssistantCommandError,
    context: dict[str, Any],
    *,
    installed_records: list[str] | None = None,
) -> NoReturn:
    """Raise a Marketplace refusal with Core's message, code and translation.

    An ownership refusal names the owner to update, unless the owner is one of
    ha-mcp's own records and ``installed_records`` shows two of them installed:
    then neither can be updated and the suggestions are the way out of that.
    """
    code = _ERROR_CODES.get(err.code or "", ErrorCode.SERVICE_CALL_FAILED)
    suggestions = (
        _SUGGESTIONS.get(err.translation_key or "")
        or _SUGGESTIONS.get(err.code or "")
        or _DEFAULT_SUGGESTIONS[code]
    )
    if owner := _directory_owner(err):
        owners = {r.lower() for r in installed_records or []}
        if len(owners) > 1 and owner.lower() in owners:
            suggestions = DUPLICATE_RECORD_SUGGESTIONS
            context = {**context, "installed": installed_records}
        else:
            suggestions = _owned_directory_suggestions(owner)
    # Core's fields under the names the shared refusal translation gives them.
    native = err.refusal_fields()
    native["ha_error_code"] = native.pop("error_code")
    raise_tool_error(
        create_error_response(
            code, str(err), suggestions=suggestions, context={**context, **native}
        )
    )


async def store_command(
    ws_client: Any,
    command: str,
    context: dict[str, Any],
    *,
    timeout: float | None = None,
    on_timeout: list[str] | None = None,
    refusal_records: RefusalRecords | None = None,
    **fields: Any,
) -> Any:
    """Send one ``marketplace/*`` command and return Core's ``result``.

    A command given its own ``timeout`` reports that budget when it runs out,
    with ``on_timeout`` as the suggestions.
    """
    context = {"command": command, **context}
    if timeout is not None:
        fields["_wait_timeout"] = timeout
    try:
        reply = await ws_client.send_command(command, **fields)
    except HomeAssistantCommandError as err:
        records = await refusal_records(err) if refusal_records else None
        raise_store_error(err, context, installed_records=records)
    except HomeAssistantCommandTimeout as err:
        if timeout is None:
            raise
        exception_to_structured_error(
            err,
            context={**context, "timeout_seconds": timeout},
            suggestions=on_timeout,
            raise_error=True,
        )
    return reply.get("result")


async def marketplace_entry(ws_client: Any, context: dict[str, Any]) -> dict[str, Any]:
    """Core's config entry of the Marketplace."""
    reply = await ws_client.send_command("config_entries/get", domain="marketplace")
    entries = reply.get("result") or []
    if not entries:
        raise_tool_error(
            create_error_response(
                ErrorCode.COMPONENT_NOT_INSTALLED,
                "Home Assistant has no Marketplace entry.",
                suggestions=_NOT_AVAILABLE,
                context=context,
            )
        )
    entry: dict[str, Any] = entries[0]
    return entry


async def resolve_repository(
    ws_client: Any, repository_id: str, context: dict[str, Any]
) -> tuple[str, dict[str, Any] | None]:
    """The Marketplace's id for ``repository_id`` and, for a name, its row.

    Core takes only the numeric id, so ``owner/repo`` is looked up in one
    ``repositories/list`` (case-insensitive exact ``full_name``).
    """
    wanted = repository_id.strip()
    if wanted.isdigit():
        return wanted, None
    rows = await store_command(ws_client, LIST, context)
    for row in rows or []:
        if str(row.get("full_name") or "").lower() == wanted.lower():
            return str(row["id"]), row
    store = await store_command(ws_client, "marketplace/info", context) or {}
    incomplete = store_state_warning(store)
    raise_tool_error(
        create_error_response(
            ErrorCode.RESOURCE_NOT_FOUND,
            f"Repository '{wanted}' not found in the Marketplace's list."
            + (f" {incomplete}" if incomplete else ""),
            suggestions=[
                "Find it with ha_get_hacs_info(action='search', query=...)",
                "Retry once the Marketplace is running; a catalog repository "
                + "is listed then without being added"
                if incomplete
                else "A repository outside the catalog is added first: "
                + "ha_manage_hacs(action='add_repository', repository='owner/repo')",
            ],
            context={**context, "repository_id": repository_id},
        )
    )
