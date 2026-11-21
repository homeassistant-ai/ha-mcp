"""Automations on a real ``hass`` for the automation tool routing tests.

The tools run on the server's own client against Core's config views
(``_config_writes``). ``routing`` picks whether the component's
``ha_mcp_tools/*`` commands answer too, so one test can run on both paths.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.ha_mcp_tools import websocket_api
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_config_automations import AutomationConfigTools

from ._config_writes import automation_tools

COMPONENT = "component"
FALLBACK = "fallback"


async def routed_automation_tools(
    routing: str,
    hass: HomeAssistant,
    client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> AutomationConfigTools:
    """The automation tools, with the component's commands when ``routing`` asks."""
    if routing == COMPONENT:
        websocket_api.async_register_commands(hass)
    return await automation_tools(hass, client, monkeypatch)


async def load_automations_file(
    hass: HomeAssistant, name: str, items: list[dict[str, Any]]
) -> None:
    """Write ``items`` to ``name`` (JSON is YAML) and have Core reload automations."""
    await hass.async_add_executor_job(
        Path(hass.config.path(name)).write_text, json.dumps(items)
    )
    await hass.services.async_call("automation", "reload", blocking=True)
    await hass.async_block_till_done()


async def define_in_package(hass: HomeAssistant, item: dict[str, Any]) -> str:
    """Load ``item`` from a second ``automation`` key Core's config API never edits.

    Returns the entity_id Core registered for it.
    """
    configuration = (
        "automation: !include automations.yaml\n"
        "automation manual: !include manual.yaml\n"
        "script: !include scripts.yaml\n"
    )
    await hass.async_add_executor_job(
        Path(hass.config.path("configuration.yaml")).write_text, configuration
    )
    await load_automations_file(hass, "manual.yaml", [item])
    return entity_id_of(hass, str(item["id"]))


def entity_id_of(hass: HomeAssistant, unique_id: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        "automation", "automation", unique_id
    )
    assert entity_id is not None, unique_id
    return entity_id


@contextmanager
def upserts(client: HomeAssistantClient) -> Iterator[AsyncMock]:
    """Spy on the automation write; Core still answers it."""
    with patch.object(
        client, "upsert_automation_config", wraps=client.upsert_automation_config
    ) as spy:
        yield spy


@contextmanager
def config_reads(client: HomeAssistantClient) -> Iterator[AsyncMock]:
    """Spy on the automation config read; Core still answers it."""
    with patch.object(
        client, "get_automation_config", wraps=client.get_automation_config
    ) as spy:
        yield spy
