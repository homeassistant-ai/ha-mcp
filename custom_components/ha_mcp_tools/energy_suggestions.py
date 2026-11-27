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
from typing import TYPE_CHECKING, Any

from homeassistant.components.energy.data import EnergyManager, async_get_manager
from homeassistant.components.frontend.storage import async_user_store
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import list_statistic_ids
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.json import json_loads

from . import card_definitions as cd

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .frontend_build import FrontendBuild

WS_ENERGY_SUGGESTIONS = "ha_mcp_tools/energy_suggestions"
CAPABILITY = "energy_suggestions"
# The suggestion objects the function builds and the metadata field it filters
# on; the module that holds both is energy_suggestions.ts.
_MARKERS = ('tab:"electricity"', ".has_sum")
_USER_DATA_KEY = "energy"


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
        # Drops dismissed statistics that no longer exist: it reads their ids.
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
        # A refused read is a missing component to the server: an update that
        # reads the frontend's new layout is the fix.
        raise HomeAssistantError(
            "The installed frontend's energy meter suggestions were not found"
        )
    return module_id, {role: names[0] for role, names in found.items()}


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
            # This build dismisses one statistic per call, as its panel does.
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
    dismissed = _dismissed_after(build, module_id, names, data, msg)
    return {
        "status": "available",
        "suggestions": build.run_export(
            module_id, names["compute"], {**data, "dismissed": dismissed}
        ),
        "dismissed": dismissed,
        "editors": build.run_export(module_id, names["editors"]),
    }


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
        async with asyncio.timeout(cd.CARD_WORK_SECONDS):
            build = await cd.async_get_definitions(hass, timeout=15)
            if build is None:
                raise HomeAssistantError(
                    "The installed frontend's code could not be loaded"
                )
            data = await _inputs(hass, user_id)
            result: dict[str, Any] = await hass.async_add_executor_job(
                _suggest, build, data, msg
            )
        if result["status"] == "available" and result["dismissed"] != data["dismissed"]:
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
