"""Which store API answers: Core's Marketplace (2026.11+) or the HACS integration.

Home Assistant 2026.11 migrates HACS into the built-in ``marketplace``
integration and removes the ``hacs/*`` WebSocket commands. The HACS tools
talk to whichever API the instance serves, Marketplace first.
"""

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

# The Marketplace refuses these until the calling user acts in the UI; the
# message Core sends is already user-facing, the suggestions add the step.
_GATE_SUGGESTIONS = {
    "warning_not_accepted": [
        "Open Settings > Marketplace as the user this server connects as, and "
        "accept the first-run warning. The agent cannot accept it on the user's "
        "behalf.",
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


def raise_store_error(
    err: HomeAssistantCommandError, context: dict[str, Any]
) -> NoReturn:
    """Raise a store command failure; a Marketplace gate names the UI step.

    The gates are told apart by Core's error code: their messages ("Connect a
    GitHub account ...") would otherwise read as a connection failure.
    """
    if suggestions := _GATE_SUGGESTIONS.get(err.code or ""):
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                str(err),
                suggestions=suggestions,
                context={**context, "store_error": err.code},
            )
        )
    exception_to_structured_error(err, context=context, raise_error=True)


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
