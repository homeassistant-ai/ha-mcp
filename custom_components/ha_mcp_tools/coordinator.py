"""Poll the server package's installed vs. latest PyPI version (issue #1760).

Backs the ``update`` platform entity (:mod:`update`) and the automatic-update
decision (:func:`embedded_setup.async_maybe_auto_update`). Runs on
:data:`UPDATE_CHECK_INTERVAL` regardless of the ``auto_update`` option — unlike
the check this replaces, visibility must not depend on auto-update being on
(issue #1760: with auto-update off, users previously got zero signal that a
server update existed). Only the resulting *reload* is gated on ``auto_update``,
in :mod:`embedded_setup`.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aiohttp import ClientError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    DEFAULT_CHANNEL,
    DEFAULT_PIP_SPEC,
    DOMAIN,
    OPT_CHANNEL,
    OPT_PIP_SPEC,
    PYPI_JSON_URL,
    UPDATE_CHECK_INTERVAL,
    dist_for_channel,
)
from .embedded_server import _installed_dist_version

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Per-request timeout for the PyPI version-check fetch - short so a slow or
# wedged PyPI never ties up the coordinator; a miss just retries next interval
# (moved here from the old embedded_setup.async_check_for_update).
_PYPI_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class ServerVersionInfo:
    """Installed vs. latest server-package version for one config entry."""

    installed: str | None
    latest: str | None
    dist: str


class ServerVersionCoordinator(DataUpdateCoordinator[ServerVersionInfo]):
    """Poll the installed + PyPI-latest version of the in-process server package.

    Deliberately NOT scoped to the ``auto_update`` option: the update entity
    must stay populated and the periodic check must keep running even when the
    user has automatic updates turned off - that visibility is the point of
    issue #1760. ``embedded_entry`` schedules this coordinator's listener to
    decide whether to actually reload.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Bind to the config entry and schedule on UPDATE_CHECK_INTERVAL."""
        self._entry = entry
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} server version",
            update_interval=UPDATE_CHECK_INTERVAL,
        )

    async def _async_update_data(self) -> ServerVersionInfo:
        """Return the installed + latest version for the configured channel.

        Never raises ``UpdateFailed`` for an expected PyPI transient - the
        entity must stay available (showing the installed version) even when
        PyPI is unreachable; :meth:`_async_fetch_latest`'s own narrow except
        clause is the only one expected to fire in normal operation.
        """
        options = self._entry.options
        channel = str(options.get(OPT_CHANNEL) or DEFAULT_CHANNEL)
        dist = dist_for_channel(channel)
        installed = await self.hass.async_add_executor_job(
            _installed_dist_version, dist
        )

        override = str(options.get(OPT_PIP_SPEC) or "").strip()
        if override and override != DEFAULT_PIP_SPEC:
            # An explicit pip-spec override (a version pin, a tarball URL)
            # makes a PyPI-latest comparison meaningless - skip the fetch.
            return ServerVersionInfo(installed=installed, latest=None, dist=dist)

        latest = await self._async_fetch_latest(dist)
        return ServerVersionInfo(installed=installed, latest=latest, dist=dist)

    async def _async_fetch_latest(self, dist: str) -> str | None:
        """Return the newest PyPI version for ``dist``, or None on any failure."""
        try:
            session = async_get_clientsession(self.hass)
            async with asyncio.timeout(_PYPI_TIMEOUT_SECONDS):
                async with session.get(PYPI_JSON_URL.format(dist=dist)) as resp:
                    resp.raise_for_status()
                    payload = await resp.json()
            return str(payload["info"]["version"])
        except (ClientError, TimeoutError, KeyError, ValueError) as err:
            _LOGGER.debug("HA-MCP server version check failed for %s: %s", dist, err)
            return None
