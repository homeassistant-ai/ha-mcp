"""Nudge HACS to refresh this component's repository info when a newer
component is detected (#1783/#1785 follow-up).

The component notices a newer custom component quickly — it checks PyPI every
6 hours (and on every reload/restart) and the auto-update gate reads the
component version shipped at each server release's tag. HACS, by contrast,
refreshes a *custom* repository's release data only about every 48 hours, so
the component update the hold (or the component-outdated repair) is waiting on
is usually not yet visible in HACS. This module runs the same force-refresh
that HACS's own repository "Update information" menu action performs, so HACS's
update entity flips promptly and Home Assistant advertises the component update
natively instead of the user waiting out HACS's cache.

HACS registers no service and ``homeassistant.update_entity`` is a no-op on its
entities, so there is no supported API for this: the refresh reaches directly
into HACS internals (``hass.data["hacs"]``). Those internals can change under us
at any HACS release, so EVERY access here is defensive and the whole interaction
is advisory — any failure degrades to a debug log and never touches the caller's
update-check path (which runs on bring-up, gating webhook registration, and on
the version coordinator's listener). Mirrors install_source_check's posture, the
component's other unsupported reach into ``hass.data["hacs"]``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .const import (
    DOMAIN,
    HACS_LEGACY_REPO_FULL_NAME,
    HACS_MIRROR_REPO_FULL_NAME,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# hass.data[DOMAIN] sub-key holding the component version we last asked HACS to
# refresh for. The hold check runs every 6h; without this the same pending
# version would force a fresh HACS network refresh on every pass. A plain string
# (not per-repo): one pending component version means one refresh. Distinct from
# the other DOMAIN sub-keys (const.py) so both entry types can share
# hass.data[DOMAIN].
_DATA_HACS_NUDGED_VERSION = "hacs_nudged_version"

# The repository full_names (owner/repo) HACS may track this component under, in
# lookup order: the dedicated mirror first (the current install path), the
# legacy main-repo path second (pre-mirror installs — see install_source_check).
_CANDIDATE_REPO_FULL_NAMES = (
    HACS_MIRROR_REPO_FULL_NAME,
    HACS_LEGACY_REPO_FULL_NAME,
)


def async_schedule_hacs_nudge(hass: HomeAssistant, target_version: str) -> None:
    """Fire-and-forget the HACS refresh nudge for ``target_version``.

    Scheduled as a background task rather than awaited: ``update_repository``
    makes a GitHub network call, and the callers must not block on it — the
    component-compat check is awaited inside the server bring-up *before*
    webhook registration (blocking it would delay the connect URLs for a
    display-only refresh), and the auto-update hold runs on the version
    coordinator's listener. ``async_create_task`` keeps a strong reference so
    the task is not garbage-collected mid-flight; every failure is contained
    inside :func:`async_nudge_hacs_refresh`.
    """
    hass.async_create_task(
        async_nudge_hacs_refresh(hass, target_version),
        f"{DOMAIN}_hacs_nudge",
    )


async def async_nudge_hacs_refresh(hass: HomeAssistant, target_version: str) -> None:
    """Ask HACS to re-fetch this component's repository info for ``target_version``.

    Throttled to at most one refresh per detected component version (the marker
    lives in ``hass.data[DOMAIN]``): the hold check repeats every 6h, and
    hammering HACS's GitHub fetch each pass for the same pending version would
    be pointless. Absent / broken HACS and a not-yet-registered repository stay
    unthrottled so a later pass (once HACS is ready) still gets its one refresh.
    """
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get(_DATA_HACS_NUDGED_VERSION) == target_version:
        # Already refreshed HACS for this pending component version.
        return

    try:
        nudged = await _async_force_hacs_repo_refresh(hass)
    except Exception:
        # HACS internals are unsupported and may change shape (missing hacs,
        # renamed attributes, a network failure inside update_repository); any
        # of it must degrade to a debug log, never fault the caller.
        _LOGGER.debug(
            "HA-MCP: could not nudge HACS to refresh the component repository",
            exc_info=True,
        )
        return

    if nudged:
        # Throttle only on a completed refresh, so a transient miss (HACS not
        # set up yet, repo not registered this pass) is retried next check
        # rather than suppressed for this version forever.
        domain_data[_DATA_HACS_NUDGED_VERSION] = target_version


async def _async_force_hacs_repo_refresh(hass: HomeAssistant) -> bool:
    """Run HACS's "Update information" force-refresh for this component's repo.

    Returns True when a tracked repository was found and its refresh completed,
    False when there is nothing to refresh (no HACS, or no INSTALLED repository
    under either candidate name). Reaches into HACS internals —
    the top-level lookups are ``getattr``-guarded so a wholly different HACS
    shape returns False cleanly; anything deeper that changes shape raises and is
    swallowed by :func:`async_nudge_hacs_refresh`.
    """
    hacs = hass.data.get("hacs")
    if hacs is None:
        # No HACS (manual/copy install, or HACS set up later this run) — nothing
        # to refresh; the caller leaves the throttle unset so a later pass retries.
        return False

    repositories = getattr(hacs, "repositories", None)
    get_by_full_name = getattr(repositories, "get_by_full_name", None)
    if get_by_full_name is None:
        return False

    repository = None
    for full_name in _CANDIDATE_REPO_FULL_NAMES:
        candidate = get_by_full_name(full_name)
        if candidate is None:
            continue
        # HACS keeps a repository record for every ADDED repo, downloaded or
        # not, but only creates an update entity for DOWNLOADED ones — and a
        # legacy->mirror migration can leave the mirror added but not yet
        # (re)installed while the running component is still tracked under the
        # legacy record. Refreshing an uninstalled record lights up nothing, so
        # only an installed candidate counts (review finding).
        if not getattr(getattr(candidate, "data", None), "installed", False):
            continue
        repository = candidate
        break
    if repository is None:
        return False

    # The repository's "Update information" menu action: re-fetch its release
    # data ignoring cached state, then push the fresh data to HACS's own update
    # entity so Home Assistant advertises the component update immediately.
    await repository.update_repository(ignore_issues=True, force=True)
    coordinators = getattr(hacs, "coordinators", None) or {}
    coordinator = coordinators.get(repository.data.category)
    if coordinator is not None:
        coordinator.async_update_listeners()
    return True
