"""Core set-ups for the automation config tool tests on the component-absent path."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.utils import skill_loader

from ._receipt_faults import on_receipt

_LOADS_AUTOMATIONS = (
    "automation: !include automations.yaml\nscript: !include scripts.yaml\n"
)
_LOADS_NO_AUTOMATIONS = "automation: []\nscript: !include scripts.yaml\n"


def require_vendored_skills() -> Path:
    """Skip the test cleanly when the submodule isn't initialised."""
    skills_dir = skill_loader.get_skills_dir()
    if (
        skills_dir is None
        or not (skills_dir / "home-assistant-best-practices").is_dir()
    ):
        pytest.skip(
            "skills-vendor submodule not initialised; "
            "run `git submodule update --init` to enable real-skill tests"
        )
    return skills_dir


async def _write(hass: HomeAssistant, name: str, text: str) -> None:
    await hass.async_add_executor_job(Path(hass.config.path(name)).write_text, text)


async def store_unloaded(hass: HomeAssistant, items: list[dict[str, Any]]) -> None:
    """Put ``items`` in automations.yaml without a reload: Core's config API
    reads them, but no automation entity exists for them."""
    await _write(hass, "automations.yaml", json.dumps(items))


async def break_automations_file(hass: HomeAssistant) -> None:
    """A hand edit leaves automations.yaml unparsable: Core's config API fails
    every read of it."""
    await _write(hass, "automations.yaml", "- id: [unclosed\n")


async def stop_loading_automations(hass: HomeAssistant) -> None:
    """configuration.yaml stops including automations.yaml, which Core's config
    API still writes: a saved automation then never gets an entity."""
    await _write(hass, "configuration.yaml", _LOADS_NO_AUTOMATIONS)


async def _load_automations_again(hass: HomeAssistant) -> None:
    await _write(hass, "configuration.yaml", _LOADS_AUTOMATIONS)
    await hass.services.async_call("automation", "reload", blocking=True)


@contextmanager
def loaded_at_second_entity_wait(hass: HomeAssistant) -> Iterator[None]:
    """When a second wait for a new automation entity subscribes, Core loads
    automations.yaml again (after ``stop_loading_automations``)."""
    waits = 0

    def on_wait(msg: dict[str, Any]) -> None:
        nonlocal waits
        waits += 1
        if waits == 2:
            hass.async_create_task(_load_automations_again(hass))

    with on_receipt(
        hass,
        "subscribe_events",
        on_wait,
        when=lambda msg: msg.get("event_type") == "state_changed",
    ):
        yield


@contextmanager
def config_reads_lost_at_entity_wait(
    hass: HomeAssistant, client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Once a write's wait for its entity subscribes, the client's automation
    config reads get no answer."""

    def lose(msg: dict[str, Any]) -> None:
        monkeypatch.setattr(
            client,
            "get_automation_config",
            AsyncMock(side_effect=HomeAssistantConnectionError("network down")),
        )

    with on_receipt(
        hass,
        "subscribe_events",
        lose,
        when=lambda msg: msg.get("event_type") == "state_changed",
    ):
        yield


@contextmanager
def deletes(client: HomeAssistantClient) -> Iterator[AsyncMock]:
    """Spy on the automation delete; Core still answers it."""
    with patch.object(
        client, "delete_automation_config", wraps=client.delete_automation_config
    ) as spy:
        yield spy


async def with_related_and_repairs(hass: HomeAssistant) -> None:
    """Core's ``search/related`` and ``repairs/list_issues`` commands answer."""
    assert await async_setup_component(hass, "search", {})
    assert await async_setup_component(hass, "repairs", {})
