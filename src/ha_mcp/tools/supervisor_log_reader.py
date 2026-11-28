"""Journal reads through the component's Supervisor client, and their fallback.

The component reads Supervisor's journals over Core's own client, which Core
has from 2026.12. Before that, and without the component, the reads go to
Supervisor's API directly or through Core's proxy.
"""

import json
import re
from typing import Any, NoReturn
from urllib.parse import quote

from .._version import is_running_in_addon
from ..client.error_log_journald import JournaldPage, log_lines, range_header
from ..client.exceptions import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantError,
)
from ..client.journal_window import fetch_range
from ..client.rest_client import MIN_LOG_WINDOW_LINES
from ..errors import ErrorCode, create_error_response
from .component_api import (
    COMPONENT_VIA_MARKETPLACE,
    ask_component,
    component_supports,
    component_unusable_warning,
    get_component_caps,
)
from .helpers import exception_to_structured_error, raise_tool_error
from .log_common import (
    DEFAULT_LOG_LIMIT,
    IDENTIFIER_PATTERN,
    SLUG_PATTERN,
    SUPERVISOR_SEARCH_WINDOW_LINES,
    _addon_auth_error_suggestions,
    _coerce_limit,
    _validate_log_slug,
)

# The component's timeline command takes each source's text up to 1,000,000
# characters, and Core closes a WebSocket on a message over 4 MiB.
_SOURCE_TEXT_CHARS = 1_000_000
_TIMELINE_MESSAGE_BYTES = 3_000_000
_RANGE_TAIL = re.compile(r"(?P<head>.*):(?P<skip>-?\d+):(?P<count>\d+)")


def journal_name(path: str) -> str:
    """The component's name for a Supervisor log path (``addons/x`` is ``app:x``)."""
    return path.replace("addons/", "app:", 1) if path.startswith("addons/") else path


def _journal_path(name: str) -> str:
    return name.replace("app:", "addons/", 1) if name.startswith("app:") else name


async def journal_fallback_warning(client: Any, action: str) -> str:
    caps = await get_component_caps(client)
    if component_supports(caps, "supervisor_log"):
        return (
            f"The native journal read was unavailable, so {action}. It needs "
            "Home Assistant 2026.12 or newer: update Home Assistant. If it is "
            f"already on 2026.12 or newer, install component updates {COMPONENT_VIA_MARKETPLACE}"
        )
    return component_unusable_warning(caps, action)


async def proxy_refused_path(client: Any) -> bool:
    """Whether a 401 for a log path means Core's Supervisor proxy does not serve it.

    The proxy answers 401 for a path outside its list, exactly as for a
    rejected token; the token still reading Supervisor's own log through it
    tells them apart.
    """
    if is_running_in_addon():
        return False
    try:
        await fetch_range(client, "supervisor", range_header(1))
    except HomeAssistantError:
        return False
    return True


def _byte_limited(exc: HomeAssistantAPIError) -> bool:
    return bool((exc.response_data or {}).get("byte_limited"))


def _fit(text: str, budget: int) -> tuple[str, bool]:
    """``text`` cut at a line end to what one source may send, and whether it was cut.

    The budget is in bytes of the JSON the WebSocket client sends, which
    escapes every non-ASCII character.
    """
    cut = False
    while len(text) > _SOURCE_TEXT_CHARS or len(json.dumps(text)) > budget:
        keep = min(len(text), _SOURCE_TEXT_CHARS) * 3 // 4
        text = text[: text.rfind("\n", 0, keep) + 1] or text[:keep]
        cut = True
    return text, cut


async def _proxied(client: Any, request: dict[str, str]) -> JournaldPage:
    return await fetch_range(
        client,
        _journal_path(request["source"]),
        request["range"],
        request.get("format"),
    )


async def _proxy_texts(
    client: Any, requests: dict[str, list[dict[str, str]]], alone: bool
) -> tuple[dict[str, str], list[str], dict[str, str]]:
    """Each timeline source's text, the sources cut to size, and the ones that failed.

    ``alone`` says no other source is left to return when all of these fail.
    """
    budget = _TIMELINE_MESSAGE_BYTES // len(requests)
    texts: dict[str, str] = {}
    limited: list[str] = []
    errors: dict[str, str] = {}
    failure: HomeAssistantAPIError | None = None
    for name, (request,) in requests.items():
        try:
            text = (await _proxied(client, request)).text
        except HomeAssistantAuthError:
            if not await proxy_refused_path(client):
                raise
            errors[name] = "Home Assistant serves no logs for this source."
            continue
        except HomeAssistantAPIError as exc:
            errors[name], failure = str(exc), exc
            continue
        texts[name], cut = _fit(text, budget)
        if cut:
            limited.append(name)
    if alone and failure and not texts:
        raise failure
    if alone and not texts:
        raise_tool_error(
            create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND,
                "; ".join(f"{name}: {text}" for name, text in errors.items()),
            )
        )
    return texts, limited, errors


