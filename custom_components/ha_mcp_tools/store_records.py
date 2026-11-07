"""Keep this integration on one store record (#2993).

Two store repositories carry this integration: the main repository, listed in
the default catalog, and the legacy distribution mirror earlier installs came
from. Both extract to ``custom_components/ha_mcp_tools``. HACS let a download
of the second one land on top of the first, and the Marketplace in Home
Assistant 2026.11 then refuses to update either record and refuses to
uninstall either while any config entry of this domain exists. The store's own
UI has no way out, so the repair here flips the duplicate record back to "not
installed" through the store's own manager, the same state the store keeps
for a repository it never downloaded.

The store managers are private to Home Assistant, so every access is guarded:
a store that is missing or shaped differently leaves no issue and no change.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from .const import DOMAIN, ISSUE_STORE_RECORDS_CONFLICT

_LOGGER = logging.getLogger(__name__)

MAIN_REPOSITORY = "homeassistant-ai/ha-mcp"
MIRROR_REPOSITORY = "homeassistant-ai/ha-mcp-integration"
OWN_REPOSITORIES = (MAIN_REPOSITORY, MIRROR_REPOSITORY)


def _store_manager(hass: HomeAssistant) -> Any | None:
    """Return the loaded Marketplace (2026.11+) or HACS manager, or None."""
    try:
        from homeassistant.components.marketplace.base import async_get_marketplace

        return async_get_marketplace(hass)
    except Exception:  # noqa: BLE001 - older Core, or the Marketplace not loaded
        pass
    return hass.data.get("hacs")


def _installed_own_records(manager: Any) -> list[Any]:
    """The store's repository objects for this integration that are installed."""
    records = []
    for full_name in OWN_REPOSITORIES:
        try:
            repository = manager.repositories.get_by_full_name(full_name)
        except Exception:  # noqa: BLE001 - unknown manager shape
            return []
        if repository is not None and getattr(repository.data, "installed", False):
            records.append(repository)
    return records


def conflicting_records(hass: HomeAssistant) -> list[str]:
    """Full names of the installed records when more than one claims the directory."""
    manager = _store_manager(hass)
    if manager is None:
        return []
    records = _installed_own_records(manager)
    if len(records) < 2:
        return []
    return [str(repository.data.full_name) for repository in records]


def async_check_store_records(hass: HomeAssistant) -> None:
    """File or clear the repair issue for two installed records."""
    from homeassistant.helpers import issue_registry as ir

    records = conflicting_records(hass)
    if not records:
        ir.async_delete_issue(hass, DOMAIN, ISSUE_STORE_RECORDS_CONFLICT)
        return
    _LOGGER.warning(
        "%s are all installed in the store and claim custom_components/%s; "
        "the store refuses to update any of them until one is forgotten. The "
        "repair under Settings > System > Repairs keeps %s",
        " and ".join(records),
        DOMAIN,
        MAIN_REPOSITORY,
    )
    ir.async_create_issue(
        hass,
        DOMAIN,
        ISSUE_STORE_RECORDS_CONFLICT,
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_STORE_RECORDS_CONFLICT,
        translation_placeholders={
            "records": ", ".join(records),
            "keep": MAIN_REPOSITORY,
        },
    )


async def async_resolve_store_records(hass: HomeAssistant) -> str | None:
    """Keep one record, the main repository when it is among them, and forget
    the others; returns the kept record's name, or None when nothing to do."""
    manager = _store_manager(hass)
    if manager is None:
        return None
    records = _installed_own_records(manager)
    if len(records) < 2:
        return None
    kept = next(
        (r for r in records if str(r.data.full_name) == MAIN_REPOSITORY), records[0]
    )
    for repository in records:
        if repository is kept:
            continue
        repository.data.installed = False
        repository.data.installed_version = None
        repository.data.installed_commit = None
        _LOGGER.info("Forgot the duplicate store record %s", repository.data.full_name)
    await manager.data.async_write()
    return str(kept.data.full_name)


def async_schedule_store_records_check(hass: HomeAssistant) -> None:
    """Run the check once Home Assistant has started, when the store is up."""

    def _check(_event: Any = None) -> None:
        try:
            async_check_store_records(hass)
        except Exception:
            _LOGGER.debug("Store record check failed", exc_info=True)

    try:
        from homeassistant.helpers.start import async_at_started
    except ImportError:
        # Hermetic unit tests stub ``homeassistant.helpers`` without ``start``.
        hass.bus.async_listen_once("homeassistant_started", _check)
        return
    async_at_started(hass, _check)
