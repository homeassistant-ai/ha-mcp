"""Read bounded journal ranges over Core's own Supervisor connection."""

import re
from contextlib import aclosing
from importlib import import_module
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.hassio import is_hassio


async def _open(client: Any, source: str, options: Any, msg: dict[str, Any]) -> Any:
    boot = msg.get("boot")
    latest = msg.get("latest", False)
    if source == "host":
        if latest:
            raise ValueError("The host journal has no latest container start.")
        return await client.host.logs(
            options, identifier=msg.get("identifier"), boot=boot
        )
    if msg.get("identifier"):
        raise ValueError("Only the host journal supports an identifier.")
    if source.startswith("app:"):
        return await client.addons.addon_logs(
            source[4:], options, boot=boot, latest=latest
        )
    if source == "core":
        return await client.homeassistant.logs(options, boot=boot, latest=latest)
    if source == "supervisor":
        return await client.supervisor.logs(options, boot=boot, latest=latest)
    return await client.plugins.logs(source, options, boot=boot, latest=latest)


async def _read(client: Any, msg: dict[str, Any]) -> dict[str, Any]:
    models = import_module("aiohasupervisor.models")

    if msg.get("listing") == "boots":
        return {"boots": await client.host.log_boots()}
    if msg.get("listing") == "identifiers":
        return {"identifiers": await client.host.log_identifiers()}
    options = models.LogsOptions(
        lines=msg.get("lines"),
        range=msg.get("range"),
        no_colors=True,
        format=models.LogFormat(msg["format"]) if msg.get("format") else None,
    )
    stream = await _open(client, msg["source"], options, msg)
    async with aclosing(stream):
        # aiohasupervisor 0.11 exposes lines publicly, but the journal cursor
        # needed to resume a page is only on its underlying HTTP response.
        cursor = stream._response.headers.get("X-First-Cursor")
        lines = []
        size = 0
        async for line in stream:
            size += len(line.encode()) + 1
            if size > 1_000_000:
                return {
                    "error": "The journal window exceeds HA-MCP's byte limit. Narrow the window.",
                    "byte_limited": True,
                }
            lines.append(line)
    # The iterator removes each newline. Reattach it so the readers' newline split also
    # preserves an empty final journal entry.
    return {"text": "".join(f"{line}\n" for line in lines), "first_cursor": cursor}


async def _prepare(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    if not is_hassio(hass):
        return {"unavailable": True}
    from aiohasupervisor.exceptions import (
        SupervisorAuthenticationError,
        SupervisorBadRequestError,
        SupervisorError,
        SupervisorForbiddenError,
        SupervisorNotFoundError,
        SupervisorServiceUnavailableError,
    )
    from homeassistant.components.hassio import get_supervisor_client

    client = get_supervisor_client(hass)
    # Core 2026.11 fallback: remove when the minimum Home Assistant is 2026.12.
    if not hasattr(client.homeassistant, "logs"):
        return {"unavailable": True}
    try:
        return await _read(client, msg)
    except ValueError as exc:
        return {"error": str(exc), "status": 400}
    except SupervisorError as exc:
        codes = {
            SupervisorAuthenticationError: 401,
            SupervisorBadRequestError: 400,
            SupervisorForbiddenError: 403,
            SupervisorNotFoundError: 404,
            SupervisorServiceUnavailableError: 503,
        }
        status = next(
            (value for cls, value in codes.items() if isinstance(exc, cls)), None
        )
        # Supervisor's log routes answer in plain text, which leaves the error empty.
        source = msg.get("source", "host")
        text = str(exc) or (
            f"Supervisor answered {status} for the {source} logs."
            if status
            else f"Supervisor could not serve the {source} logs. Its own log "
            "(source system_service, slug supervisor) holds the reason."
        )
        return {"error": text, "status": status}


def command_specs(vol: Any) -> list[tuple[dict[Any, Any], Any, Any]]:
    segment = r"[a-zA-Z0-9_][a-zA-Z0-9_.-]*"
    return [
        (
            {
                vol.Required("type"): "ha_mcp_tools/supervisor_log",
                vol.Required("source"): vol.Match(
                    re.compile(rf"^(?:app:)?{segment}\Z")
                ),
                vol.Optional("boot"): vol.Any(
                    int, vol.Match(r"^(?:-?[0-9]+|[a-fA-F0-9]{32})\Z")
                ),
                vol.Optional("identifier"): vol.All(str, vol.Length(min=1, max=255)),
                vol.Optional("latest"): bool,
                vol.Optional("format"): vol.In(["plain", "verbose"]),
                vol.Optional("listing"): vol.In(["boots", "identifiers"]),
                vol.Optional("lines"): vol.All(int, vol.Range(min=1, max=10000)),
                vol.Optional("range"): vol.All(
                    str,
                    vol.Length(max=2048),
                    vol.Match(r"^(?:entries|realtime)=[\w;=.:-]+\Z"),
                ),
            },
            lambda hass, msg, **result: result,
            _prepare,
        )
    ]
