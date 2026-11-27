"""The energy meters Settings > Energy suggests, for ``ha_mcp_tools/energy_suggestions``.

From Home Assistant 2026.12, Settings > Energy lists the energy, gas and water
meters that are not in the Energy dashboard yet. Only the frontend computes
that list (``computeEnergySuggestions`` in ``src/data/energy_suggestions.ts``),
so the installed frontend's own function runs here on the data it reads in the
browser: the recorder's sum statistics, the energy preferences, the states and
the statistics the user dismissed. Its slot map (``ENERGY_SUGGESTION_EDITORS``)
names the field of a new source or device each suggested slot fills.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import HomeAssistantError

from . import card_definitions as cd

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .frontend_build import FrontendBuild

WS_ENERGY_SUGGESTIONS = "ha_mcp_tools/energy_suggestions"
CAPABILITY = "energy_suggestions"
# The suggestion objects the function builds and the metadata field it filters
# on; the module that holds both is energy_suggestions.ts.
_MARKERS = ('tab:"electricity"', ".has_sum")


def _suggest(build: FrontendBuild, data: dict[str, Any]) -> dict[str, Any]:
    module_id = build.module_containing(*_MARKERS)
    if module_id is None:
        return {
            "status": "unavailable",
            "reason": "The installed frontend suggests no energy meters "
            "(Settings > Energy does from Home Assistant 2026.12).",
        }
    exported = build.module_exports(module_id)
    compute = [n for n, d in exported.items() if all(m in d for m in _MARKERS)]
    editors = [n for n, d in exported.items() if d.startswith("{") and "kind:" in d]
    if len(compute) != 1 or len(editors) != 1:
        # A refused read is a missing component to the server: an update that
        # reads the frontend's new layout is the fix.
        raise HomeAssistantError(
            "The installed frontend's energy meter suggestions were not found"
        )
    return {
        "status": "available",
        "suggestions": build.run_export(module_id, compute[0], data),
        "dismissed": data["dismissed"],
        "editors": build.run_export(module_id, editors[0]),
    }


async def _inputs(hass: HomeAssistant, user_id: str | None) -> dict[str, Any]:
    """What Settings > Energy hands the function, read from Core."""
    from homeassistant.components.energy.data import EnergyManager, async_get_manager
    from homeassistant.components.frontend.storage import async_user_store
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import list_statistic_ids
    from homeassistant.util.json import json_loads

    manager = await async_get_manager(hass)
    statistics = await get_instance(hass).async_add_executor_job(
        list_statistic_ids, hass, None, "sum"
    )
    dismissed: list[str] = []
    if user_id is not None:
        store = await async_user_store(hass, user_id)
        dismissed = (store.data.get("energy") or {}).get("dismissed_suggestions", [])
    return {
        "statistics": statistics,
        "preferences": manager.data or EnergyManager.default_preferences(),
        "states": {
            state.entity_id: json_loads(state.as_dict_json)
            for state in hass.states.async_all()
        },
        "dismissed": dismissed,
    }


def command_specs(vol: Any) -> list[tuple[dict[Any, Any], Any, Any]]:
    """The (schema, handler, async prep) row websocket_api registers."""

    async def prep(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
        async with asyncio.timeout(cd.CARD_WORK_SECONDS):
            build = await cd.async_get_definitions(hass, timeout=15)
            if build is None:
                raise HomeAssistantError(
                    "The installed frontend's code could not be loaded"
                )
            data = await _inputs(hass, msg["ha_mcp_context"].user_id)
            result: dict[str, Any] = await hass.async_add_executor_job(
                _suggest, build, data
            )
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
