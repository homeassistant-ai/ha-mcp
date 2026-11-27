"""Page or summarize Core's compact history in the recorder executor."""

from __future__ import annotations

from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING, Any

from .core_contract import validate_request

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

CAPABILITY = "history_page"


def _page(
    entity_id: str, rows: list[dict[str, Any]], *, msg: dict[str, Any], start: float
) -> tuple[str, dict[str, Any]]:
    if (until := msg.get("summary_until")) is not None:
        from ha_mcp.tools.history_response import summarize_states

        rows = [row for row in rows if row["lu"] < until]
        data = {"summary": summarize_states(rows, start, until, msg["limit"])}
    else:
        offset, limit = msg["offset"], msg["limit"]
        if msg["order"] == "asc":
            page = rows[offset : offset + limit]
        else:
            end = max(0, len(rows) - offset)
            page = rows[max(0, end - limit) : end][::-1]
        data = {"states": page, "total_count": len(rows)}
    data["first"] = rows[0]["lu"] if rows else None
    return entity_id, data


def _read(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    from homeassistant.components.recorder import history
    from homeassistant.util import dt as dt_util

    params = dict(msg["query"])
    start = datetime.fromisoformat(params.pop("start_time"))
    end = datetime.fromisoformat(params.pop("end_time"))
    ids = params.pop("entity_ids")
    if start > dt_util.utcnow():
        return {}
    page = partial(_page, msg=msg, start=start.timestamp())
    if serialized := getattr(history, "get_significant_states_serialized", None):
        return dict(serialized(hass, start, end, ids, **params, serialize=page))
    # Core 2026.11 fallback: remove when the minimum Home Assistant is 2026.12.
    pages = {}
    for entity_id in ids:
        states = history.get_significant_states(
            hass, start, end, [entity_id], compressed_state_format=True, **params
        )
        if rows := states.get(entity_id):
            pages.update([page(entity_id, rows)])  # type: ignore[arg-type]
    return pages


async def _prepare(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    from homeassistant.core import valid_entity_id
    from homeassistant.helpers.recorder import get_instance

    query = msg["query"]
    contract = validate_request(hass, "history/history_during_period", query)
    if not contract.get("valid") or any(
        not hass.states.get(entity_id) and not valid_entity_id(entity_id)
        for entity_id in query["entity_ids"]
    ):
        return {"result": {}}
    pages = await get_instance(hass).async_add_executor_job(_read, hass, msg)
    return {"result": {"entities": pages}}


def _result(
    hass: HomeAssistant, msg: dict[str, Any], *, result: dict[str, Any]
) -> dict[str, Any]:
    return result


def command_specs(vol: Any) -> list[tuple[dict[Any, Any], Any, Any]]:
    return [
        (
            {
                vol.Required("type"): "ha_mcp_tools/history_page",
                vol.Required("query"): dict,
                vol.Required("limit"): vol.All(int, vol.Range(min=1, max=1000)),
                vol.Required("offset"): vol.All(int, vol.Range(min=0)),
                vol.Required("order"): vol.In(("asc", "desc")),
                vol.Optional("summary_until"): float,
            },
            _result,
            _prepare,
        )
    ]
