"""Select the component reader or the public log API for a single window."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..client.error_log_journald import JournaldPage, journald_page, log_lines
from ..client.exceptions import HomeAssistantAPIError, HomeAssistantCommandNotSent
from ..client.models import ErrorLogPage
from ..client.rest_client import MIN_LOG_WINDOW_LINES
from ..errors import ErrorCode, create_error_response
from .component_api import ask_component, component_unusable_warning, get_component_caps
from .helpers import raise_tool_error
from .supervisor_log_reader import component_journal_page, journal_fallback_warning

# Under the 1,000,000 characters ``core_log_records`` accepts in one call.
RECORDS_CHARS = 900_000


async def _record_headers(client: Any, text: str) -> dict[str, dict[str, Any]] | None:
    """Core's record headers for ``text`` by line index; ``None`` without the component."""
    lines = log_lines(text) or [""]
    headers: dict[str, dict[str, Any]] = {}
    start = 0
    while start < len(lines):
        end, size = start + 1, len(lines[start]) + 1
        while end < len(lines) and size + len(lines[end]) + 1 <= RECORDS_CHARS:
            size += len(lines[end]) + 1
            end += 1
        parsed = await ask_component(
            client, "core_log_records", text="\n".join(lines[start:end])
        )
        if parsed is None:
            return None
        headers.update(
            {str(start + int(index)): row for index, row in parsed["headers"].items()}
        )
        start = end
    return headers


async def _core_journal(
    client: Any, lines: int, offset: int | str, records: bool
) -> CoreLogWindow | None:
    async def fetch(range_header: str) -> JournaldPage:
        result = await component_journal_page(client, "core", range=range_header)
        if result is None:
            raise HomeAssistantCommandNotSent(
                "The component journal reader is unavailable."
            )
        return result

    count = lines
    while True:
        try:
            page = await journald_page(count, offset, fetch)
            break
        except HomeAssistantCommandNotSent:
            return None
        except HomeAssistantAPIError as exc:
            if count == 1 or not (exc.response_data or {}).get("byte_limited"):
                raise
            count = max(1, count // 2)
    return CoreLogWindow(
        page.text,
        page.has_more,
        page.first_cursor,
        headers=await _record_headers(client, page.text) if records else None,
        requested_lines=count,
        warning="HA-MCP's byte limit shortened this journal window. Continue paging for older entries."
        if count < lines
        else None,
    )


@dataclass(frozen=True, kw_only=True)
class CoreLogWindow(ErrorLogPage):
    """A Core log window and what its reader knows about it.

    ``headers`` maps a line's index in ``text`` to Core's parse of the record
    it starts; ``None`` means no parse was available, so lines are not grouped
    into records. ``requested_lines`` is the window's width when the reader
    changed it from what was asked.
    """

    warning: str | None = None
    disabled_reason: str | None = None
    headers: dict[str, dict[str, Any]] | None = None
    requested_lines: int | None = None


def _file_window(reply: dict[str, Any]) -> CoreLogWindow:
    if "error" in reply:
        code = (
            ErrorCode.RESOURCE_NOT_FOUND
            if reply.get("status") == 404
            else ErrorCode.SERVICE_CALL_FAILED
        )
        raise_tool_error(create_error_response(code, reply["error"]))
    warning = None
    if reply.get("byte_limited"):
        warning = "HA-MCP's byte limit shortened this window. Continue paging for older lines."
    elif reply.get("backlog_limit_reached"):
        warning = "Core's integration backlog is full. Earlier lines may exist."
    return CoreLogWindow(
        reply["text"],
        reply["has_more"],
        reply.get("first_cursor"),
        warning=warning,
        requested_lines=len(log_lines(reply["text"]))
        if reply.get("byte_limited")
        else None,
        disabled_reason=reply.get("disabled_reason"),
        headers=reply.get("headers"),
    )


async def core_log_window(
    client: Any,
    lines: int,
    offset: int | str = 0,
    integration: str | None = None,
    *,
    records: bool = False,
    plain_lines: int | None = None,
    require_records: bool = False,
) -> CoreLogWindow:
    params = {"integration": integration} if integration else {}
    reply = await ask_component(
        client, "core_log_window", lines=lines, offset=offset, records=records, **params
    )
    if reply is not None and not reply.get("journal"):
        return _file_window(reply)
    if reply and reply.get("journal"):
        native = await _core_journal(client, lines, offset, records)
        if native is not None:
            return native
    action = "the raw log was read through Home Assistant's API"
    warning = (
        await journal_fallback_warning(client, action)
        if reply
        else component_unusable_warning(await get_component_caps(client), action)
    )
    if integration or (require_records and not reply):
        config = await client.get_config()
        if reason := (config.get("logging") or {}).get("log_file_disabled_reason"):
            return CoreLogWindow("", False, warning=warning, disabled_reason=reason)
        raise_tool_error(
            create_error_response(
                ErrorCode.COMPONENT_NOT_INSTALLED,
                component_unusable_warning(
                    await get_component_caps(client),
                    "filtering raw logs by integration is unavailable"
                    if integration
                    else "structured and level-filtered reads require Core log records",
                ),
            )
        )
    requested = max(MIN_LOG_WINDOW_LINES, lines if reply else (plain_lines or lines))
    try:
        page = await client.get_error_log(lines=requested, offset=offset)
    except HomeAssistantAPIError as exc:
        if exc.status_code != 404:
            raise
        config = await client.get_config()
        if reason := (config.get("logging") or {}).get("log_file_disabled_reason"):
            return CoreLogWindow("", False, warning=warning, disabled_reason=reason)
        raise
    headers = None
    if reply and reply.get("journal") and records:
        headers = await _record_headers(client, page.text)
    return CoreLogWindow(
        page.text,
        page.has_more,
        page.first_cursor,
        warning=warning,
        headers=headers,
        requested_lines=requested,
    )
