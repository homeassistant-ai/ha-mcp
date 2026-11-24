"""The exposure write fails: Core refuses it, the socket is gone before it
leaves, or it is sent and the connection drops."""

import contextlib
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.homeassistant import exposed_entities as core_exposed
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantCommandNotSent,
)

from ._command_faults import dropped_on, pooled_socket

_EXPOSE = "homeassistant/expose_entity"


def expose_raises(
    monkeypatch: pytest.MonkeyPatch, only_for: bool | None = None
) -> None:
    """Core's exposure write raises; ``only_for`` limits it to that value."""
    expose = core_exposed.async_expose_entity

    def expose_or_raise(
        hass: HomeAssistant, assistant: str, entity_id: str, value: bool
    ) -> None:
        if only_for in (None, value):
            raise HomeAssistantError("boom")
        expose(hass, assistant, entity_id, value)

    monkeypatch.setattr(core_exposed, "async_expose_entity", expose_or_raise)


async def expose_fault(
    fault: str,
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    core_client: HomeAssistantClient,
    updates: int,
) -> contextlib.AbstractContextManager[None]:
    """The exposure write fails after ``updates`` registry updates landed:
    Core refuses it, the socket is gone before it leaves, or it is sent and
    the connection drops."""
    if fault == "refused":
        expose_raises(monkeypatch)
    elif fault == "not_sent":
        update = er.EntityRegistry.async_update_entity
        done: list[str] = []

        def update_then_lose_socket(
            registry: er.EntityRegistry, entity_id: str, **changes: Any
        ) -> Any:
            result = update(registry, entity_id, **changes)
            done.append(entity_id)
            if len(done) == updates:
                monkeypatch.setattr(
                    core_client,
                    "send_websocket_message",
                    AsyncMock(side_effect=HomeAssistantCommandNotSent("not sent")),
                )
            return result

        monkeypatch.setattr(
            er.EntityRegistry, "async_update_entity", update_then_lose_socket
        )
    else:
        return dropped_on(hass, _EXPOSE, await pooled_socket(core_client))
    return contextlib.nullcontext()
