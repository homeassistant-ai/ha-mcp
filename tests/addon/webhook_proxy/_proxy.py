"""Helpers for setting the proxy up in a real ``hass`` and relaying to it."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from homeassistant import loader
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from ._variants import Variant

STATE_ROOTS = ("/config", "/data")


def state_path(fs_root: Path, path: str | PurePath) -> Path:
    """The redirected location of an absolute ``/config`` or ``/data`` path."""
    return fs_root / str(path).lstrip("/")


def proxy_config_path(fs_root: Path, variant: Variant) -> Path:
    return state_path(fs_root, variant.config_file)


def write_proxy_config(fs_root: Path, variant: Variant, config: dict[str, Any]) -> None:
    """Write the file ``start.py`` hands the component."""
    proxy_config_path(fs_root, variant).write_text(json.dumps(config))


async def set_up_proxy(hass: HomeAssistant, variant: Variant) -> MockConfigEntry:
    """Add the proxy's config entry and let Core set it up.

    Returns the entry whatever the outcome; ``entry.state`` and
    ``entry.reason`` say how setup went.
    """
    hass.data.pop(loader.DATA_CUSTOM_COMPONENTS, None)
    assert await async_setup_component(hass, "persistent_notification", {})
    entry = MockConfigEntry(domain=variant.domain, title="MCP Proxy")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@dataclass
class UpstreamRequest:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


@dataclass
class Upstream:
    """A loopback stand-in for the ha-mcp server app the proxy relays to.

    The server is this repository's own collaborator, not Home Assistant, so
    a test may decide what it answers by replacing ``handler``.
    """

    base_url: str
    requests: list[UpstreamRequest] = field(default_factory=list)
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]] | None = None

    def target_url(self, secret: str = "private_zctpwlX7ZkIAr7oqdfLPxw") -> str:
        """A target URL whose secret path passes the component's validation."""
        return f"{self.base_url}/{secret}"


def record_client_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> list[aiohttp.ClientSession]:
    """Every ``aiohttp.ClientSession`` the component opens from here on.

    The sessions stay fully real. The component creates its relay session
    internally and never hands it out, so this is the one way a test can see
    whether a failed setup or an unload closed it.
    """
    created: list[aiohttp.ClientSession] = []
    real = aiohttp.ClientSession

    def opened(*args: Any, **kwargs: Any) -> aiohttp.ClientSession:
        session = real(*args, **kwargs)
        created.append(session)
        return session

    monkeypatch.setattr(aiohttp, "ClientSession", opened)
    return created
