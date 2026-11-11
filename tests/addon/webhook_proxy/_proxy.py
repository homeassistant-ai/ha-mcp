"""Helpers for setting the proxy up in a real ``hass`` and relaying to it."""

from __future__ import annotations

import asyncio
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
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from ._variants import Variant

STATE_ROOTS = ("/config", "/data")
# The click-to-restart Repair the component raises when OAuth changes need a restart.
OAUTH_ISSUE = "oauth_restart_required"


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


async def start_proxy(
    hass: HomeAssistant, variant: Variant, fs_root: Path, config: dict[str, Any]
) -> MockConfigEntry:
    """Write ``config`` as the app would and let Core set the entry up."""
    write_proxy_config(fs_root, variant, config)
    return await set_up_proxy(hass, variant)


def restart_issue(
    hass: HomeAssistant, variant: Variant, issue_id: str = OAUTH_ISSUE
) -> ir.IssueEntry | None:
    """The proxy's click-to-restart Repairs issue, as Core's registry holds it."""
    return ir.async_get(hass).async_get_issue(variant.domain, issue_id)


async def wait_until(
    hass: HomeAssistant, condition: Callable[[], bool], timeout: float = 5.0
) -> None:
    """Wait for work the component hands to Core's executor without awaiting.

    The inbound-log mirror write is fire-and-forget, so
    ``hass.async_block_till_done()`` alone can return before it has run.
    """
    async with asyncio.timeout(timeout):
        while True:
            await hass.async_block_till_done()
            if condition():
                return
            await asyncio.sleep(0.01)


def text_of(path: Path) -> str:
    """The file's text, "" until it exists."""
    return path.read_text() if path.exists() else ""
