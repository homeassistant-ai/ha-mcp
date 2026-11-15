"""Core's config API for automations, scripts and scenes, on a real ``hass``.

configuration.yaml includes the file each config view edits, in the test's own
config directory; Core reloads the integration after every write.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantAPIError, HomeAssistantClient

FILES = {
    "automation": "automations.yaml",
    "script": "scripts.yaml",
    "scene": "scenes.yaml",
}


async def write_config_file(hass: HomeAssistant, name: str, content: Any) -> None:
    """Write ``content`` (text, or data as JSON, which is YAML) to Core's ``name``."""
    path = Path(hass.config.path(name))
    text = content if isinstance(content, str) else json.dumps(content)
    await hass.async_add_executor_job(path.parent.mkdir, 0o755, True, True)
    await hass.async_add_executor_job(path.write_text, text)


async def set_up_config_api(
    hass: HomeAssistant, included: tuple[str, ...] = tuple(FILES)
) -> None:
    """Core's config views, with only the ``included`` files read at a reload."""
    includes = "".join(f"{domain}: !include {FILES[domain]}\n" for domain in included)
    await write_config_file(hass, "configuration.yaml", includes)
    for domain, name in FILES.items():
        await write_config_file(hass, name, {} if domain == "script" else [])
    assert await async_setup_component(hass, "config", {})
    for domain in FILES:
        assert await async_setup_component(hass, domain, {})


async def load(hass: HomeAssistant, domain: str, items: Any) -> None:
    """Store ``items`` in ``domain``'s file and have Core reload it."""
    await write_config_file(hass, FILES[domain], items)
    await hass.services.async_call(domain, "reload", blocking=True)
    await hass.async_block_till_done()


async def error_status(read: Awaitable[Any]) -> int | None:
    """The status of Core's error reply to ``read`` (``None`` when it succeeds)."""
    try:
        await read
    except HomeAssistantAPIError as e:
        return e.status_code
    return None


@pytest.fixture
async def config_client(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> HomeAssistantClient:
    """``core_client`` on a ``hass`` whose config API edits all three files."""
    await set_up_config_api(hass)
    return core_client
