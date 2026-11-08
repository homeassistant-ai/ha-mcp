"""Which store API answers: Core's Marketplace (2026.11+) or the HACS integration.

Home Assistant 2026.11 migrates HACS into the built-in ``marketplace``
integration and removes the ``hacs/*`` WebSocket commands. The HACS tools
talk to whichever API the instance serves, Marketplace first.
"""

import re
from dataclasses import dataclass, field
from typing import Any, NoReturn

from ..client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
)
from ..errors import ErrorCode, create_error_response
from .helpers import exception_to_structured_error, raise_tool_error

_COMMANDS = {
    "hacs": {
        "list": "hacs/repositories/list",
        "info": "hacs/repository/info",
        "install": "hacs/repository/download",
        "remove": "hacs/repository/remove",
        "refresh": "hacs/repository/refresh",
        "add": "hacs/repositories/add",
        "subscribe": "hacs/subscribe",
        "releases": "hacs/repository/releases",
    },
    "marketplace": {
        "list": "marketplace/repositories/list",
        "info": "marketplace/repository/info",
        "install": "marketplace/repository/install",
        "remove": "marketplace/repository/uninstall",
        "refresh": "marketplace/repository/refresh",
        "add": "marketplace/repositories/add",
        "subscribe": "marketplace/subscribe",
        "releases": "marketplace/repository/releases",
        # HACS has no first-run warning, so no counterpart.
        "accept_warning": "marketplace/warning/accept",
    },
}
_REPOSITORY_SIGNAL = {
    "hacs": "hacs_dispatch_repository",
    "marketplace": "marketplace_repository",
}
# HA-MCP's older name for what both stores call "plugin".
_CATEGORY_ALIASES = {"lovelace": "plugin"}
CATEGORY_HELP = (
    "One of the categories the store reports (search's store.categories); "
    "'lovelace' means 'plugin' (dashboard cards)."
)

# The Marketplace refuses these until the calling user acts; the message Core
# sends is already user-facing, the suggestions add the step.
_GATE_SUGGESTIONS = {
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
        "Open Settings > Marketplace and select Connect GitHub (a device "
        "sign-in), then retry. The agent cannot connect it on the user's behalf.",
    ],
    "github_rate_limited": [
        "GitHub's anonymous rate limit ran out; wait and retry, or connect a "
        "GitHub account in Marketplace to raise the limit.",
    ],
    "replaces_built_in": [
        "This repository replaces a built-in integration of the same domain. "
        "Ask the user to confirm, then retry with confirm_replace_built_in=True.",
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
    + ".storage/marketplace.repositories (.storage/hacs.repositories on HACS), "
    + "start Core, then update the kept record.",
]
# Core's ``integration_owned`` message: "The {domain} directory belongs to
# {owner}." The store sends no error code for it, so the whole sentence is
# matched, owner as ``owner/repo``, never a fragment of some other message.
_OWNED_DIRECTORY = re.compile(
    r"^(?:Command failed: )?The \S+ directory belongs to ([\w.-]+/[\w-][\w.-]*?)\.?$"
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


@dataclass(frozen=True)
class StoreApi:
    """The store API this Home Assistant serves and its ``info`` reply."""

    prefix: str
    info: dict[str, Any] = field(default_factory=dict)

    @property
    def is_marketplace(self) -> bool:
        return self.prefix == "marketplace"

    @property
    def name(self) -> str:
        return "Marketplace" if self.is_marketplace else "HACS"

    @property
    def repository_signal(self) -> str:
        return _REPOSITORY_SIGNAL[self.prefix]

    def command(self, operation: str) -> str:
        return _COMMANDS[self.prefix][operation]

    def native_category(self, category: str) -> str:
        """The store's name for ``category``, checked against its live list."""
        # The stores lower-case it themselves
        native = _CATEGORY_ALIASES.get(category.lower(), category.lower())
        offered = self.info.get("categories")
        if offered and native not in offered:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"{self.name} has no '{category}' category.",
                    suggestions=[f"Use one of: {', '.join(sorted(offered))}"],
                    context={"category": category, "categories": sorted(offered)},
                )
            )
        return native


HACS_API = StoreApi("hacs")
# The order to probe them in: Home Assistant 2026.11 replaced HACS.
STORE_APIS = (StoreApi("marketplace"), HACS_API)


async def _probe(ws_client: Any, command: str) -> tuple[dict[str, Any] | None, str]:
    """``(result, "")`` on success, else ``(None, error code or message)``."""
    try:
        reply = await ws_client.send_command(command)
    except HomeAssistantCommandError as err:
        return None, err.code or str(err)
    if reply.get("success") is False:
        error = reply.get("error")
        if isinstance(error, dict):
            return None, error.get("code") or error.get("message") or ""
        return None, str(error)
    return reply.get("result") or {}, ""


def _is_unknown(code: str) -> bool:
    return code == "unknown_command" or "unknown command" in code.lower()


