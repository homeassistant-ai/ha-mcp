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

from datetime import UTC, datetime, timedelta
from typing import Any, Literal, NoReturn

from ulid_transform import ulid_to_bytes_or_none

from ha_mcp._vendor.fastmcp.exceptions import ToolError

from ..client.exceptions import HomeAssistantResponseTooLarge
from ..client.rest_client import (
    HomeAssistantAuthError,
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ..errors import ErrorCode, create_error_response
from .component_api import (
    component_supports,
    component_unusable_warning,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)
from .helpers import exception_to_structured_error, raise_tool_error
from .log_common import _coerce_limit
from .logbook_cursor import (
    LogbookCursor,
    fill_logbook_page,
)
from .response_helpers import add_timezone_metadata, require_ha_timezone

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
    def _validate_logbook_filters(
        context_id: str | None,
    ) -> None:
        # Core uses a binary ULID to constrain a context-only recorder query.
        if context_id and ulid_to_bytes_or_none(context_id) is None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "The Logbook context filter must be a valid Home Assistant context ID.",
                )
            )

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
        action_calls: bool = False,
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
        if action_calls:
            param_parts.append("action_calls=True")
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
                    search_lower
                    in f"{e.get('name') or ''} {e.get('message') or ''}".strip().lower()
                    or search_lower in str(e.get("entity_id") or "").lower()
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

    def _scope(
        self,
        entity_id: str | None,
        device_id: str | None,
        context_id: str | None,
        action_calls: bool,
    ) -> dict[str, Any]:
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

        command: dict[str, Any] = {}
        if entity_ids:
            command["entity_ids"] = entity_ids
        if device_ids:
            command["device_ids"] = device_ids
        if context_id:
            command["context_id"] = context_id

        if action_calls:
            command["action_calls"] = True
        return command

    async def _native_rows(self, command: dict[str, Any]) -> tuple[Any, str]:
        native = dict(command)
        action_calls = native.pop("action_calls", False)
        if action_calls or native.get("device_ids"):
            native["type"] = (
                "logbook/get_action_calls" if action_calls else "logbook/get_events"
            )
            return await self._fetch_logbook_websocket(native), "websocket"
        rest = await self._client.get_logbook(
            entity_id=",".join(command.get("entity_ids", [])) or None,
            start_time=command["start_time"],
            end_time=command["end_time"],
            context_id=command.get("context_id"),
        )
        if rest is None:
            # get_logbook answers None only for a reply that was not a row
            # list; an empty logbook is a real list, so this is a failure.
            # Core's ``/logbook`` always answers a list, so this guards a
            # non-conforming responder and no test can produce it.
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "The REST logbook reply was not a row list.",
                    context={"source": "logbook", "transport": "rest"},
                    suggestions=[
                        "Retry the request; the REST logbook endpoint answered "
                        + "with an unexpected payload",
                        "Check the Home Assistant logs if it keeps happening",
                    ],
                )
            )
        return rest, "rest"

    async def _fetch_logbook_component(
        self, command: dict[str, Any], page: dict[str, Any] | None = None
    ) -> list[Any] | dict[str, Any] | None:
        """Run the component's Core-processor command, or ``None`` to fall back.

        ``None`` means the component is absent, downgraded (``unknown_command``
        drops the cached capability) or the transport gave no answer: no
        connection, or one that dropped before the reply. Core's own rejections
        surface unchanged. Timeouts and oversized replies must not repeat the
        recorder query. A token rejected on the command itself also surfaces;
        a token already rejected on the capability probe falls back.
        """
        caps = await get_component_caps(self._client)
        if not component_supports(caps, LOGBOOK_EVENTS_CAPABILITY):
            return None
        message = {**command, "type": LOGBOOK_EVENTS_COMMAND}
        if page and component_supports(caps, "logbook_scoped_pages"):
            message["page"] = page
        elif page:
            return None
        try:
            response = await self._client.send_websocket_message(message)
        except HomeAssistantCommandTimeout:
            self._raise_logbook_timeout(
                command["start_time"], command.get("end_time") or ""
            )
        except HomeAssistantConnectionError as exc:
            self._raise_if_logbook_read_failed(exc, command)
            if "page" in message or isinstance(exc.__cause__, HomeAssistantAuthError):
                # A failed paged read must never turn into a whole-window retry.
                exception_to_structured_error(exc)
            return None
        except HomeAssistantCommandError as exc:
            if not is_unknown_command(exc):
                raise
            # Older component without the command: stop routing to it.
            invalidate_caps(self._client)
            return None

        return self._component_reply(response, "page" in message)

    def _component_reply(
        self, response: Any, paged: bool
    ) -> list[Any] | dict[str, Any] | None:
        if isinstance(response, dict) and response.get("success"):
            return self._component_rows(response.get("result"), paged)

        self._raise_logbook_rejection(response, LOGBOOK_EVENTS_COMMAND)
        if (
            isinstance(response, dict)
            and response.get("error_code") == "unknown_command"
        ):
            # Older component without the command: stop routing to it.
            invalidate_caps(self._client)
        elif paged:
            raise_tool_error(
                create_error_response(
                    ErrorCode.CONNECTION_FAILED, "No reply to the paged Logbook read."
                )
            )
        return None

    @staticmethod
    def _component_rows(result: Any, paged: bool) -> list[Any] | dict[str, Any] | None:
        if not paged:
            return result if isinstance(result, list) else None
        if not isinstance(result, dict) or not isinstance(result.get("entries"), list):
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "The Logbook component returned an invalid page.",
                )
            )
        return result

    async def _fetch_logbook_websocket(self, command: dict[str, Any]) -> list[Any]:
        """Core's ``logbook/get_events`` — the device-filter fallback."""
        try:
            response = await self._client.send_websocket_message(command)
        except HomeAssistantCommandTimeout:
            self._raise_logbook_timeout(
                command["start_time"], command.get("end_time") or ""
            )
        except HomeAssistantConnectionError as exc:
            self._raise_if_logbook_read_failed(exc, command)
            if isinstance(exc.__cause__, HomeAssistantAuthError):
                # A token rejected on connect is not a WebSocket outage.
                exception_to_structured_error(
                    exc, context={"device_ids": command.get("device_ids")}
                )
            self._raise_logbook_ws_required(command, reason=str(exc))
        except HomeAssistantCommandError as exc:
            if not is_unknown_command(exc):
                raise
            self._raise_logbook_ws_required(command)

        if isinstance(response, dict) and response.get("success"):
            result = response.get("result")
            return result if isinstance(result, list) else []
        self._raise_logbook_rejection(response, command["type"])
        # No REST device fallback exists after this point, so a tolerated
        # soft failure (``unknown_command`` on an old Core, or a code-less
        # proxy envelope such as a post-connect 403) must surface instead of
        # being reported as an empty logbook.
        reason = response.get("error") if isinstance(response, dict) else None
        return self._raise_logbook_ws_required(
            command,
            reason=str(reason) if reason else None,
        )

    def _raise_if_logbook_read_failed(
        self, exc: HomeAssistantConnectionError, command: dict[str, Any]
    ) -> None:
        if _wrapped_timeout(exc):
            self._raise_logbook_timeout(
                command["start_time"], command.get("end_time") or ""
            )
        if isinstance(exc, HomeAssistantResponseTooLarge):
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "The WebSocket closed because a response was too large. "
                    "The Logbook query was not repeated.",
                    suggestions=[
                        "Narrow the time window or filter by a specific entity or device"
                    ],
                )
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
        self, command: dict[str, Any], *, reason: str | None = None
    ) -> NoReturn:
        """Surface an unavailable native WebSocket-only read."""
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
                context={
                    "command": command["type"],
                    "device_ids": command.get("device_ids"),
                },
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
                        + "e.g. 'light.kitchen'"
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

    async def _required_component_page(
        self, command: dict[str, Any], page: dict[str, Any]
    ) -> dict[str, Any]:
        response = await self._fetch_logbook_component(command, page)
        if not isinstance(response, dict):
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "The component serving this Logbook page is unavailable.",
                )
            )
        return response

    async def _native_page(
        self, command: dict[str, Any], request: dict[str, Any], hours: int
    ) -> dict[str, Any]:
        # HA-MCP policy: native transports materialize the entire time window.
        if hours > 24:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "HA-MCP limits native Logbook reads to 24 hours. Install or update the ha_mcp_tools component for longer windows.",
                )
            )
        rows, transport = await self._native_rows(command)
        rows = self._normalize_logbook_events(rows, transport)
        rows, _ = self._filter_logbook_by_search(rows, request.get("search"))
        selected, total, _ = self._paginate_logbook_entries(
            rows, request["skip"], request["limit"], request["order"]
        )
        caps = await get_component_caps(self._client)
        label = "WebSocket" if transport == "websocket" else "REST"
        warnings = [
            component_unusable_warning(
                caps, f"the {label} Logbook read loaded its whole window"
            )
        ]
        if transport == "websocket" and any(
            "state" in row and not row.get("name") for row in selected
        ):
            warnings.append("State rows from the WebSocket fallback carry no 'name'.")
        return {
            "entries": selected,
            "total": total,
            "legacy": True,
            "warnings": warnings,
        }

    async def _format_logbook_page(
        self,
        request: dict[str, Any],
        cursor: LogbookCursor | None,
        offset: int,
        start: datetime,
        end: datetime,
        order: Literal["oldest", "newest"],
        search: str | None,
        limit: int,
        compact: bool,
        scope: dict[str, Any],
    ) -> dict[str, Any]:
        command = {
            **scope,
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
        }
        open_window = cursor.open_at_count if cursor else end > datetime.now(UTC)
        before = cursor.seen if cursor else 0
        first = await (
            self._required_component_page if cursor else self._fetch_logbook_component
        )(command, request)
        if isinstance(first, dict):
            page = await fill_logbook_page(
                self._required_component_page, command, request, first, cursor
            )
        else:
            page = await self._native_page(
                command, request, (end - start) // timedelta(hours=1)
            )
        total = page["total"]
        entries = page["entries"]
        after = page.get("after")
        position = max(
            offset, before + page.get("matched", len(entries)) - len(entries)
        )
        more = (
            position + len(entries) < total
            if page.get("legacy")
            else after is not None
            and (open_window or total is None or position + len(entries) < total)
        )
        hours = (end - start) // timedelta(hours=1)
        data: dict[str, Any] = {
            "success": True,
            "source": "logbook",
            "entries": entries,
            "period": f"{hours} hours back from {end.isoformat()}",
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
            "entity_filter": ",".join(scope.get("entity_ids", [])) or None,
            "context_filter": scope.get("context_id"),
            "device_filter": ",".join(scope.get("device_ids", [])) or None,
            "total_entries": total,
            "returned_entries": len(entries),
            "limit": limit,
            "offset": position,
            "order": order,
            "has_more": more,
        }
        if total is None:
            data["total_entries_lower_bound"] = page["lower_bound"]
        if search:
            data["filters_applied"] = {"search": search}
        if page.get("legacy"):
            data["warnings"] = page["warnings"]
            if more:
                data["next_offset"] = position + len(entries)
                data["pagination_hint"] = self._build_pagination_hint(
                    position,
                    limit,
                    total,
                    entries,
                    hours,
                    end.isoformat(),
                    data["entity_filter"],
                    data["device_filter"],
                    data["context_filter"],
                    search,
                    compact,
                    order,
                    bool(scope.get("action_calls")),
                )
        elif more and after is not None:
            data["next_offset"] = LogbookCursor(
                scope=scope,
                lower_bound=page["lower_bound"],
                hours_back=hours,
                total=total,
                end=end,
                order=order,
                search=search,
                after=after,
                seen=before + page["matched"],
                skip=page["skip"],
                open_at_count=open_window,
            ).token()
            data["pagination_hint"] = (
                "Pass next_offset as offset with the same filters and order to continue this Logbook window."
            )
        return await add_timezone_metadata(self._client, data)

    async def _logbook_page_request(
        self,
        offset: int | str,
        end_time: str | None,
        order: Literal["newest", "oldest"],
        search: str | None,
        limit: int,
        scope: dict[str, Any],
    ) -> tuple[LogbookCursor | None, datetime, dict[str, Any]]:
        cursor = LogbookCursor.parse(offset) if isinstance(offset, str) else None
        end = cursor.end if cursor else datetime.now(UTC)
        if end_time:
            try:
                end = datetime.fromisoformat(end_time)
            except ValueError:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"Invalid end_time '{end_time}': not an ISO 8601 timestamp",
                        suggestions=[
                            "Use ISO format, e.g. end_time='2026-08-28T01:00:00Z'"
                        ],
                    )
                )
        if end.tzinfo is None:
            zone = await require_ha_timezone(
                self._client,
                needed_for="a Logbook time without an offset needs",
                workaround="Pass end_time with an explicit UTC offset",
            )
            end = end.replace(tzinfo=zone).astimezone(UTC)
        if cursor and (
            scope != cursor.scope
            or (order, search, end) != (cursor.order, cursor.search, cursor.end)
        ):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "Use this Logbook cursor with the same window, filters and order.",
                )
            )
        page: dict[str, Any] = {
            "order": order,
            "limit": limit,
            "skip": cursor.skip if cursor else offset,
        }
        if cursor:
            page["after"] = list(cursor.after)
        if search:
            page["search"] = search
        return cursor, end, page

    async def _get_logbook(
        self,
        hours_back: int | None = None,
        entity_id: str | None = None,
        device_id: str | None = None,
        context_id: str | None = None,
        end_time: str | None = None,
        limit: int | None = None,
        offset: int | str = 0,
        search: str | None = None,
        compact: bool = True,
        action_calls: bool = False,
        order: Literal["newest", "oldest"] = "newest",
    ) -> dict[str, Any]:
        """Fetch logbook entries with search and pagination."""
        effective_limit = _coerce_limit(limit)

        scope = self._scope(entity_id, device_id, context_id, action_calls)
        cursor, end_dt, page = await self._logbook_page_request(
            offset, end_time, order, search, effective_limit, scope
        )
        hours_back = (
            hours_back
            if hours_back is not None
            else (cursor.hours_back if cursor else 1)
        )
        if cursor and hours_back != cursor.hours_back:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "Use the same hours_back with this Logbook cursor.",
                )
            )
        self._validate_logbook_filters(context_id)
        numeric_offset = offset if isinstance(offset, int) else 0
        start_dt = end_dt - timedelta(hours=hours_back)
        try:
            return await self._format_logbook_page(
                page,
                cursor,
                numeric_offset,
                start_dt,
                end_dt,
                order,
                search,
                effective_limit,
                compact,
                scope,
            )
        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={
                    "period": f"{hours_back} hours back from {end_dt.isoformat()}",
                },
                suggestions=self._logbook_error_suggestions(e),
            )
            raise  # unreachable: exception_to_structured_error always raises
