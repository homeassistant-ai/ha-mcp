"""Page or summarize Core's compact history in the recorder executor."""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from datetime import datetime
from functools import cache, partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from .core_contract import validate_request

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .websocket_api.seam import PreSerializedResult

CAPABILITY = "history_page"


@cache
def _summarize_states() -> Callable[..., dict[str, Any]]:
    """The server's summary, loaded from its file so its package is not imported."""
    package = importlib.util.find_spec("ha_mcp")
    if package is None or not package.submodule_search_locations:
        raise ImportError("the ha-mcp server package is not installed")
    path = Path(next(iter(package.submodule_search_locations))) / "history_summary.py"
    spec = importlib.util.spec_from_file_location("ha_mcp_tools_history_summary", path)
    if not path.is_file() or spec is None or spec.loader is None:
        # A server wheel from before the file existed: remove once the
        # manifest pins a release that has it.
        from ha_mcp.tools.history_response import summarize_states

        legacy: Callable[..., dict[str, Any]] = summarize_states
        return legacy
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    summarize: Callable[..., dict[str, Any]] = module.summarize_states
    return summarize


def _page(
    entity_id: str, rows: list[dict[str, Any]], *, msg: dict[str, Any], start: float
) -> tuple[str, dict[str, Any]]:
    if (until := msg.get("summary_until")) is not None:
        # Core can answer past the clock reading the summary ends at.
        rows = [row for row in rows if row["lu"] < until]
        data: dict[str, Any] = {
            "summary": _summarize_states()(rows, start, until, msg["limit"])
        }
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


def _encoded(msg: dict[str, Any], result: dict[str, Any]) -> bytes:
    from homeassistant.components.websocket_api import messages
    from homeassistant.helpers.json import json_bytes

    payload: bytes = json_bytes(messages.result_message(msg["id"], result))
    return payload


def _read_encoded(hass: HomeAssistant, msg: dict[str, Any]) -> bytes:
    """Read and encode in the recorder executor, as Core's history command does."""
    return _encoded(msg, {"entities": _read(hass, msg)})


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
    pages: dict[str, Any] = {}
    for entity_id in ids:
        states = history.get_significant_states(
            hass, start, end, [entity_id], compressed_state_format=True, **params
        )
        if entity_id in states:
            rows = cast(list[dict[str, Any]], states.pop(entity_id))
            _, pages[entity_id] = page(entity_id, rows)
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
        # No pages: the server then asks Core's own command, whose refusal it returns.
        return {"payload": _encoded(msg, {})}
    return {
        "payload": await get_instance(hass).async_add_executor_job(
            _read_encoded, hass, msg
        )
    }


def _result(
    hass: HomeAssistant, msg: dict[str, Any], *, payload: bytes
) -> PreSerializedResult:
    from .websocket_api.seam import PreSerializedResult

    return PreSerializedResult(payload=payload)


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
