"""Services that switch entity states on a real ``hass``, as a device would.

The handlers are registered in Core's ``hass.services`` and the entities are
states only, so every call, event and state the tools see is Core's. Each
target follows the call by default; ``behaviour`` makes one ``stay`` as it is,
settle ``off`` whatever was asked, be ``remove``d, or ``fail_once`` before it
follows.
"""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.json import JsonValueType

_SERVICES = (
    ("light", "turn_on"),
    ("light", "turn_off"),
    ("switch", "turn_on"),
    ("number", "set_value"),
)


class SwitchedStates:
    """The services of ``_SERVICES``; ``calls`` holds every call Core made."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self.behaviour: dict[str, str] = {}
        self.calls: list[ServiceCall] = []
        for domain, service in _SERVICES:
            hass.services.async_register(
                domain,
                service,
                self._handle,
                supports_response=SupportsResponse.OPTIONAL,
            )

    def _targets(self, call: ServiceCall) -> list[str]:
        raw = call.data.get("entity_id") or []
        listed = raw.split(",") if isinstance(raw, str) else list(raw)
        return [entity_id.strip() for entity_id in listed if entity_id.strip()]

    def _new_state(self, call: ServiceCall) -> str:
        if call.service == "set_value":
            return str(call.data["value"])
        return "on" if call.service == "turn_on" else "off"

    def _apply(self, call: ServiceCall, entity_id: str) -> None:
        how = self.behaviour.get(entity_id, "follow")
        if how == "fail_once":
            self.behaviour[entity_id] = "follow"
            raise HomeAssistantError(f"{entity_id} did not answer")
        if how == "stay" or entity_id == "all":
            return
        if how == "remove":
            self._hass.states.async_remove(entity_id)
            return
        state = "off" if how == "off" else self._new_state(call)
        attributes: dict[str, Any] = {
            key: value
            for key, value in call.data.items()
            if key not in ("entity_id", "value")
        }
        self._hass.states.async_set(entity_id, state, attributes)

    async def _handle(self, call: ServiceCall) -> ServiceResponse:
        self.calls.append(call)
        targets = self._targets(call)
        for entity_id in targets:
            self._apply(call, entity_id)
        changed: list[JsonValueType] = list(targets)
        return {"changed": changed} if call.return_response else None


def set_later(hass: HomeAssistant, entity_id: str, state: str) -> asyncio.Task[None]:
    """Move an entity to ``state`` shortly after its service call returned."""

    async def _set() -> None:
        await asyncio.sleep(0.05)
        hass.states.async_set(entity_id, state)

    return hass.async_create_task(_set())