async def _proxy_window(
    client: Any,
    sources: list[str],
    args: dict[str, Any],
    requests: dict[str, list[dict[str, str]]],
) -> dict[str, Any]:
    """Make the reads the component could not, through Supervisor's API.

    The component then merges them with the sources it reads itself: Core's
    log file, where an install writes one beside the journal.
    """
    warning = await journal_fallback_warning(
        client, "the exact journal window uses the Supervisor API"
    )
    if args["raw"]:
        ((page_read, probe_read),) = requests.values()
        page = await _proxied(client, page_read)
        probe = await _proxied(client, probe_read)
        search = args["search"].casefold()
        lines = [line for line in log_lines(page.text) if search in line.casefold()]
        return {
            "log": "\n".join(lines),
            "returned_lines": len(lines),
            "has_more": bool(probe.first_cursor or probe.text),
            "warnings": [warning],
        }
    served = [name for name in sources if name not in requests]
    texts, limited, errors = await _proxy_texts(client, requests, not served)
    parsed = await ask_component(
        client,
        "log_time_window",
        **args,
        sources=[name for name in sources if name in texts or name in served],
        journal_text=texts,
        journal_limited=limited,
    )
    if parsed is None:
        _raise_component_required(await get_component_caps(client))
    if errors:
        parsed["errors"] = {**parsed.get("errors", {}), **errors}
    return {**parsed, "warnings": [warning, *parsed.get("warnings", [])]}


def _raise_component_required(caps: Any) -> NoReturn:
    raise_tool_error(
        create_error_response(
            ErrorCode.COMPONENT_NOT_INSTALLED,
            component_unusable_warning(caps, "exact log windows are unavailable"),
        )
    )


def _window_sources(
    source: str, slug: str | None, sources: list[str] | None
) -> list[str]:
    """The journal names an exact window reads: the timeline's, or the raw source's."""
    if sources is None:
        if source not in {"error_log", "supervisor", "system_service"}:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "A timed raw read takes source error_log, supervisor or "
                    "system_service. A Logbook window takes hours_back with end_time.",
                )
            )
        _validate_log_slug(source, slug)
        sources = [
            "core"
            if source == "error_log"
            else f"app:{slug}"
            if source == "supervisor"
            else str(slug)
        ]
    if (
        not sources
        or len(sources) > 12
        or any(not re.fullmatch(f"(?:app:)?{SLUG_PATTERN}", name) for name in sources)
    ):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "Supply one to twelve journal source names.",
            )
        )
    return sources


async def _window_reply(
    client: Any, sources: list[str], args: dict[str, Any]
) -> dict[str, Any]:
    try:
        reply = await ask_component(client, "log_time_window", sources=sources, **args)
        if reply is None:
            _raise_component_required(await get_component_caps(client))
        if reply.get("unavailable"):
            reply = await _proxy_window(client, sources, args, reply["requests"])
    except HomeAssistantAuthError as exc:
        if await proxy_refused_path(client):
            raise_tool_error(
                create_error_response(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    f"Home Assistant serves no logs for {', '.join(sources)}.",
                    context={"sources": sources},
                )
            )
        exception_to_structured_error(
            exc,
            context={"sources": sources},
            suggestions=_addon_auth_error_suggestions(),
        )
    except (HomeAssistantError, TimeoutError, OSError) as exc:
        exception_to_structured_error(exc, context={"sources": sources})
    return reply


