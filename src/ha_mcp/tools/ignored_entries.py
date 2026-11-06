"""HA-MCP policy for ignored config entries (``source == "ignore"``).

Core never sets an ignored entry up, so its flows can raise (#2927), while
Core's ``supports_options`` and ``supports_reconfigure`` ask only the domain's
flow handler, never ``source``. The frontend lists ignored entries apart, with
only a stop-ignoring action. The reads report neither the options nor the
reconfigure flow, and the writes refuse to start either.
"""

from typing import Any

from ..errors import ErrorCode, create_error_response
from .helpers import raise_tool_error


def normalize_ignored_entry(entry: dict[str, Any]) -> None:
    """Report an ignored entry as having no options or reconfigure flow."""
    if entry.get("source") == "ignore":
        entry["supports_options"] = False
        entry["supports_reconfigure"] = False


def refuse_ignored_entry(entry: dict[str, Any], *, action: str, entry_id: str) -> None:
    """Refuse ``action``, which starts a flow, on an ignored entry."""
    if entry.get("source") != "ignore":
        return
    raise_tool_error(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            f"Cannot {action} config entry '{entry_id}': it is ignored, so "
            "Home Assistant never set it up and starts no flow for it.",
            suggestions=[
                "Stop ignoring it in the Home Assistant UI (Settings > Devices "
                "& services, ignored integrations), then set the integration up."
            ],
            context={"entry_id": entry_id, "domain": entry.get("domain")},
        )
    )
