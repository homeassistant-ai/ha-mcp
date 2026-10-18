"""Logbook source for ``ha_get_logs`` — Core WS with a REST fallback.

Moved out of ``log_sources`` when the native WebSocket transport and
name-normalization work pushed that module past the 1,000-line limit. The
Supervisor-backed sources stay in ``log_sources_supervisor`` and the fault log
in ``log_sources_fault``; the host class (``LogTools`` in ``tools_logs``)
mixes this in alongside them.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from ha_mcp._vendor.fastmcp.exceptions import ToolError

from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ..errors import ErrorCode, create_error_response
from .component_api import is_unknown_command
from .helpers import exception_to_structured_error, raise_tool_error
from .log_common import _coerce_limit, _compact_logbook_entries
from .response_helpers import add_timezone_metadata

logger = logging.getLogger(__name__)


class LogbookLogSourceMixin:
    """``ha_get_logs(source='logbook')`` — Core WS, REST as fallback."""

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
    def _logbook_error_suggestions(error_str: str) -> list[str]:
        """Build remediation suggestions for a logbook fetch failure.

        Adds server-crash-specific guidance when the error indicates a 500
        (heavy query causing HA to fail) on top of the general tips.
        """
        if "500" in error_str:
            return [
                "The query returned too many results causing a server error (500).",
                "This often happens with very active entities or long time periods.",
                "Try reducing 'hours_back' parameter (e.g., from 24 to 1 hour)",
                "Add a specific 'entity_id' filter to narrow down results",
                "If debugging an automation, filter by that automation's entity_id",
                "Use ha_report_issue to check Home Assistant logs for crash details",
            ]
        return [
            "Try reducing 'hours_back' parameter (e.g., from 24 to 1 hour)",
            "Add a specific 'entity_id' filter to narrow down results",
        ]

    async def _fetch_logbook_events(
        self,
        *,
        start_time: str,
        end_time: str,
        entity_id: str | None,
        device_id: str | None,
        context_id: str | None,
    ) -> tuple[Any, str]:
        """Fetch events over Core WS ``logbook/get_events``, REST as fallback.

        The WS command is the native path: it takes entity/device/context
        filters and defaults an omitted ``end_time`` to now. ``/api/logbook``
        stays for WebSocket-disabled instances; it has no device filter, so a
        ``device_id`` request is WebSocket-only, while ``context_id`` alone is
        supported by both.

        Returns ``(response, transport)``.
        """
        ws_params: dict[str, Any] = {
            "type": "logbook/get_events",
            "start_time": start_time,
            "end_time": end_time,
        }
        if entity_id:
            ws_params["entity_ids"] = [
                item.strip() for item in entity_id.split(",") if item.strip()
            ]
        if device_id:
            ws_params["device_ids"] = [
                item.strip() for item in device_id.split(",") if item.strip()
            ]
        if context_id:
            ws_params["context_id"] = context_id

        response: Any = None
        try:
            response = await self._client.send_websocket_message(ws_params)
        except HomeAssistantCommandTimeout:
            # The client only drops its local future; Home Assistant keeps
            # running the recorder query. Falling back would run the slowest
            # query a second time, so the timeout surfaces instead.
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
        except HomeAssistantConnectionError:
            response = None
        except HomeAssistantCommandError as exc:
            # Older Core without the command: fall back. Any other HA error
            # (bad timestamps, unsupported combination) would fail on REST too.
            if not is_unknown_command(exc):
                raise
            response = None

        if isinstance(response, dict) and response.get("success"):
            result = response.get("result")
            return result if isinstance(result, list) else [], "websocket"

        self._raise_logbook_rejection(response)

        if device_id:
            # REST has no device filter; only the WebSocket command can serve
            # this request.
            raise_tool_error(
                create_error_response(
                    ErrorCode.CONNECTION_FAILED,
                    "The device_id filter requires Home Assistant's WebSocket "
                    "API, which is unavailable.",
                    context={"device_id": device_id},
                    suggestions=[
                        "Check the Home Assistant WebSocket connection",
                        "Filter by entity_id instead",
                    ],
                )
            )
        response = await self._client.get_logbook(
            entity_id=entity_id,
            start_time=start_time,
            end_time=end_time,
            context_id=context_id,
        )
        return response, "rest"

    def _raise_logbook_rejection(self, response: Any) -> None:
        """Surface Core's own rejection unless it means "unknown command".

        ``send_websocket_message`` reports Home Assistant's own failures as a
        soft envelope carrying Core's code; only an unknown command (older
        Core) or a code-less transport envelope (for example a proxy 403) may
        fall back. Any other rejection is authoritative and reaches the
        caller unchanged.
        """
        error_code = response.get("error_code") if isinstance(response, dict) else None
        if error_code and error_code != "unknown_command":
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    str(response.get("error") or "logbook/get_events failed"),
                    context={"error_code": error_code},
                )
            )

    async def _logbook_state_entity_names(
        self, entity_ids: set[str]
    ) -> tuple[dict[str, str], list[str]]:
        """Resolve state-row names from Core; WS events carry no ``name``.

        Mirrors Core's ``EntityNameCache``: the live state's ``friendly_name``
        when present; otherwise the caller falls back to a prettified object
        id (as Core does). A failed lookup degrades to that fallback plus a
        caller-visible warning.
        """
        if not entity_ids:
            return {}, []
        try:
            states = await self._client.get_states()
        except (
            HomeAssistantAPIError,
            HomeAssistantAuthError,
            HomeAssistantConnectionError,
            TimeoutError,
            OSError,
        ) as exc:
            logger.debug("logbook entity-name lookup failed: %r", exc)
            return {}, [
                "Entity names could not be fetched from Home Assistant; state "
                "rows show the object id instead of the friendly name."
            ]
        names: dict[str, str] = {}
        for state in states:
            if not isinstance(state, dict):
                continue
            entity = state.get("entity_id")
            if not isinstance(entity, str) or entity not in entity_ids:
                continue
            attributes = state.get("attributes")
            if isinstance(attributes, dict):
                friendly_name = attributes.get("friendly_name")
                if friendly_name:
                    names[entity] = str(friendly_name)
        return names, []

    @staticmethod
    def _normalize_logbook_events(events: Any, names: dict[str, str]) -> Any:
        """Normalize WS events onto the REST-shaped contract.

        ``logbook/get_events`` returns ``when`` as epoch seconds and omits the
        ``name`` field on state rows; the tool expects the REST shape (ISO
        timestamp, name on state rows). REST responses pass through unchanged.
        """
        if not isinstance(events, list):
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
            entity = row.get("entity_id")
            if isinstance(entity, str) and "state" in row and not row.get("name"):
                row["name"] = names.get(entity) or entity.split(".", 1)[-1].replace(
                    "_", " "
                )
            normalized.append(row)
        return normalized

    async def _attach_logbook_names(
        self, response: Any, transport: str
    ) -> tuple[Any, list[str]]:
        """Attach Core-resolved names to WS state rows; REST already has them."""
        if transport != "websocket" or not isinstance(response, list):
            return response, []
        state_entities = {
            row["entity_id"]
            for row in response
            if isinstance(row, dict)
            and isinstance(row.get("entity_id"), str)
            and "state" in row
        }
        names, warnings = await self._logbook_state_entity_names(state_entities)
        return self._normalize_logbook_events(response, names), warnings

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

            name_warnings: list[str] = []
            response, name_warnings = await self._attach_logbook_names(
                response, transport
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
            if name_warnings:
                # Top-level, alongside the other tool warnings: nesting these
                # under ``data`` would hide them from the warning merger.
                wrapped["warnings"] = name_warnings + wrapped.get("warnings", [])
            return wrapped

        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={
                    "period": f"{hours_back_int} hours back from {end_dt.isoformat()}",
                },
                suggestions=self._logbook_error_suggestions(str(e)),
            )
            raise  # unreachable: exception_to_structured_error always raises
