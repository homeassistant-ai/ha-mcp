"""Update platform for the in-process server package (issue #1760).

Exposes one ``update`` entity per "server" config entry for the ha-mcp server
package it runs in-process, backed by :class:`~.coordinator.ServerVersionCoordinator`.
The entity stays populated whether or not automatic updates are on - see the
coordinator's docstring for why.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from aiohttp import ClientError
from awesomeversion import AwesomeVersion, AwesomeVersionException
from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DATA_BRINGUP_TASK,
    DATA_PENDING_INSTALL_VERSION,
    DATA_UPDATE_COORDINATOR,
    DEFAULT_AUTO_UPDATE,
    DIST_NAME_DEV,
    DOMAIN,
    OPT_AUTO_UPDATE,
)
from .coordinator import ServerVersionCoordinator, ServerVersionInfo
from .embedded_server import _installed_dist_version
from .embedded_setup import _async_update_held_by_component

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

_LOGGER = logging.getLogger(__name__)

# GitHub releases API for the release-notes surface (stable channel only - the
# dev channel has no tagged releases, see release_url / supported_features).
_RELEASES_URL = (
    "https://api.github.com/repos/homeassistant-ai/ha-mcp/releases?per_page=30"
)
_RELEASE_NOTES_TIMEOUT_SECONDS = 15

# Prepended to the release notes while the automatic server update is HELD on a
# newer custom component (embedded_setup's auto-update gate). ``ha-alert`` renders
# as a prominent banner in Home Assistant's markdown; ``{shipped}`` is the
# component version the release ships, ``{running}`` the one currently installed.
_COMPONENT_HOLD_WARNING = (
    '<ha-alert alert-type="warning">\n'
    "This release also updates the HA-MCP Custom Component (to {shipped}; you "
    "are running {running}). Update the component in HACS first — installing "
    "this server update now runs a server build the HACS component has never "
    "been tested with.\n"
    "</ha-alert>"
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Add the single server-package update entity for this config entry."""
    coordinator: ServerVersionCoordinator = hass.data[DOMAIN][DATA_UPDATE_COORDINATOR]
    async_add_entities([ServerUpdateEntity(coordinator, entry)])


