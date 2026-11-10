"""Keep this integration on one store record (#2993).

Two store repositories carry this integration: the main repository, listed in
the default catalog, and the distribution mirror earlier installs came from.
Both extract to ``custom_components/ha_mcp_tools``. HACS let a download
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

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.start import async_at_started

from .const import DOMAIN, ISSUE_STORE_RECORDS_CONFLICT

_LOGGER = logging.getLogger(__name__)

MAIN_REPOSITORY = "homeassistant-ai/ha-mcp"
MIRROR_REPOSITORY = "homeassistant-ai/ha-mcp-integration"
OWN_REPOSITORIES = (MAIN_REPOSITORY, MIRROR_REPOSITORY)


def _store_manager(hass: HomeAssistant) -> Any | None:
    """Return the loaded Marketplace (2026.11+) or HACS manager, or None."""
    # The Marketplace (2026.11+) keeps its manager on its single config
    # entry; HACS kept it in hass.data. Not imported, so hassfest does not ask
    # for a dependency on a component older Core releases do not have.
    for entry in hass.config_entries.async_loaded_entries("marketplace"):
        manager = getattr(entry, "runtime_data", None)
        if manager is not None:
            return manager
    hacs = hass.data.get("hacs")
    # HACS publishes its manager before restoring its records and, when that
    # restore fails, disables itself and tries again 15 minutes later; until
    # then its repository list is empty, not clean.
    if hacs is not None and getattr(getattr(hacs, "system", None), "disabled", False):
        return None
    return hacs


def _installed_own_records(manager: Any) -> list[Any] | None:
    """The store's repository objects for this integration that are installed;
    None when the manager cannot be asked, which is not the same as none."""
    records = []
    for full_name in OWN_REPOSITORIES:
        try:
            repository = manager.repositories.get_by_full_name(full_name)
        except Exception:
            _LOGGER.debug("Store manager lookup failed", exc_info=True)
            return None
        if repository is not None and getattr(repository.data, "installed", False):
            records.append(repository)
    return records


def conflicting_records(hass: HomeAssistant) -> list[str] | None:
    """Full names of the installed records when more than one claims the
    directory; None when no store manager is loaded to ask."""
    manager = _store_manager(hass)
    if manager is None:
        return None
    records = _installed_own_records(manager)
    if records is None:
        return None
    if len(records) < 2:
        return []
    return [str(repository.data.full_name) for repository in records]


def async_check_store_records(hass: HomeAssistant) -> bool:
    """File or clear the repair issue for two installed records.

    Returns False when no store manager was loaded yet, so the caller can ask
    again; the issue is left as it was rather than cleared on no evidence."""
    records = conflicting_records(hass)
    if records is None:
        return False
    if not records:
        async_clear_store_records_issue(hass)
        return True
    _LOGGER.warning(
        "%s are all installed in the store and claim custom_components/%s; on "
        "Home Assistant 2026.11 the Marketplace refuses to update any of them "
        "until one is forgotten. The repair under Settings > System > Repairs "
        "keeps %s",
        " and ".join(records),
        DOMAIN,
        MAIN_REPOSITORY,
    )
    ir.async_create_issue(
        hass,
        DOMAIN,
        ISSUE_STORE_RECORDS_CONFLICT,
        is_fixable=True,
        # Survives a restart: while the store is not loaded the check leaves
        # the issue alone instead of re-deriving it, so it must still be there.
        is_persistent=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_STORE_RECORDS_CONFLICT,
        translation_placeholders={
            "records": " and ".join(records),
            "keep": MAIN_REPOSITORY,
        },
    )
    return True


def async_clear_store_records_issue(hass: HomeAssistant) -> None:
    """Drop the repair once the store shows one installed listing."""
    ir.async_delete_issue(hass, DOMAIN, ISSUE_STORE_RECORDS_CONFLICT)


async def async_resolve_store_records(hass: HomeAssistant) -> str | None:
    """Keep one record, the main repository when it is among them, and forget
    the others; returns the kept record's name, or None when nothing to do."""
    manager = _store_manager(hass)
    if manager is None:
        return None
    records = _installed_own_records(manager)
    if not records or len(records) < 2:
        return None
    kept = next(
        (r for r in records if str(r.data.full_name) == MAIN_REPOSITORY), records[0]
    )
    forgotten = [r for r in records if r is not kept]
    saved = {
        id(r): (r.data.installed, r.data.installed_version, r.data.installed_commit)
        for r in forgotten
    }
    for repository in forgotten:
        repository.data.installed = False
        repository.data.installed_version = None
        repository.data.installed_commit = None
    try:
        await manager.data.async_write()
    except Exception:
        # Nothing reached disk, so nothing in memory may differ from it either
        for repository in forgotten:
            (
                repository.data.installed,
                repository.data.installed_version,
                repository.data.installed_commit,
            ) = saved[id(repository)]
        raise
    for repository in forgotten:
        _LOGGER.info("Forgot the duplicate store record %s", repository.data.full_name)
    return str(kept.data.full_name)


_SCHEDULED_KEY = "store_records_check_scheduled"
_STORE_DOMAINS = ("marketplace", "hacs")


def async_schedule_store_records_check(hass: HomeAssistant) -> None:
    """Run the check once Home Assistant has started, when the store is up.

    Both entry types register through here, so a pending check is shared; once
    it has run, a later entry reload schedules a fresh one. A store whose entry
    is not loaded yet (still setting up, retrying, or disabled and enabled
    later) is checked the moment that entry changes state."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get(_SCHEDULED_KEY):
        return
    domain_data[_SCHEDULED_KEY] = True
    unsubscribers: list[Any] = []

    @callback
    def _done() -> None:
        # Deferred: this can run from inside Core's walk over the entry's
        # state-change listeners, and removing ours mid-walk would skip the
        # listener registered after it.
        for unsubscribe in unsubscribers:
            hass.loop.call_soon(unsubscribe)
        unsubscribers.clear()
        domain_data.pop(_SCHEDULED_KEY, None)

    @callback
    def _check(_event: Any = None) -> None:
        try:
            checked = async_check_store_records(hass)
        except Exception:
            _LOGGER.warning("Store record check failed", exc_info=True)
            checked = True
        if checked:
            _done()
            return
        if unsubscribers:
            return
        entries = [
            entry
            for domain in _STORE_DOMAINS
            for entry in hass.config_entries.async_entries(domain)
        ]
        if not entries:
            _done()
            return
        # HACS whose start failed leaves its entry in setup error and retries
        # on its own 15 minutes later without changing the entry's state; that
        # case waits for the next start.
        unsubscribers.extend(entry.async_on_state_change(_check) for entry in entries)

    async_at_started(hass, _check)
