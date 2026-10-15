"""The ``dashboard_cards`` websocket command: the card type list, or one card
type's form, from ``card_definitions``."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from . import card_definitions as cd

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


def _no_definitions() -> dict[str, Any]:
    """The first describe after a Core start builds the frontend index; on a
    slow board that outlasts the command wait (#2744), so say it is still
    building instead of letting the transport time out."""
    task = cd._build_task
    loading = cd._definitions is None and task is not None and not task.done()
    error = "definitions_loading" if loading else "unavailable"
    return {"result": {"success": False, "error": error}}


async def _describe(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    definitions = await cd.async_get_definitions(hass, timeout=cd._CARD_WORK_SECONDS)
    if definitions is None:
        return _no_definitions()
    card_type = msg.get("card_type") or None
    custom = (
        await cd.async_get_custom_cards(hass, timeout=cd._CUSTOM_WAIT_SECONDS)
        if card_type is None or card_type.startswith("custom:")
        else None
    )
    card_types = definitions.card_types() + (custom.card_types() if custom else [])
    status = cd.custom_load_status(custom)
    if card_type is None:
        return {
            "result": {
                "success": True,
                "card_types": card_types,
                "custom_status": status,
            }
        }
    if card_type.startswith("custom:"):
        if status["state"] == "loading":
            return {
                "result": {
                    "success": False,
                    "error": "custom_cards_loading",
                    "custom_status": status,
                }
            }
        describe = custom.describe if custom else lambda _tag: None
        described = await hass.async_add_executor_job(
            describe, card_type[len("custom:") :]
        )
    else:
        described = await hass.async_add_executor_job(definitions.describe, card_type)
    if described is None:
        if card_type.startswith("custom:"):
            return {
                "result": {
                    "success": False,
                    "error": "custom_card_not_inspected",
                    "custom_status": status,
                }
            }
        return {
            "result": {
                "success": False,
                "error": "unknown_card_type",
                "card_types": [c["type"] for c in card_types],
            }
        }
    return {"result": {"success": True, **described}}


def command_specs(vol: Any) -> list[tuple[dict[Any, Any], Any, Any]]:
    """``dashboard_cards``: the card type list, or one card type's form."""

    async def prep(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
        try:
            async with asyncio.timeout(cd._CARD_WORK_SECONDS):
                return await _describe(hass, msg)
        except TimeoutError:
            if cd._definitions is not None:  # the index is loaded; describe was slow
                return {"result": {"success": False, "error": "describe_timeout"}}
            return _no_definitions()

    def do(
        hass: HomeAssistant, msg: dict[str, Any], *, result: dict[str, Any]
    ) -> dict[str, Any]:
        return result

    return [
        (
            {
                vol.Required("type"): cd.WS_DASHBOARD_CARDS,
                vol.Optional("card_type"): str,
            },
            do,
            prep,
        )
    ]