class ServerUpdateEntity(CoordinatorEntity[ServerVersionCoordinator], UpdateEntity):
    """Update entity for the ha-mcp server package the "server" entry runs."""

    _attr_has_entity_name = True
    _attr_translation_key = "server_update"

    def __init__(
        self, coordinator: ServerVersionCoordinator, entry: ConfigEntry
    ) -> None:
        """Bind to the coordinator and the owning config entry."""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_server_update"

    @property
    def device_info(self) -> DeviceInfo:
        """Group under one device per config entry; sw_version = installed."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name="HA-MCP Server",
            manufacturer="homeassistant-ai",
            model="ha-mcp (in-process server)",
            sw_version=self.installed_version,
            configuration_url="https://github.com/homeassistant-ai/ha-mcp",
        )

    @property
    def installed_version(self) -> str | None:
        """Return the installed server-package version, or None if unknown."""
        data = self.coordinator.data
        return data.installed if data is not None else None

    @property
    def latest_version(self) -> str | None:
        """Return the newest PyPI version, or None if unknown/unresolvable."""
        data = self.coordinator.data
        return data.latest if data is not None else None

    @property
    def auto_update(self) -> bool:
        """Reflect the entry's automatic-update option."""
        return bool(self._entry.options.get(OPT_AUTO_UPDATE, DEFAULT_AUTO_UPDATE))

    @property
    def release_url(self) -> str | None:
        """Stable: the tagged GitHub release. Dev: the commit history (no tags)."""
        data = self.coordinator.data
        if data is None:
            return None
        if data.dist == DIST_NAME_DEV:
            return "https://github.com/homeassistant-ai/ha-mcp/commits/master"
        if data.latest is None:
            return None
        return f"https://github.com/homeassistant-ai/ha-mcp/releases/tag/v{data.latest}"

    @property
    def supported_features(self) -> UpdateEntityFeature:
        """RELEASE_NOTES only on the stable channel — dev builds have no tags."""
        features = UpdateEntityFeature.INSTALL
        data = self.coordinator.data
        if data is not None and data.dist != DIST_NAME_DEV:
            features |= UpdateEntityFeature.RELEASE_NOTES
        return features

    async def async_release_notes(self) -> str | None:
        """Return the GitHub release notes, with a component-update warning
        prepended when the pending update is held on a component update.

        When the newer server release also ships a newer custom component than
        the one running, the auto-update gate HOLDS the install (see
        embedded_setup._async_update_held_by_component), so the dialog leads
        with a prominent warning to update the component in HACS first. That
        warning must survive even a failed or empty notes fetch — surfacing it
        is the whole point of opening a held update's dialog — so a held update
        returns the warning alone rather than None. When not held, behaviour is
        unchanged.

        Advisory-only (same reasoning as embedded_setup's
        _async_check_component_compat): a GitHub fetch failure, rate limit, or
        unexpected payload shape degrades to the :attr:`release_url` fallback
        rather than breaking the update dialog.
        """
        data = self.coordinator.data
        if data is None or data.installed is None or data.latest is None:
            return None

        # Both probes are advisory network calls that contain their own
        # failures and timeouts; run them concurrently so the dialog waits for
        # the slower of the two, not their sum — a blocked/slow manifest host
        # must not stall the ordinary notes fetch (review finding).
        warning, notes = await asyncio.gather(
            self._async_component_hold_warning(data),
            self._async_fetch_release_notes(data),
        )

        if warning is None:
            # Not held: exactly the pre-existing behaviour (the notes, or None
            # on any failure/empty — the UI then falls back to release_url).
            return notes
        # Held: the warning must always surface, even when the notes fetch
        # failed or returned nothing — it must never vanish with the notes.
        if notes is None:
            return warning
        return f"{warning}\n\n{notes}"

    async def _async_component_hold_warning(
        self, data: ServerVersionInfo
    ) -> str | None:
        """Return the markdown component-hold warning, or None when not held.

        Reuses the auto-update gate's own held-check so this dialog and the
        Repairs hold agree on when the component is behind. Fully advisory: any
        failure — including an unexpected error escaping the gate — degrades to
        None so the hold warning can never break the release-notes dialog.
        """
        try:
            held = await _async_update_held_by_component(self.hass, data)
        except Exception:
            # The gate contains all its expected failures internally, so an
            # exception escaping it is a bug — logged visibly per the repo's
            # convention (review finding), still degrading to plain notes.
            _LOGGER.warning(
                "HA-MCP release-notes component-hold check failed", exc_info=True
            )
            return None
        if held is None:
            return None
        shipped, running = held
        return _COMPONENT_HOLD_WARNING.format(shipped=shipped, running=running)

    async def _async_fetch_release_notes(self, data: ServerVersionInfo) -> str | None:
        """Concatenate GitHub release bodies between installed and latest.

        Advisory-only: a GitHub fetch failure, rate limit, or unexpected payload
        shape degrades to None so the dialog falls back to :attr:`release_url`.
        """
        try:
            installed = AwesomeVersion(data.installed)
            latest = AwesomeVersion(data.latest)
            session = async_get_clientsession(self.hass)
            async with asyncio.timeout(_RELEASE_NOTES_TIMEOUT_SECONDS):
                async with session.get(_RELEASES_URL) as resp:
                    resp.raise_for_status()
                    releases = await resp.json()

            notes: list[tuple[AwesomeVersion, str]] = []
            for release in releases:
                tag = str(release.get("tag_name") or "").removeprefix("v")
                try:
                    version = AwesomeVersion(tag)
                except AwesomeVersionException:
                    continue
                if installed < version <= latest:
                    notes.append((version, str(release.get("body") or "")))
        except (ClientError, TimeoutError) as err:
            # Expected transients (GitHub unreachable, rate-limited, slow) —
            # quiet; the dialog falls back to release_url.
            _LOGGER.debug("HA-MCP release-notes fetch failed: %s", err)
            return None
        except Exception:
            # An unexpected payload shape (TypeError/AttributeError in the
            # parse loop) is a bug or a GitHub API change — logged visibly per
            # the repo's convention (review finding), still degrading to the
            # release_url fallback rather than breaking the update dialog.
            _LOGGER.warning("HA-MCP release-notes fetch failed", exc_info=True)
            return None

        if not notes:
            return None
        notes.sort(key=lambda item: item[0], reverse=True)
        return "\n\n---\n\n".join(body for _, body in notes)

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        """Reinstall pinned to ``version`` (or the latest known build).

        With auto-update off, ``_resolve_pip_spec`` pins the install to the
        currently-installed version, so a bare reload would just reinstall the
        same build. The one-shot pending-install marker overrides that pin for
        this single reload; embedded_server clears it when it consumes it (one
        marker buys one attempt). The reload only completes entry SETUP — the
        pip install runs in the reloaded entry's background bring-up — so this
        waits for that bring-up and verifies the requested version actually
        landed; returning at reload time would report success for an install
        that can still fail (review finding).
        """
        data = self.coordinator.data
        target = version or self.latest_version
        if target is None:
            raise HomeAssistantError("No target version available to install.")
        # Broad except is intentional here (unlike this repo's usual narrow
        # convention): async_install feeds Home Assistant's update UI, which
        # expects a HomeAssistantError for ANY failure rather than an opaque
        # traceback in the install dialog. Logged with traceback first so a
        # genuine bug still reaches the log (review finding).
        try:
            new_data = {**self._entry.data, DATA_PENDING_INSTALL_VERSION: target}
            self.hass.config_entries.async_update_entry(self._entry, data=new_data)
            await self.hass.config_entries.async_reload(self._entry.entry_id)
            # The reloaded entry's bring-up task does the actual install; it
            # contains its own failures (files repair issues instead of
            # raising), so awaiting it tells us the attempt is over, not that
            # it worked — the version read below is the success check.
            bringup = self.hass.data.get(DOMAIN, {}).get(DATA_BRINGUP_TASK)
            if bringup is not None:
                await bringup
            installed: str | None = None
            if data is not None:
                installed = await self.hass.async_add_executor_job(
                    _installed_dist_version, data.dist
                )
        except Exception as err:
            _LOGGER.exception("HA-MCP server update install failed")
            raise HomeAssistantError(
                f"Could not install the HA-MCP server update: {err}"
            ) from err
        # Outside the broad except: these raises must reach the UI as-is, not
        # get re-wrapped into the generic message.
        if data is not None and installed != target:
            raise HomeAssistantError(
                f"The HA-MCP server update to {target} did not complete "
                f"(installed: {installed or 'none'}). See Settings > Repairs "
                "for the failure details."
            )
