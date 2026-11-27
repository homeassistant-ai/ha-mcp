"""Page the Core log using byte positions in Core's configured file."""

import os
from pathlib import Path
from typing import Any, BinaryIO

from homeassistant.const import KEY_DATA_LOGGING, KEY_DATA_LOGGING_DISABLED_REASON
from homeassistant.core import HomeAssistant
from homeassistant.helpers.hassio import is_hassio
from homeassistant.loader import IntegrationNotFound, async_get_integration

from .core_log_records import record_headers

_MAX_BYTES = 1_000_000


def _back(file: BinaryIO, end: int, lines: int) -> int:
    """Find the start of the last lines before end, dropping no partial line."""
    if not end or not lines:
        return end
    file.seek(end - 1)
    needed = lines + int(file.read(1) == b"\n")
    while end:
        size = min(65536, end)
        end -= size
        file.seek(end)
        block = file.read(size)
        count = block.count(b"\n")
        if count >= needed:
            return end + len(block.rsplit(b"\n", needed)[0]) + 1
        needed -= count
    return 0


def _read(path: Path, lines: int, offset: int | str) -> dict[str, Any]:
    with path.open("rb") as file:
        stat = os.fstat(file.fileno())
        if isinstance(offset, int):
            end = _back(file, stat.st_size, offset)
        else:
            try:
                prefix, anchor, skip = offset.split(":")
                kind, inode, position = anchor.split(";")
                if prefix != "cursor" or kind != "file" or int(inode) != stat.st_ino:
                    raise ValueError
                pos, forward = int(position), int(skip)
                if not 0 <= pos <= stat.st_size or not 0 <= forward <= 2000:
                    raise ValueError
            except ValueError:
                return {
                    "error": "The log file changed or the cursor is invalid. Start a new read.",
                    "status": 404,
                }
            file.seek(pos)
            for _ in range(forward):
                if len(file.readline(_MAX_BYTES + 1)) > _MAX_BYTES:
                    return {"error": "A log line exceeds HA-MCP's read limit."}
            end = file.tell()
        start = _back(file, end, lines)
        byte_limited = end - start > _MAX_BYTES
        if byte_limited:
            file.seek(end - _MAX_BYTES - 1)
            file.readline(_MAX_BYTES + 1)
            start = file.tell()
            if start >= end:
                return {"error": "A log line exceeds HA-MCP's byte limit."}
        else:
            file.seek(start)
        content = file.read(end - start).decode("utf-8", errors="replace")
    return {
        "text": content,
        "byte_limited": byte_limited,
        "has_more": start > 0,
        "first_cursor": f"file;{stat.st_ino};{start}",
    }


async def _window(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    if domain := msg.get("integration"):
        return await _integration_window(hass, domain, msg["lines"], msg["offset"])
    if path := hass.data.get(KEY_DATA_LOGGING):
        return await hass.async_add_executor_job(
            _read, Path(path), msg["lines"], msg["offset"]
        )
    if is_hassio(hass):
        return {"journal": True}
    return {
        "text": "",
        "has_more": False,
        "disabled_reason": hass.data.get(KEY_DATA_LOGGING_DISABLED_REASON),
    }


async def _integration_window(
    hass: HomeAssistant, domain: str, count: int, offset: int | str
) -> dict[str, Any]:
    from homeassistant.components.system_log.const import RAW_INTEGRATION_BACKLOG_LINES
    from homeassistant.components.system_log.stream import _async_read_backlog

    if isinstance(offset, str):
        return {"error": "Use the offset returned for this integration."}
    try:
        integration = await async_get_integration(hass, domain)
    except IntegrationNotFound as exc:
        return {"error": str(exc), "status": 404}
    loggers = (integration.pkg_path, *(integration.loggers or ()))
    backlog = await _async_read_backlog(hass, loggers)
    end = max(0, len(backlog) - offset)
    start = max(0, end - count)
    return {
        "text": "\n".join(backlog[start:end]),
        "has_more": start > 0,
        "backlog_limit_reached": len(backlog) == RAW_INTEGRATION_BACKLOG_LINES,
    }


def command_specs(vol: Any) -> list[tuple[dict[Any, Any], Any, Any]]:
    return [
        (
            {
                vol.Required("type"): "ha_mcp_tools/core_log_window",
                vol.Required("lines"): vol.All(int, vol.Range(min=1, max=2000)),
                vol.Optional("integration"): str,
                vol.Optional("records"): bool,
                vol.Optional("offset", default=0): vol.Any(
                    vol.All(int, vol.Range(min=0)), str
                ),
            },
            lambda hass, msg, **result: result,
            _prepare,
        )
    ]


async def _prepare(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    result = await _window(hass, msg)
    if msg.get("records") and "text" in result:
        result["headers"] = await record_headers(hass, result["text"])
    return result
