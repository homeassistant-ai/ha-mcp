"""Real-Core setup shared by the enforce-mode concealment tests.

The hidden scripts, scenes and automations are real entity-registry rows with
real states, so the hidden set the enforcement computes comes from Core's
registry and state machine read over the server's own client.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.visibility.model import VisibilityConfig


def register_entity(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    entity_id: str,
    unique_id: str,
    platform: str,
) -> er.RegistryEntry:
    """Create the registry row and state Core keeps for a loaded entity."""
    domain, _, object_id = entity_id.partition(".")
    entry = entity_registry.async_get_or_create(
        domain, platform, unique_id, suggested_object_id=object_id
    )
    assert (entry.entity_id, entry.unique_id) == (entity_id, unique_id)
    hass.states.async_set(entity_id, "on")
    return entry


def enforce_hiding(set_config: Callable[..., VisibilityConfig], *hidden: str) -> None:
    """Turn enforce mode on with ``hidden`` on the deny list."""
    set_config(enabled=True, enforce=True, deny_entity_ids=list(hidden))


def error_code(exc: pytest.ExceptionInfo[ToolError]) -> str:
    """The structured error code a refused or concealed call carries."""
    code: str = error_body(exc)["error"]["code"]
    return code


def error_body(exc: pytest.ExceptionInfo[ToolError]) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(exc.value.args[0])
    return body


async def unreached_call_next(_context: Any) -> Any:
    raise AssertionError("enforcement must refuse the call before the tool runs")