async def exact_log_window(
    client: Any,
    source: str,
    slug: str | None,
    sources: list[str] | None,
    start: str | None,
    end: str | None,
    limit: int | None,
    offset: int | str,
    search: str | None,
    order: str | None,
) -> dict[str, Any]:
    """An exact raw window of one source, or a merged timeline of several."""
    raw = sources is None
    if (
        not start
        or not end
        or not isinstance(offset, int)
        or offset < 0
        or (not raw and offset)
    ):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "Supply start_time and end_time with UTC offsets. Raw reads accept a non-negative offset; continue a timeline with next_start_time.",
            )
        )
    sources = _window_sources(source, slug, sources)
    effective = _coerce_limit(limit, default=DEFAULT_LOG_LIMIT)
    reply = await _window_reply(
        client,
        sources,
        {
            "start_time": start,
            "end_time": end,
            "raw": raw,
            "limit": effective,
            "offset": offset,
            "search": search or "",
        },
    )
    if "error" in reply:
        if status := reply.get("status"):
            exception_to_structured_error(
                HomeAssistantAPIError(reply["error"], status),
                context={"sources": sources, "status": status},
            )
        raise_tool_error(
            create_error_response(ErrorCode.SERVICE_CALL_FAILED, reply["error"])
        )
    if raw and reply["has_more"]:
        reply.setdefault("next_offset", offset + effective)
    if order == "newest":
        reply.setdefault("warnings", []).append(
            "Exact windows are served oldest first."
        )
    return {
        "success": True,
        "source": source if raw else "timeline",
        "sources": sources,
        "start_time": start,
        "end_time": end,
        **reply,
    }


async def component_journal(
    client: Any, source: str, **params: Any
) -> dict[str, Any] | None:
    result = await ask_component(client, "supervisor_log", source=source, **params)
    if result is None or result.get("unavailable"):
        return None
    if "error" in result:
        raise HomeAssistantAPIError(result["error"], result.get("status"), result)
    return result


async def component_journal_page(
    client: Any, source: str, *, whole: bool = False, **params: Any
) -> JournaldPage | None:
    """One journal read through the component, ``None`` when it cannot make it.

    The component refuses a read over its byte limit. With ``whole``, such a
    range is read in two halves, the second counted from the first's cursor.
    """
    try:
        result = await component_journal(client, source, **params)
    except HomeAssistantAPIError as exc:
        parts = _RANGE_TAIL.fullmatch(params.get("range") or "")
        if not (whole and parts and _byte_limited(exc) and int(parts["count"]) > 1):
            raise
        skip, count = int(parts["skip"]), int(parts["count"])
        half = count // 2
        first = await component_journal_page(
            client,
            source,
            whole=True,
            **{**params, "range": f"{parts['head']}:{skip}:{half}"},
        )
        if first is None or not first.first_cursor:
            return first
        rest = await component_journal_page(
            client,
            source,
            whole=True,
            **{
                **params,
                "range": f"entries={first.first_cursor}:{half}:{count - half}",
            },
        )
        if rest is None:
            return None
        return JournaldPage(first.text + rest.text, first.first_cursor)
    if result is None:
        return None
    return JournaldPage(result["text"], result.get("first_cursor"))


async def _proxy_listing(client: Any, listing: str) -> dict[str, Any]:
    """The host journal's boots or identifiers, asked of Supervisor."""
    if is_running_in_addon():
        # Core refuses the app's token on its Supervisor command.
        response = await client._supervisor_logs_response(
            "host", {"Accept": "application/json"}, {}, f"/{listing}"
        )
        result = response.json()
    else:
        reply = await client.send_websocket_message(
            {
                "type": "supervisor/api",
                "method": "get",
                "endpoint": f"/host/logs/{listing}",
            }
        )
        if not reply.get("success"):
            raise HomeAssistantAPIError(str(reply.get("error")))
        result = reply["result"]
    payload = result.get("data", result) if isinstance(result, dict) else None
    if not isinstance(payload, dict) or listing not in payload:
        raise HomeAssistantAPIError(
            "Supervisor returned an unexpected journal listing."
        )
    return payload


async def _proxy_selection(
    client: Any, source: str, args: dict[str, Any], warning: str
) -> dict[str, Any]:
    if listing := args.get("listing"):
        return await _proxy_listing(client, listing)
    boot, identifier, latest = (
        args.get(key) for key in ("boot", "identifier", "latest")
    )
    selection = (
        (f"/boots/{boot}" if boot is not None else "")
        + (f"/identifiers/{quote(identifier, safe='')}" if identifier else "")
        + ("/latest" if latest else "")
    )
    headers = {"Accept": "text/plain"}
    params = {"no_colors": "true", "lines": str(args["lines"])}
    if is_running_in_addon():
        # Core's proxy refuses the app's token; Supervisor serves every selection.
        response = await client._supervisor_logs_response(
            _journal_path(source), headers, params, selection
        )
        return {"text": response.text}
    if (
        identifier
        or (latest and source != "core")
        or (boot is not None and not re.fullmatch(r"-?\d+", str(boot)))
    ):
        # Core's Supervisor proxy serves none of these paths.
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                "A boot ID, a host identifier and the latest start of an app or "
                "service need Home Assistant 2026.12 or newer with the HA-MCP component.",
                suggestions=[
                    warning,
                    "A boot can be chosen by its offset: 0 is the current boot, -1 the one before.",
                ],
            )
        )
    response = await client._raw_request(
        "GET",
        f"/hassio/{_journal_path(source)}/logs{selection}",
        headers=headers,
        params=params,
    )
    return {"text": response.text}


