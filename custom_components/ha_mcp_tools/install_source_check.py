"""Detect a legacy (main-repo) HACS install source and warn (issue #1760).

Before the dedicated HACS mirror (``homeassistant-ai/ha-mcp-integration``)
existed, the README told users to add the MAIN ``ha-mcp`` server repository as
a HACS custom repository. Those installs still work — HACS downloads the repo
snapshot at the release tag, which contains this component — but HACS shows
the SERVER's version numbers (``7.x``) and the server/add-on release notes in
the update dialog, as if the component were the server itself. HACS has no
repository-migration mechanism, so this population stays confused forever
unless the component itself detects the legacy source and points them at the
mirror. The legacy install keeps working either way — this only files an
advisory repair issue, never blocks anything.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, callback
from homeassistant.helpers import issue_registry as ir

from .const import (
    DOMAIN,
    HACS_COMPONENT_URL,
    HACS_LEGACY_REPO_FULL_NAME,
    ISSUE_LEGACY_HACS_SOURCE,
)

if TYPE_CHECKING:
    from homeassistant.core import Event, HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Guards the schedule below so multiple config entries (tools + server) on the
# same HA run only ever schedule the check once.
_DATA_SCHEDULED = "install_source_check_scheduled"


def async_schedule_install_source_check(hass: HomeAssistant) -> None:
    """Schedule the legacy-HACS-source check to run once per Home Assistant run.

    Deferred to (or past) Home Assistant startup rather than run at
    component-setup time: HACS is a separate integration that may set up AFTER
    this one on a fresh boot, so checking here directly would race it and could
    misread a legitimate HACS-managed install as "no HACS" before HACS has
    populated ``hass.data["hacs"]``. ``EVENT_HOMEASSISTANT_STARTED`` only fires
    once every integration's config entries have finished setup, which is the
    guarantee this check needs. When hass has already reached that point (a
    config entry added or reloaded after startup), the event has already fired
    and never will again this run, so the check runs immediately instead.

    Guarded by a once-flag in ``hass.data[DOMAIN]``: both entry types call this
    on setup, and this must run at most once per HA run.
    """
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get(_DATA_SCHEDULED):
        return
    domain_data[_DATA_SCHEDULED] = True

    if hass.state == CoreState.running:
        hass.async_create_task(
            _async_check_install_source(hass), f"{DOMAIN}_install_source_check"
        )
        return

    @callback
    def _on_started(_event: Event) -> None:
        hass.async_create_task(
            _async_check_install_source(hass), f"{DOMAIN}_install_source_check"
        )

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _on_started)


async def _async_check_install_source(hass: HomeAssistant) -> None:
    """File/clear the legacy-HACS-source repair issue.

    Wraps the entire HACS interaction in a broad except: HACS is a third-party
    integration whose internals this reaches into directly (no public API
    exists for "what repository is this component tracking"), so any shape
    change there must degrade to a warning log rather than break Home
    Assistant. Advisory only — a failure changes nothing in the issue registry.
    """
    try:
        hacs = hass.data.get("hacs")
        installed = False
        if hacs is not None:
            repo = hacs.repositories.get_by_full_name(HACS_LEGACY_REPO_FULL_NAME)
            installed = repo is not None and bool(repo.data.installed)
    except Exception:
        _LOGGER.warning(
            "HA-MCP: could not determine the HACS install source", exc_info=True
        )
        return

    if installed:
        ir.async_create_issue(
            hass,
            DOMAIN,
            ISSUE_LEGACY_HACS_SOURCE,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_LEGACY_HACS_SOURCE,
            learn_more_url=HACS_COMPONENT_URL,
        )
    else:
        # Not installed via the legacy repo (including: no HACS at all, e.g. a
        # manual install) — clear any issue filed before the user migrated.
        ir.async_delete_issue(hass, DOMAIN, ISSUE_LEGACY_HACS_SOURCE)
