"""The energy meters Settings > Energy suggests, for ``ha_mcp_tools/energy_suggestions``.

From Home Assistant 2026.12, Settings > Energy lists the energy, gas and water
meters that are not in the Energy dashboard yet. Only the frontend computes
that list (``src/data/energy_suggestions.ts``), so the installed frontend's own
function runs here on the data the panel hands it: the recorder's sum
statistics, the energy preferences, the states of the meters the panel keeps
and the statistics the user dismissed. Dismissals belong to each Home
Assistant user's own Settings > Energy, so this read reports the list of the
account the connection runs as and never changes it. The slot map
(``ENERGY_SUGGESTION_EDITORS``) names the field of a new source or device each
suggested slot fills.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any

from homeassistant.components.energy.data import EnergyManager, async_get_manager
from homeassistant.components.frontend.storage import async_user_store
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import list_statistic_ids
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.json import json_loads

from . import card_definitions as cd
from .frontend_build import _balanced_end

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .frontend_build import FrontendBuild

WS_ENERGY_SUGGESTIONS = "ha_mcp_tools/energy_suggestions"
CAPABILITY = "energy_suggestions"
# The suggestion objects the function builds and the metadata field it filters
# on; the module that holds both is energy_suggestions.ts.
_MARKERS = ('tab:"electricity"', ".has_sum")
_USER_DATA_KEY = "energy"
# The suggestion panel subscribes to the states of the statistics this filter
# keeps; other energy panels set the same field.
_PANEL_MARKERS = ("this._statisticIds=", "dismissed_suggestions")
_PANEL_FILTER_RE = re.compile(r"this\._statisticIds=[\w$]+\.filter\(")
_NOT_FOUND = "The installed frontend's energy meter suggestions were not found"


def _exports(build: FrontendBuild) -> tuple[str, str, str] | None:
    """The module, its compute export and its slot map export."""
    module_id = build.module_containing(*_MARKERS)
    if module_id is None:
        return None
    exported = build.module_exports(module_id)
    compute = [n for n, d in exported.items() if all(m in d for m in _MARKERS)]
    editors = [n for n, d in exported.items() if d.startswith("{") and "kind:" in d]
    if len(compute) != 1 or len(editors) != 1:
        # The frontend changed its layout; a component update reads it.
        raise HomeAssistantError(_NOT_FOUND)
    return module_id, compute[0], editors[0]


def _panel_states(build: FrontendBuild, data: dict[str, Any]) -> dict[str, Any]:
    """The states Settings > Energy hands the function: those of the statistics
    its panel keeps, chosen by the panel's own filter."""
    module_id = build.module_containing(*_PANEL_MARKERS)
    body = build.module_body(module_id) if module_id else ""
    found = _PANEL_FILTER_RE.search(body)
    if module_id is None or found is None:
        raise HomeAssistantError(_NOT_FOUND)
    keep = body[found.end() : _balanced_end(body, found.end() - 1) - 1]
    kept = build.run_expression(module_id, f"s=>s.filter({keep})", data["statistics"])
    ids = {meta["statistic_id"] for meta in kept}
    return {k: v for k, v in data["states"].items() if k in ids}


def _suggest(build: FrontendBuild, data: dict[str, Any]) -> dict[str, Any]:
    located = _exports(build)
    if located is None:
        return {
            "status": "unavailable",
            "reason": "The installed frontend suggests no energy meters "
            "(Settings > Energy does from Home Assistant 2026.12).",
        }
    module_id, compute, editors = located
    data = {**data, "states": _panel_states(build, data)}
    # The panel computes every eligible meter, then lists those its user
    # dismissed apart; a stored id it no longer suggests (now configured, or
    # gone) is listed nowhere.
    eligible = build.run_export(module_id, compute, {**data, "dismissed": []})
    shown = build.run_export(module_id, compute, data)
    shown_ids = {s["statisticId"] for s in shown}
    return {
        "status": "available",
        "suggestions": shown,
        "dismissed": [
            s["statisticId"] for s in eligible if s["statisticId"] not in shown_ids
        ],
        "editors": build.run_export(module_id, editors),
    }


async def _inputs(hass: HomeAssistant, user_id: str) -> dict[str, Any]:
    """What Settings > Energy hands the function, read from Core."""
    manager = await async_get_manager(hass)
    statistics = await get_instance(hass).async_add_executor_job(
        list_statistic_ids, hass, None, "sum"
    )
    # Shielded: a timeout stops this wait without cancelling Core's shared
    # load of the user's frontend data, which the browser may be awaiting.
    store = await asyncio.shield(async_user_store(hass, user_id))
    return {
        "statistics": statistics,
        "preferences": manager.data or EnergyManager.default_preferences(),
        "states": {
            state.entity_id: json_loads(state.as_dict_json)
            for state in hass.states.async_all()
        },
        "dismissed": (store.data.get(_USER_DATA_KEY) or {}).get("dismissed_suggestions")
        or [],
    }


def command_specs(vol: Any) -> list[tuple[dict[Any, Any], Any, Any]]:
    """The (schema, handler, async prep) row websocket_api registers."""

    async def prep(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
        user_id = msg["ha_mcp_context"].user_id
        async with asyncio.timeout(cd.CARD_WORK_SECONDS):
            build = await cd.async_get_definitions(hass, timeout=15)
            if build is None:
                raise HomeAssistantError(
                    "The installed frontend's code could not be loaded"
                )
            data = await _inputs(hass, user_id)
            result: dict[str, Any] = await hass.async_add_executor_job(
                _suggest, build, data
            )
        if result["status"] == "available":
            user = await hass.auth.async_get_user(user_id)
            result["dismissed_by"] = user.name if user else None
        return {"result": result}

    def do_energy_suggestions(
        hass: HomeAssistant, msg: dict[str, Any], *, result: dict[str, Any]
    ) -> dict[str, Any]:
        return result

    return [
        (
            {vol.Required("type"): WS_ENERGY_SUGGESTIONS},
            do_energy_suggestions,
            prep,
        )
    ]
