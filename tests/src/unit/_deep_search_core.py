"""Automations, scripts and scenes on a real ``hass`` for the deep-search tests.

Core's per-id config views read ``automations.yaml``, ``scripts.yaml`` and
``scenes.yaml`` on every request. An item Core loaded that is also in its file
is a stored one; one left out of the file is YAML-defined, and the view
answers 404 for it. A file holding a ``!secret`` makes the view answer 500 for
every id, as on the install behind #1784's follow-up report.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.smart_search import INDIVIDUAL_CONFIG_TIMEOUT, SmartSearchTools

AUTOMATIONS_FILE = "automations.yaml"
SCRIPTS_FILE = "scripts.yaml"
SCENES_FILE = "scenes.yaml"


def automation(uid: str, alias: str, **body: Any) -> dict[str, Any]:
    """An automation config; ``body`` adds or replaces its sections."""
    return {
        "id": uid,
        "alias": alias,
        "triggers": [{"trigger": "event", "event_type": "deep_search_test"}],
        "actions": [{"event": "deep_search_test_ran"}],
        **body,
    }


def script(alias: str, *steps: dict[str, Any]) -> dict[str, Any]:
    return {"alias": alias, "sequence": list(steps) or [{"event": "deep_search_test"}]}


def scene(
    uid: str, name: str, entities: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {"id": uid, "name": name, "entities": entities or {"light.desk": "on"}}


async def write_config_file(hass: HomeAssistant, name: str, text: str) -> None:
    """Write ``name`` and make sure Core's config views serve it."""
    await hass.async_add_executor_job(Path(hass.config.path(name)).write_text, text)
    assert await async_setup_component(hass, "config", {})


async def write_secret_reference(hass: HomeAssistant, name: str) -> None:
    """Make ``name`` hold a ``!secret``, which the per-id config views reject."""
    item = "- id: secret_item\n  alias: !secret item_alias\n"
    if name == SCRIPTS_FILE:
        item = "secret_item:\n  alias: !secret item_alias\n"
    await write_config_file(hass, name, item)


async def load_automations(
    hass: HomeAssistant,
    stored: list[dict[str, Any]],
    yaml_only: list[dict[str, Any]] | None = None,
) -> None:
    """Have Core load ``stored`` (also written to its file) and ``yaml_only``."""
    await write_config_file(hass, AUTOMATIONS_FILE, json.dumps(stored))
    items = [*stored, *(yaml_only or [])]
    assert await async_setup_component(hass, "automation", {"automation": items})
    await hass.async_block_till_done()


async def load_scripts(
    hass: HomeAssistant,
    stored: dict[str, dict[str, Any]],
    yaml_only: dict[str, dict[str, Any]] | None = None,
) -> None:
    await write_config_file(hass, SCRIPTS_FILE, json.dumps(stored))
    items = {**stored, **(yaml_only or {})}
    assert await async_setup_component(hass, "script", {"script": items})
    await hass.async_block_till_done()


async def load_scenes(
    hass: HomeAssistant,
    stored: list[dict[str, Any]],
    yaml_only: list[dict[str, Any]] | None = None,
) -> None:
    await write_config_file(hass, SCENES_FILE, json.dumps(stored))
    items = [*stored, *(yaml_only or [])]
    assert await async_setup_component(hass, "scene", {"scene": items})
    await hass.async_block_till_done()


def stepping_clock(*readings: float) -> Callable[[], float]:
    """A clock that reads ``readings`` in turn, then keeps the last one."""
    remaining = list(readings)

    def read() -> float:
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    return read


def search_tools(
    client: HomeAssistantClient,
    *,
    clock: Callable[[], float] | None = None,
    config_timeout: float = INDIVIDUAL_CONFIG_TIMEOUT,
) -> SmartSearchTools:
    """The search tools on ``client``; ``config_timeout=0`` times out every
    per-id fetch before it is sent."""
    return SmartSearchTools(client=client, clock=clock, config_timeout=config_timeout)


@asynccontextmanager
async def client_without_patience(
    core_client: HomeAssistantClient,
) -> AsyncIterator[HomeAssistantClient]:
    """A client of the same Core whose own HTTP timeout is zero, so every REST
    request times out in the transport."""
    client = HomeAssistantClient(
        base_url=core_client.base_url,
        token=core_client.token,
        verify_ssl=False,
        timeout=0,
    )
    try:
        yield client
    finally:
        await client.close()
