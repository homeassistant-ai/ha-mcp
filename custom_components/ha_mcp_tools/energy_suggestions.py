"""The energy meters Settings > Energy suggests, for ``ha_mcp_tools/energy_suggestions``.

From Home Assistant 2026.12, Settings > Energy lists the energy, gas and water
meters that are not in the Energy dashboard yet, and lets the user dismiss and
restore them. Only the frontend computes that list and the dismissed list
(``src/data/energy_suggestions.ts``), so the installed frontend's own functions
run here on the data it reads in the browser: the recorder's sum statistics,
the energy preferences, the states and the statistics the user dismissed. A
dismissal is saved where the frontend saves it, the user's ``energy`` frontend
data. The slot map (``ENERGY_SUGGESTION_EDITORS``) names the field of a new
source or device each suggested slot fills.
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
_LOCKS = "ha_mcp_tools_energy_suggestion_locks"
# The suggestion panel subscribes to the states of the statistics this filter
# keeps; other energy panels set the same field.
_PANEL_MARKERS = ("this._statisticIds=", "dismissed_suggestions")
_PANEL_FILTER_RE = re.compile(r"this\._statisticIds=[\w$]+\.filter\(")


def _exports(build: FrontendBuild) -> tuple[str, dict[str, str]] | None:
    """The module and its exports: compute, editors, update and restore."""
    module_id = build.module_containing(*_MARKERS)
    if module_id is None:
        return None
    exported = build.module_exports(module_id)
    functions = {n: d for n, d in exported.items() if "=>" in d.split("{", 1)[0]}
    found = {
        "compute": [n for n, d in functions.items() if all(m in d for m in _MARKERS)],
        "editors": [
            n for n, d in exported.items() if d.startswith("{") and "kind:" in d
        ],
        # Besides compute, the only one that reads the recorder's statistic_id:
        # it drops dismissed ids the recorder no longer lists.
        "update": [
            n
            for n, d in functions.items()
            if ".statistic_id" in d and ".has_sum" not in d
        ],
    }
    found["restore"] = [
        n for n in functions if n not in found["compute"] + found["update"]
    ]
    if any(len(names) != 1 for names in found.values()):
        # The frontend changed its layout; a component update reads it.
        raise HomeAssistantError(
            "The installed frontend's energy meter suggestions were not found"
        )
    return module_id, {role: names[0] for role, names in found.items()}


def _panel_states(build: FrontendBuild, data: dict[str, Any]) -> dict[str, Any]:
    """The states Settings > Energy hands the function: those of the statistics
    its panel keeps, chosen by the panel's own filter."""
    module_id = build.module_containing(*_PANEL_MARKERS)
    body = build.module_body(module_id) if module_id else ""
    found = _PANEL_FILTER_RE.search(body)
    if module_id is None or found is None:
        raise HomeAssistantError(
            "The installed frontend's energy meter suggestions were not found"
        )
    keep = body[found.end() : _balanced_end(body, found.end() - 1) - 1]
    kept = build.run_expression(module_id, f"s=>s.filter({keep})", data["statistics"])
    ids = {meta["statistic_id"] for meta in kept}
    return {k: v for k, v in data["states"].items() if k in ids}


def _dismissed_after(
    build: FrontendBuild,
    module_id: str,
    names: dict[str, str],
    data: dict[str, Any],
    msg: dict[str, Any],
) -> list[str]:
    """The dismissed list after the frontend's own dismiss and restore steps."""
    dismissed: list[str] = data["dismissed"]
    if ids := msg.get("dismiss"):
        updated = build.run_export(
            module_id, names["update"], dismissed, ids, data["statistics"]
        )
        if any(isinstance(item, list) for item in updated):
            # Frontends before "Dismiss all" (frontend #56198) dismiss one
            # statistic per call.
            for statistic_id in ids:
                dismissed = build.run_export(
                    module_id,
                    names["update"],
                    dismissed,
                    statistic_id,
                    data["statistics"],
                )
        else:
            dismissed = updated
    for statistic_id in msg.get("restore") or []:
        dismissed = build.run_export(
            module_id, names["restore"], dismissed, statistic_id
        )
    return dismissed


