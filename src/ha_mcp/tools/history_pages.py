"""Recorder pages through the component, or Core's unpaged WebSocket read."""

from typing import Any

from ..client.rest_client import HomeAssistantConnectionError
from ..errors import ErrorCode, create_error_response, create_validation_error
from .coercion import parse_string_list_param
from .component_api import ask_component, component_unusable_warning, get_component_caps
from .helpers import raise_core_refusal, raise_tool_error
from .history_response import summarize_states
from .recorder_errors import raise_recorder_refusal


def history_fields(fields: str | list[str] | None) -> list[str] | None:
    try:
        return (
            parse_string_list_param(fields, "fields", allow_csv=True)
            if fields is not None
            else None
        )
    except ValueError as exc:
        raise_tool_error(create_validation_error(str(exc), parameter="fields"))


async def resolve_entities(
    client: Any, ids: str | list[str] | None, target: dict[str, Any] | None
) -> list[str]:
    if target is None:
        return _parse_entity_ids(ids or [])
    selected = _parse_entity_ids(ids) if ids is not None else []
    expanded = await target_entities(client, target)
    return _parse_entity_ids(list(dict.fromkeys([*selected, *expanded])))


def _parse_entity_ids(entity_ids: str | list[str]) -> list[str]:
    """Parse entity_ids parameter into a list of strings."""
    if isinstance(entity_ids, str):
        if entity_ids.startswith("["):
            # Belt-and-suspenders: JSON_STRING_COERCION on the param already
            # parses a JSON-array string to a list upstream, so a string reaching
            # here is normally CSV/single. This branch stays as a fallback.
            parsed_ids = parse_string_list_param(entity_ids, "entity_ids")
            if parsed_ids is None:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_MISSING_PARAMETER,
                        "entity_ids is required",
                        suggestions=["Provide at least one entity ID"],
                    )
                )
            return parsed_ids
        elif "," in entity_ids:
            result = [e.strip() for e in entity_ids.split(",") if e.strip()]
            if not result:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_MISSING_PARAMETER,
                        "entity_ids is required",
                        suggestions=["Provide at least one entity ID"],
                    )
                )
            return result
        else:
            return [entity_ids.strip()]
    if not entity_ids:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_MISSING_PARAMETER,
                "entity_ids is required",
                suggestions=["Provide at least one entity ID"],
            )
        )

    return entity_ids


async def target_entities(client: Any, target: dict[str, Any]) -> list[str]:
    reply = await client.send_websocket_message(
        {"type": "extract_from_target", "target": target}
    )
    if not reply.get("success"):
        raise_core_refusal("extract_from_target failed", reply)
    return sorted(reply["result"]["referenced_entities"])


async def history_pages(
    client: Any,
    query: dict[str, Any],
    limit: int,
    offset: int,
    order: str,
    start: float,
    until: float | None,
) -> tuple[dict[str, Any], list[str]]:
    options = {"summary_until": until} if until is not None else {}
    try:
        answer = await ask_component(
            client,
            "history_page",
            query=query,
            limit=limit,
            offset=offset,
            order=order,
            **options,
        )
    except HomeAssistantConnectionError:
        answer = None
    if answer is not None and isinstance(answer.get("entities"), dict):
        return answer["entities"], []
    reply = await client.send_websocket_message(
        {"type": "history/history_during_period", **query}
    )
    if not reply.get("success"):
        raise_recorder_refusal("history", reply, query["entity_ids"])
    pages: dict[str, Any] = {}
    for entity_id, all_rows in reply["result"].items():
        rows = all_rows
        if until is not None:
            rows = [row for row in all_rows if row["lu"] < until]
            page = {"summary": summarize_states(rows, start, until, limit)}
        else:
            ordered = rows if order == "asc" else rows[::-1]
            page = {
                "states": ordered[offset : offset + limit],
                "total_count": len(rows),
            }
        pages[entity_id] = {**page, "first": rows[0]["lu"] if rows else None}
    warning = component_unusable_warning(
        await get_component_caps(client), "the full history window was downloaded"
    )
    return pages, [warning]
