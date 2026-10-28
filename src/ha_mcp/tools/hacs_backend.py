"""Which store API answers: Core's Marketplace (2026.11+) or the HACS integration.

Home Assistant 2026.11 migrates HACS into the built-in ``marketplace``
integration and removes the ``hacs/*`` WebSocket commands. The HACS tools
talk to whichever API the instance serves, Marketplace first.
"""

from dataclasses import dataclass, field
from typing import Any

from ..client.rest_client import HomeAssistantCommandError
from ..errors import ErrorCode, create_error_response
from .helpers import raise_tool_error

_COMMANDS = {
    "hacs": {
        "list": "hacs/repositories/list",
        "info": "hacs/repository/info",
        "install": "hacs/repository/download",
        "remove": "hacs/repository/remove",
        "refresh": "hacs/repository/refresh",
        "add": "hacs/repositories/add",
        "subscribe": "hacs/subscribe",
    },
    "marketplace": {
        "list": "marketplace/repositories/list",
        "info": "marketplace/repository/info",
        "install": "marketplace/repository/install",
        "remove": "marketplace/repository/uninstall",
        "refresh": "marketplace/repository/refresh",
        "add": "marketplace/repositories/add",
        "subscribe": "marketplace/subscribe",
    },
}
_REPOSITORY_SIGNAL = {
    "hacs": "hacs_dispatch_repository",
    "marketplace": "marketplace_repository",
}

# Categories HACS managed that the Marketplace retired (they get a repair).
RETIRED_MARKETPLACE_CATEGORIES = frozenset({"appdaemon", "python_script"})

# The Marketplace refuses these until the calling user acts in the UI; the
# message Core sends is already user-facing, the suggestions add the step.
_GATE_SUGGESTIONS = {
    "warning_not_accepted": [
        "Open Marketplace in the Home Assistant sidebar as the user this server "
        "connects as, and accept the first-run warning. The agent cannot accept "
        "it on the user's behalf.",
    ],
    "github_not_connected": [
        "Open Marketplace in the Home Assistant sidebar and connect a GitHub "
        "account (a device sign-in), then retry.",
    ],
    "github_rate_limited": [
        "GitHub's anonymous rate limit ran out; wait and retry, or connect a "
        "GitHub account in Marketplace to raise the limit.",
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


HACS_API = StoreApi("hacs")


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
                "On older versions, install HACS from https://hacs.xyz/ and "
                "restart Home Assistant",
            ],
        )
    )
    return HACS_API  # unreachable: raise_tool_error always raises


def gate_suggestions(err: HomeAssistantCommandError) -> list[str] | None:
    """The UI step for a Marketplace refusal the agent cannot clear itself."""
    return _GATE_SUGGESTIONS.get(err.code or "")