def _suggest(
    build: FrontendBuild, data: dict[str, Any], msg: dict[str, Any]
) -> dict[str, Any]:
    located = _exports(build)
    if located is None:
        reason = (
            "The installed frontend suggests no energy meters "
            "(Settings > Energy does from Home Assistant 2026.12)."
        )
        if msg.get("dismiss") or msg.get("restore"):
            raise HomeAssistantError(reason)
        return {"status": "unavailable", "reason": reason}
    module_id, names = located
    data = {**data, "states": _panel_states(build, data)}
    result: dict[str, Any] = {
        "status": "available",
        "suggestions": build.run_export(module_id, names["compute"], data),
        "dismissed": data["dismissed"],
        "editors": build.run_export(module_id, names["editors"]),
    }
    # The panel dismisses only a meter it suggests and restores only one it
    # lists as dismissed; anything else changes nothing and is reported.
    suggested = {s["statisticId"] for s in result["suggestions"]}
    unknown = [
        *(
            i
            for i in msg.get("dismiss") or []
            if i not in suggested | {*data["dismissed"]}
        ),
        *(i for i in msg.get("restore") or [] if i not in data["dismissed"]),
    ]
    if unknown:
        return {**result, "unknown": unknown}
    if msg.get("dismiss") or msg.get("restore"):
        dismissed = _dismissed_after(build, module_id, names, data, msg)
        result["dismissed"] = dismissed
        result["suggestions"] = build.run_export(
            module_id, names["compute"], {**data, "dismissed": dismissed}
        )
    return result


async def _inputs(hass: HomeAssistant, user_id: str) -> dict[str, Any]:
    """What Settings > Energy hands the functions, read from Core."""
    manager = await async_get_manager(hass)
    statistics = await get_instance(hass).async_add_executor_job(
        list_statistic_ids, hass, None, "sum"
    )
    store = await async_user_store(hass, user_id)
    return {
        "statistics": statistics,
        "preferences": manager.data or EnergyManager.default_preferences(),
        "states": {
            state.entity_id: json_loads(state.as_dict_json)
            for state in hass.states.async_all()
        },
        "dismissed": (store.data.get(_USER_DATA_KEY) or {}).get(
            "dismissed_suggestions", []
        ),
    }


def command_specs(vol: Any) -> list[tuple[dict[Any, Any], Any, Any]]:
    """The (schema, handler, async prep) row websocket_api registers."""

    async def prep(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
        user_id = msg["ha_mcp_context"].user_id
        # A change reads and saves the user's whole list; one at a time per
        # user, as the panel queues its saves.
        lock = hass.data.setdefault(_LOCKS, {}).setdefault(user_id, asyncio.Lock())
        async with lock, asyncio.timeout(cd.CARD_WORK_SECONDS):
            build = await cd.async_get_definitions(hass, timeout=15)
            if build is None:
                raise HomeAssistantError(
                    "The installed frontend's code could not be loaded"
                )
            data = await _inputs(hass, user_id)
            result: dict[str, Any] = await hass.async_add_executor_job(
                _suggest, build, data, msg
            )
            if result.get("dismissed", data["dismissed"]) != data["dismissed"]:
                # The panel saves the whole energy user data as this one list.
                store = await async_user_store(hass, user_id)
                await store.async_set_item(
                    _USER_DATA_KEY, {"dismissed_suggestions": result["dismissed"]}
                )
        return {"result": result}

    def do_energy_suggestions(
        hass: HomeAssistant, msg: dict[str, Any], *, result: dict[str, Any]
    ) -> dict[str, Any]:
        return result

    return [
        (
            {
                vol.Required("type"): WS_ENERGY_SUGGESTIONS,
                vol.Optional("dismiss"): vol.All([str], vol.Length(min=1)),
                vol.Optional("restore"): vol.All([str], vol.Length(min=1)),
            },
            do_energy_suggestions,
            prep,
        )
    ]