async def _selection_reply(
    client: Any, journal: str, args: dict[str, Any], lines: int, warnings: list[str]
) -> dict[str, Any]:
    """The selection through the component, shortened to its byte limit, or the proxy."""
    asked = lines
    while True:
        try:
            reply = await component_journal(client, journal, **args, lines=lines)
            break
        except HomeAssistantAPIError as exc:
            if lines == 1 or not _byte_limited(exc):
                raise
            lines //= 2
    if lines < asked:
        warnings.append(
            f"HA-MCP's byte limit shortened this read to its newest {lines} entries."
        )
    if reply is not None:
        return reply
    warning = await journal_fallback_warning(
        client, "the journal selection was read through the Supervisor API"
    )
    warnings.append(warning)
    return await _proxy_selection(client, journal, {**args, "lines": lines}, warning)


def _selected_name(
    source: str,
    slug: str | None,
    boot: int | str | None,
    latest: bool,
    identifier: str | None,
    listing: str | None,
) -> str:
    """The journal a selection reads, once its parameters agree."""
    if not listing:
        _validate_log_slug(source, slug)
    journal = (
        "core"
        if source == "error_log"
        else f"app:{slug}"
        if source == "supervisor"
        else slug or "host"
    )
    if (
        source not in {"system_service", "error_log", "supervisor"}
        or (identifier and journal != "host")
        or (identifier and not re.fullmatch(IDENTIFIER_PATTERN, identifier))
    ):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "Choose a journal source. Host identifiers require source='system_service', slug='host'.",
            )
        )
    if (boot is not None and latest) or (
        boot is not None and not re.fullmatch(r"-?\d+|[a-fA-F0-9]{32}", str(boot))
    ):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "Use a boot offset or a boot ID, or select latest on its own.",
            )
        )
    return journal


async def selected_journal(
    client: Any,
    source: str,
    slug: str | None,
    *,
    boot: int | str | None,
    latest: bool,
    identifier: str | None,
    listing: str | None,
    limit: int | None,
    search: str | None,
    order: str | None,
) -> dict[str, Any]:
    """Read a selected boot, service start, host identifier, or discovery list."""
    journal = _selected_name(source, slug, boot, latest, identifier, listing)
    effective = _coerce_limit(limit, default=DEFAULT_LOG_LIMIT)
    args = {
        k: v
        for k, v in {"boot": boot, "identifier": identifier, "listing": listing}.items()
        if v is not None
    }
    args["latest"] = latest
    result: dict[str, Any] = {"success": True, "source": source, "slug": slug}
    warnings: list[str] = []
    context = {"source": source, "slug": slug}
    try:
        reply = await _selection_reply(
            client,
            journal,
            args,
            SUPERVISOR_SEARCH_WINDOW_LINES
            if search
            else max(effective, MIN_LOG_WINDOW_LINES),
            warnings,
        )
    except HomeAssistantAuthError as exc:
        if await proxy_refused_path(client):
            raise_tool_error(
                create_error_response(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    f"Home Assistant serves no such journal for '{journal}'.",
                    context=context,
                )
            )
        exception_to_structured_error(
            exc, context=context, suggestions=_addon_auth_error_suggestions()
        )
    except HomeAssistantAPIError as exc:
        exception_to_structured_error(
            exc, context={**context, "status": exc.status_code}
        )
    except (HomeAssistantError, TimeoutError, OSError) as exc:
        exception_to_structured_error(exc, context=context)
    if warnings:
        result["warnings"] = warnings
    if listing:
        return {**result, listing: reply[listing]}
    matches = [
        line
        for line in log_lines(reply["text"])
        if not search or search.casefold() in line.casefold()
    ]
    selected = matches[-effective:]
    if order != "oldest":
        selected.reverse()
    return {
        **result,
        "log": "\n".join(selected),
        "total_lines": len(matches),
        "returned_lines": len(selected),
        "limit": effective,
        "order": order or "newest",
    }