async def detect_store_api(ws_client: Any) -> StoreApi:
    """Return the store API that answers, or raise COMPONENT_NOT_INSTALLED."""
    result, code = await _probe(ws_client, "marketplace/info")
    if result is not None:
        return StoreApi("marketplace", result)
    if not _is_unknown(code):
        raise_tool_error(
            create_error_response(
                ErrorCode.COMPONENT_NOT_INSTALLED,
                f"The Marketplace is not available ({code}).",
                suggestions=[
                    "Check Settings > Devices & services > Marketplace and the "
                    "Home Assistant log for why it failed to load",
                ],
            )
        )

    result, code = await _probe(ws_client, "hacs/info")
    if result is not None:
        return StoreApi("hacs", result)
    if not _is_unknown(code):
        raise_tool_error(
            create_error_response(
                ErrorCode.COMPONENT_NOT_INSTALLED,
                f"HACS is installed but not responding: {code}",
                suggestions=[
                    "Restart Home Assistant",
                    "Check Home Assistant logs for HACS errors",
                    "Verify HACS is up to date",
                ],
            )
        )
    raise_tool_error(
        create_error_response(
            ErrorCode.COMPONENT_NOT_INSTALLED,
            "Neither the Marketplace (Home Assistant 2026.11+) nor HACS is available.",
            suggestions=[
                "On Home Assistant 2026.11 or newer, the Marketplace is built in",
                (
                    "On older versions, install HACS from https://hacs.xyz/ "
                    "and restart Home Assistant"
                ),
            ],
        )
    )
    return HACS_API  # unreachable: raise_tool_error always raises


def is_owned_directory_refusal(err: HomeAssistantCommandError) -> bool:
    """Whether the store refused because another installed record owns the
    integration's directory."""
    return _OWNED_DIRECTORY.match(str(err).strip()) is not None


def raise_store_error(
    err: HomeAssistantCommandError,
    context: dict[str, Any],
    *,
    installed_records: list[str] | None = None,
) -> NoReturn:
    """Raise a store command failure; a Marketplace gate names the UI step.

    The gates are told apart by Core's error code: their messages ("Connect a
    GitHub account ...") would otherwise read as a connection failure. An
    ownership refusal names the owner to update, unless the owner is one of
    ha-mcp's own records and ``installed_records`` shows two of them
    installed: then neither can be updated and the suggestions are the way
    out of that. A refusal naming some other integration's owner keeps the
    owner hint whatever ha-mcp's own records look like.
    """
    suggestions = _GATE_SUGGESTIONS.get(err.code or "")
    if match := _OWNED_DIRECTORY.match(str(err).strip()):
        owners = {r.lower() for r in installed_records or []}
        if len(owners) > 1 and match[1].lower() in owners:
            suggestions = DUPLICATE_RECORD_SUGGESTIONS
            context = {**context, "installed": installed_records}
        else:
            suggestions = _owned_directory_suggestions(match[1])
    if suggestions:
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                str(err),
                suggestions=suggestions,
                context={**context, "store_error": err.code},
            )
        )
    exception_to_structured_error(err, context=context, raise_error=True)


async def accept_warning(ws_client: Any, api: StoreApi) -> dict[str, Any]:
    """Accept the Marketplace's first-run warning for the user this server is."""
    if not api.is_marketplace:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "HACS has no first-run warning to accept; only the Marketplace "
                "(Home Assistant 2026.11+) asks for one.",
                context={"action": "accept_warning", "store": api.name},
            )
        )
    accepted = {"success": True, "warning_accepted": True}
    if api.info.get("warning_accepted"):
        return {**accepted, "message": "Already accepted for this server's user."}
    command = api.command("accept_warning")
    try:
        response = await ws_client.send_command(command)
    except HomeAssistantCommandError as err:
        raise_store_error(err, {"command": command})
    if response.get("success") is False:
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                str(response.get("error") or "Operation failed"),
                context={"command": command},
            )
        )
    return {
        **accepted,
        "message": "Accepted the Marketplace's first-run warning for the user "
        "this server connects as; installs and updates now pass its gate.",
    }


async def fetch_releases(
    ws_client: Any, api: StoreApi, repository_id: str, info: dict[str, Any]
) -> dict[str, Any]:
    """The repository's GitHub releases and the notes of versions not installed.

    Release notes come from HACS's own command, or on the Marketplace from the
    repository's update entity, the way the update dialog shows them. Both
    reach GitHub, so they get the 60 s budget the store's installs get, and a
    failure or timeout becomes a warning: the repository info already arrived.
    """
    out: dict[str, Any] = {}
    warnings: list[str] = []
    try:
        reply = await ws_client.send_command(
            api.command("releases"), _wait_timeout=60.0, repository_id=repository_id
        )
        out["github_releases"] = reply.get("result") or []
    except (HomeAssistantCommandError, HomeAssistantCommandTimeout) as err:
        warnings.append(f"Releases unavailable: {err}")

    try:
        if not api.is_marketplace:
            reply = await ws_client.send_command(
                "hacs/repository/release_notes",
                _wait_timeout=60.0,
                repository=repository_id,
            )
            out["release_notes"] = reply.get("result") or []
        elif info.get("pending_upgrade") and info.get("update_entity_id"):
            reply = await ws_client.send_command(
                "update/release_notes",
                _wait_timeout=60.0,
                entity_id=info["update_entity_id"],
            )
            out["release_notes"] = reply.get("result")
    except (HomeAssistantCommandError, HomeAssistantCommandTimeout) as err:
        warnings.append(f"Release notes unavailable: {err}")
    if warnings:
        out["warnings"] = warnings
    return out
