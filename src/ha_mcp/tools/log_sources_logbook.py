"""Logbook source for ``ha_get_logs`` — component, REST and WS transports.

Moved out of ``log_sources`` when the transport work pushed that module past
the 1,000-line limit. The Supervisor-backed sources stay in
``log_sources_supervisor`` and the fault log in ``log_sources_fault``; the host
class (``LogTools`` in ``tools_logs``) mixes this in alongside them.

Transport order (best first), all fetching from Home Assistant's own code:

1. ``ha_mcp_tools/logbook_events`` — Core's own ``EventProcessor`` run in the
   live HA process through the component (see
   ``docs/agents/native-core-contracts.md``), when the capability is
   advertised. Rows carry Core's names and ISO ``when``.
2. ``/api/logbook`` — Core's REST view, the component-absent fallback for
   requests it can serve (no device filter).
3. ``logbook/get_events`` — Core's WebSocket command, only for ``device_id``
   requests without the component (the only transport with a device filter).
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, NoReturn

from ha_mcp._vendor.fastmcp.exceptions import ToolError

from ..client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ..errors import ErrorCode, create_error_response
from .component_api import (
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)
from .helpers import exception_to_structured_error, raise_tool_error
from .log_common import _coerce_limit, _compact_logbook_entries
from .response_helpers import add_timezone_metadata

logger = logging.getLogger(__name__)

# The component command serving Core's own logbook processor in-process, and
# the capability that gates it.
LOGBOOK_EVENTS_COMMAND = "ha_mcp_tools/logbook_events"
LOGBOOK_EVENTS_CAPABILITY = "logbook_events"


def _wrapped_timeout(exc: BaseException) -> bool:
    """True when ``exc`` is (or wraps) a WebSocket command timeout.

    ``HomeAssistantClient.send_websocket_message`` funnels every no-answer
    outcome — including ``HomeAssistantCommandTimeout`` — into
    ``HomeAssistantConnectionError`` and re-raises the timeout as its cause
    (``_NO_ANSWER_ERRORS`` in ``rest_client``), so a timeout must be detected
    through the chain. The direct ``except HomeAssistantCommandTimeout``
    branches stay for callers/mocks that raise it untranslated.
    """
    cur: BaseException | None = exc
    while cur is not None:
        if isinstance(cur, HomeAssistantCommandTimeout):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


class LogbookLogSourceMixin:
    """``ha_get_logs(source='logbook')`` — component, REST and WS transports."""

    _client: Any

    @staticmethod
    def _coerce_logbook_params(
        hours_back: int,
        limit: int | None,
        offset: int,
    ) -> tuple[int, int, int]:
        effective_limit = _coerce_limit(limit)
        return hours_back, effective_limit, offset

    @staticmethod
    def _build_pagination_hint(
        offset_int: int,
        effective_limit: int,
        total_entries: int,
        paginated_entries: Any,
        hours_back_int: int,
        end_time: str | None,
        entity_id: str | None,
        device_id: str | None,
        context_id: str | None,
        search: str | None,
        compact_bool: bool,
        order: Literal["newest", "oldest"] = "newest",
    ) -> str:
        """Build reproducible pagination hint string for logbook results.

        String values are quoted: the hint is meant to be copied back as a
        call, and an unquoted ``search=front door`` is not one.
        """
        next_offset = offset_int + effective_limit
        param_parts = [
            f"hours_back={hours_back_int}",
            f"limit={effective_limit}",
            f"offset={next_offset}",
        ]
        if entity_id:
            param_parts.append(f"entity_id={entity_id!r}")
        if device_id:
            param_parts.append(f"device_id={device_id!r}")
        if context_id:
            param_parts.append(f"context_id={context_id!r}")
        if end_time:
            param_parts.append(f"end_time={end_time!r}")
        if search:
            param_parts.append(f"search={search!r}")
        if not compact_bool:
            param_parts.append("compact=False")
        if order != "newest":
            param_parts.append(f"order={order!r}")
        param_str = ", ".join(param_parts)
        return (
            f"Showing entries {offset_int + 1}-{offset_int + len(paginated_entries)} of {total_entries}. "
            f"To get the next page, use: ha_get_logs({param_str})"
        )

    @staticmethod
    def _filter_logbook_by_search(
        response: Any, search: str | None
    ) -> tuple[Any, dict[str, str]]:
        """Filter logbook entries by search term across name/message/entity_id.

        Returns the (possibly filtered) response and a filters_applied dict
        recording which filters were used.
        """
        filters_applied: dict[str, str] = {}
        if search and isinstance(response, list):
            search_lower = search.lower()
            response = [
                e
                for e in response
                if isinstance(e, dict)
                and (
                    search_lower in str(e.get("name", "")).lower()
                    or search_lower in str(e.get("message", "")).lower()
                    or search_lower in str(e.get("entity_id", "")).lower()
                )
            ]
            filters_applied["search"] = search
        return response, filters_applied

    @staticmethod
    def _paginate_logbook_entries(
        response: Any,
        offset_int: int,
        effective_limit: int,
        order: Literal["newest", "oldest"],
    ) -> tuple[Any, int, bool]:
        """Slice logbook entries into the requested page.

        HA's /logbook returns entries oldest-first. Takes a window from
        the end for newest-first (default), or from the start for
        oldest-first, with offset paging deeper in the chosen order.
        """
        total_entries = len(response) if isinstance(response, list) else 1

        if isinstance(response, list):
            if order == "newest":
                end = total_entries - offset_int
                start = max(end - effective_limit, 0)
                paginated_entries = (
                    list(reversed(response[start:end])) if end > 0 else []
                )
            else:
                paginated_entries = response[offset_int : offset_int + effective_limit]
            has_more = offset_int + len(paginated_entries) < total_entries
        else:
            paginated_entries = response
            has_more = False

        return paginated_entries, total_entries, has_more

    @staticmethod
    def _logbook_error_suggestions(error: Exception) -> list[str]:
        """Build remediation suggestions for a logbook fetch failure.

        Keyed off the HTTP status the client attached (when any). A 500 from
        Core is ambiguous: the REST view raises for a malformed ``entity_id``
        and an oversized query can also fail, so the guidance names both
        instead of asserting the query was too large.
        """
        status = getattr(error, "status_code", None)
        if status in (400, 422):
            return [
                "Home Assistant rejected the request as invalid",
                "Check that 'entity_id' uses 'domain.object_id' form, "
                + "e.g. 'light.kitchen'",
                "Use ha_get_logs(source='error_log') to check Home Assistant logs",
            ]
        if status == 500:
            return [
                "A 500 can mean invalid input: check that 'entity_id' uses "
                + "'domain.object_id' form (e.g. 'light.kitchen')",
                "If the filters are valid, the query may be too large: reduce "
                + "'hours_back' or narrow the filters",
                "Use ha_get_logs(source='error_log') to check Home Assistant "
                + "logs for the traceback",
            ]
        return [
            "Try reducing 'hours_back' parameter (e.g., from 24 to 1 hour)",
            "Add a specific 'entity_id' filter to narrow down results",
        ]

    @staticmethod
    def _split_logbook_filter(value: str | None) -> list[str]:
        """Split a comma-separated native filter, dropping blank items."""
        if not value:
            return []
        return [item.strip() for item in value.split(",") if item.strip()]

    async def _fetch_logbook_events(
        self,
        *,
        start_time: str,
        end_time: str,
        entity_id: str | None,
        device_id: str | None,
        context_id: str | None,
    ) -> tuple[Any, str]:
        """Fetch events over Home Assistant's own transports, best first.

        1. ``ha_mcp_tools/logbook_events`` — Core's own ``EventProcessor`` run
           in the live Home Assistant process, when the component advertises
           it. Rows already carry Core's names and ISO ``when``.
        2. ``/api/logbook`` — Core's REST view; same processor flags, no
           device filter.
        3. ``logbook/get_events`` — Core's WebSocket command; the only
           transport with a device filter, but its rows use epoch-second
           ``when`` and carry no names.

        Filters are normalized once here so every transport sees the same
        items: a CSV of blanks (``", "``) is invalid input, never an
        unrestricted query.

        Returns ``(response, transport)``.
        """
        entity_ids = self._split_logbook_filter(entity_id)
        device_ids = self._split_logbook_filter(device_id)
        for label, raw, ids in (
            ("entity_id", entity_id, entity_ids),
            ("device_id", device_id, device_ids),
        ):
            if raw and not ids:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"'{label}' contains no {label.split('_')[0]} IDs",
                        context={label: raw},
                        suggestions=[
                            "Pass comma-separated IDs, e.g. 'light.kitchen,light.hall'"
                        ],
                    )
                )

        command: dict[str, Any] = {
            "type": "logbook/get_events",
            "start_time": start_time,
            "end_time": end_time,
        }
        if entity_ids:
            command["entity_ids"] = entity_ids
        if device_ids:
            command["device_ids"] = device_ids
        if context_id:
            command["context_id"] = context_id

        response = await self._fetch_logbook_component(command)
        if response is not None:
            return response, "component"

        if device_ids:
            # REST has no device filter; only the WebSocket command can serve
            # this request.
            return await self._fetch_logbook_websocket(command), "websocket"

        rest = await self._client.get_logbook(
            entity_id=",".join(entity_ids) if entity_ids else None,
            start_time=start_time,
            end_time=end_time,
            context_id=context_id,
        )
        return rest, "rest"

    async def _fetch_logbook_component(
        self, command: dict[str, Any]
    ) -> list[Any] | None:
        """Run the component's Core-processor command, or ``None`` to fall back.

        ``None`` means the component is absent, downgraded (``unknown_command``
        drops the cached capability) or the transport failed before Home
        Assistant saw the request. Core's own rejections surface unchanged, and
        a timeout surfaces instead of falling back — the recorder query is
        still running inside Home Assistant.
        """
        caps = await get_component_caps(self._client)
        if not component_supports(caps, LOGBOOK_EVENTS_CAPABILITY):
            return None
        message = {**command, "type": LOGBOOK_EVENTS_COMMAND}
        try:
            response = await self._client.send_websocket_message(message)
        except HomeAssistantCommandTimeout:
            self._raise_logbook_timeout(command["start_time"], command["end_time"])
        except HomeAssistantConnectionError as exc:
            if _wrapped_timeout(exc):
                self._raise_logbook_timeout(command["start_time"], command["end_time"])
            return None
        except HomeAssistantCommandError as exc:
            if not is_unknown_command(exc):
                raise
            # Older component without the command: stop routing to it.
            invalidate_caps(self._client)
            return None

        if isinstance(response, dict) and response.get("success"):
            # The command's result is Core's own row list (its result message
            # wraps ``EventProcessor.get_events()`` output directly).
            result = response.get("result")
            return result if isinstance(result, list) else None

        self._raise_logbook_rejection(response, LOGBOOK_EVENTS_COMMAND)
        if (
            isinstance(response, dict)
            and response.get("error_code") == "unknown_command"
        ):
            # Older component without the command: stop routing to it.
            invalidate_caps(self._client)
        return None

    async def _fetch_logbook_websocket(self, command: dict[str, Any]) -> list[Any]:
        """Core's ``logbook/get_events`` — the device-filter fallback."""
        try:
            response = await self._client.send_websocket_message(command)
        except HomeAssistantCommandTimeout:
            self._raise_logbook_timeout(command["start_time"], command["end_time"])
        except HomeAssistantConnectionError as exc:
            if _wrapped_timeout(exc):
                self._raise_logbook_timeout(command["start_time"], command["end_time"])
            self._raise_logbook_ws_required(command.get("device_ids"))
        except HomeAssistantCommandError as exc:
            if not is_unknown_command(exc):
                raise
            self._raise_logbook_ws_required(command.get("device_ids"))

        if isinstance(response, dict) and response.get("success"):
            result = response.get("result")
            return result if isinstance(result, list) else []
        self._raise_logbook_rejection(response, "logbook/get_events")
        # No REST device fallback exists after this point, so a tolerated
        # soft failure (``unknown_command`` on an old Core, or a code-less
        # proxy envelope such as a post-connect 403) must surface instead of
        # being reported as an empty logbook.
        reason = response.get("error") if isinstance(response, dict) else None
        return self._raise_logbook_ws_required(
            command.get("device_ids"),
            reason=str(reason) if reason else None,
        )

    def _raise_logbook_timeout(self, start_time: str, end_time: str) -> NoReturn:
        """Surface a query timeout; never re-run it on a fallback transport.

        The client only drops its local future; Home Assistant keeps running
        the recorder query. Falling back would run the slowest query a second
        time, so the timeout surfaces instead.
        """
        raise_tool_error(
            create_error_response(
                ErrorCode.TIMEOUT_OPERATION,
                "The logbook query timed out inside Home Assistant; it may "
                "still be running.",
                context={"start_time": start_time, "end_time": end_time},
                suggestions=[
                    "Retry the request",
                    "Reduce hours_back or filter by entity_id",
                ],
            )
        )

    def _raise_logbook_ws_required(
        self, device_ids: Any, *, reason: str | None = None
    ) -> NoReturn:
        """The device filter needs the WebSocket transport, which is down."""
        message = (
            "The device_id filter requires Home Assistant's WebSocket API, "
            "which is unavailable."
        )
        if reason:
            message = f"{message} Last error: {reason}"
        raise_tool_error(
            create_error_response(
                ErrorCode.CONNECTION_FAILED,
                message,
                context={"device_ids": device_ids},
                suggestions=[
                    "Check the Home Assistant WebSocket connection",
                    "Filter by entity_id instead",
                ],
            )
        )

    def _raise_logbook_rejection(self, response: Any, command: str) -> None:
        """Surface Core's own rejection unless it means "unknown command".

        ``send_websocket_message`` reports Home Assistant's own failures as a
        soft envelope carrying Core's code; only an unknown command (older
        Core/component) or a code-less transport envelope (for example a proxy
        403) may fall back. Any other rejection is authoritative and reaches
        the caller unchanged.
        """
        error_code = response.get("error_code") if isinstance(response, dict) else None
        if error_code == "invalid_format":
            # The bridge schema runs Core's own entity-id validator, so a
            # malformed request comes back as HA's format error instead of a
            # handler traceback.
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    str(response.get("error") or "Invalid logbook request"),
                    context={"error_code": error_code},
                    suggestions=[
                        "Check that 'entity_id' uses 'domain.object_id' form, "
                        "e.g. 'light.kitchen'"
                    ],
                )
            )
        if error_code and error_code != "unknown_command":
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    str(response.get("error") or f"{command} failed"),
                    context={"error_code": error_code},
                )
            )

    @staticmethod
    def _normalize_logbook_events(events: Any, transport: str) -> Any:
        """Normalize the WebSocket fallback rows onto the REST-shaped contract.

        ``logbook/get_events`` returns ``when`` as epoch seconds and omits the
        ``name`` field on state rows. The component command and REST both
        return Core's REST shape (ISO ``when``, names from Core's own
        ``EventProcessor``), so they pass through untouched; only the
        WebSocket fallback is rewritten. Names are not reconstructed here:
        without the component or REST the rows simply carry no ``name`` (the
        caller warns).
        """
        if transport != "websocket" or not isinstance(events, list):
            return events
        normalized: list[Any] = []
        for event in events:
            if not isinstance(event, dict):
                normalized.append(event)
                continue
            row = dict(event)
            when = row.get("when")
            if isinstance(when, (int, float)) and not isinstance(when, bool):
                row["when"] = datetime.fromtimestamp(float(when), UTC).isoformat()
            normalized.append(row)
        return normalized

    async def _get_logbook(
        self,
        hours_back: int = 1,
        entity_id: str | None = None,
        device_id: str | None = None,
        context_id: str | None = None,
        end_time: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        search: str | None = None,
        compact: bool = True,
        order: Literal["newest", "oldest"] = "newest",
    ) -> dict[str, Any]:
        """Fetch logbook entries with search and pagination."""
        hours_back_int, effective_limit, offset_int = self._coerce_logbook_params(
            hours_back, limit, offset
        )

        if context_id and (entity_id or device_id):
            # Core's EventProcessor asserts on this combination and REST
            # rejects it with a 400; fail early with the structured shape.
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "context_id cannot be combined with entity_id or device_id",
                    context={
                        "entity_id": entity_id,
                        "device_id": device_id,
                        "context_id": context_id,
                    },
                    suggestions=[
                        "Use context_id alone to trace what caused an event",
                        "Use entity_id or device_id alone for entity/device events",
                    ],
                )
            )

        if end_time:
            try:
                end_dt = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
            except ValueError:
                # Outside the fetch try-block below, so without this guard the
                # raw ValueError would bypass the structured ToolError shape.
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"Invalid end_time '{end_time}': not an ISO 8601 timestamp",
                        suggestions=[
                            "Use ISO format, e.g. end_time='2026-08-28T01:00:00Z'"
                        ],
                    )
                )
        else:
            end_dt = datetime.now(UTC)

        start_dt = end_dt - timedelta(hours=hours_back_int)
        start_timestamp = start_dt.isoformat()

        try:
            response, transport = await self._fetch_logbook_events(
                start_time=start_timestamp,
                end_time=end_dt.isoformat(),
                entity_id=entity_id,
                device_id=device_id,
                context_id=context_id,
            )

            response = self._normalize_logbook_events(response, transport)
            missing_names = (
                transport == "websocket"
                and isinstance(response, list)
                and any(
                    isinstance(row, dict) and "state" in row and not row.get("name")
                    for row in response
                )
            )
            transport_warnings = (
                [
                    "State rows from the WebSocket fallback carry no 'name'; "
                    "the ha_mcp_tools component supplies Core-resolved names."
                ]
                if missing_names
                else []
            )

            response, filters_applied = self._filter_logbook_by_search(response, search)

            paginated_entries, total_entries, has_more = self._paginate_logbook_entries(
                response, offset_int, effective_limit, order
            )

            # In compact mode, strip entries to essential fields only.
            # This prevents full attribute dictionaries from exhausting
            # the LLM context window during debugging workflows.
            if compact and isinstance(paginated_entries, list):
                paginated_entries = _compact_logbook_entries(paginated_entries)

            logbook_data: dict[str, Any] = {
                "success": True,
                "source": "logbook",
                "entries": paginated_entries,
                "period": f"{hours_back_int} hours back from {end_dt.isoformat()}",
                "start_time": start_timestamp,
                "end_time": end_dt.isoformat(),
                "entity_filter": entity_id,
                "device_filter": device_id,
                "context_filter": context_id,
                "total_entries": total_entries,
                "returned_entries": len(paginated_entries)
                if isinstance(paginated_entries, list)
                else 1,
                "limit": effective_limit,
                "offset": offset_int,
                "order": order,
                "has_more": has_more,
            }
            if filters_applied:
                logbook_data["filters_applied"] = filters_applied
            if has_more:
                logbook_data["pagination_hint"] = self._build_pagination_hint(
                    offset_int,
                    effective_limit,
                    total_entries,
                    paginated_entries,
                    hours_back_int,
                    end_dt.isoformat(),
                    entity_id,
                    device_id,
                    context_id,
                    search,
                    compact,
                    order,
                )

            wrapped = await add_timezone_metadata(self._client, logbook_data)
            if transport_warnings:
                # Top-level, alongside the other tool warnings: nesting these
                # under ``data`` would hide them from the warning merger.
                wrapped["warnings"] = transport_warnings + wrapped.get("warnings", [])
            return wrapped

        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={
                    "period": f"{hours_back_int} hours back from {end_dt.isoformat()}",
                },
                suggestions=self._logbook_error_suggestions(e),
            )
            raise  # unreachable: exception_to_structured_error always raises
