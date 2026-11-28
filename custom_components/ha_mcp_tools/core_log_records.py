"""Identify record boundaries with the pattern Core uses for its raw stream."""

import logging
from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant

from .system_log_entries import integration_loggers, logger_domains


async def record_headers(hass: HomeAssistant, text: str) -> dict[str, dict[str, Any]]:
    from homeassistant.components.system_log.stream import ANSI_ESCAPE, LOG_FILE_RECORD

    levels = logging.getLevelNamesMapping()
    headers = {}
    # Split at newlines only, as the readers count lines.
    for index, raw in enumerate(text.split("\n")):
        line = ANSI_ESCAPE.sub("", raw)
        if line[10:11] == "T":
            line = line[:10] + " " + line[11:]
        if not (match := LOG_FILE_RECORD.match(line)):
            continue
        date, time, level, _ = match[0].split(" ", 3)
        if level not in levels:
            continue
        timestamp = f"{date} {time}"
        try:
            datetime.fromisoformat(timestamp)
        except ValueError:
            continue
        name = match[1]
        headers[str(index)] = {
            "timestamp": timestamp,
            "level": level,
            "logger": name,
            "message": line[match.end() :],
        }
    prefixes = await integration_loggers(
        hass, names={header["logger"] for header in headers.values()}
    )
    for header in headers.values():
        header["integrations"] = logger_domains(header["logger"], prefixes)
    return headers


async def _prepare(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    return {"headers": await record_headers(hass, msg["text"])}


def command_specs(vol: Any) -> list[tuple[dict[Any, Any], Any, Any]]:
    return [
        (
            {
                vol.Required("type"): "ha_mcp_tools/core_log_records",
                vol.Required("text"): vol.All(str, vol.Length(max=1_000_000)),
            },
            lambda hass, msg, **result: result,
            _prepare,
        )
    ]
