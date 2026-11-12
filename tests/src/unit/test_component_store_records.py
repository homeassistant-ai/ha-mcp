"""Two installed store records for the integration get a fixable repair (#2993).

HACS let a download of the main repository land on top of a mirror install,
and the Marketplace then refuses to update or uninstall either record. The
component files a repair, and its fix forgets the duplicate through the store
manager so the kept record updates again.

The store manager is Core's own Marketplace manager (``_marketplace_records``), so
the fix is read back from what the Marketplace saved. HACS, which is not part
of Core, publishes the same manager shape in ``hass.data``; the Marketplace's
manager, which Core took over from it, plays it here.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.marketplace.base import MarketplaceManager
from homeassistant.components.marketplace.enums import DisabledReason
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.ha_mcp_tools import store_records
from custom_components.ha_mcp_tools.const import DOMAIN, ISSUE_STORE_RECORDS_CONFLICT

from ._marketplace_records import (
    build_manager,
    loaded_marketplace,
    store_entry,
    stored_record,
)

Marketplace = Callable[..., Awaitable[MarketplaceManager]]


@pytest.fixture
def marketplace(hass: HomeAssistant, hass_storage: dict[str, Any]) -> Marketplace:
    """Load the Marketplace with this integration's records as given."""

    async def load(**installed: bool) -> MarketplaceManager:
        return await loaded_marketplace(hass, hass_storage, **installed)

    return load


def _installed(built: MarketplaceManager, full_name: str) -> bool:
    repository = built.repositories.get_by_full_name(full_name)
    assert repository is not None
    return bool(repository.data.installed)


def _file_issue(hass: HomeAssistant) -> None:
    ir.async_create_issue(
        hass,
        DOMAIN,
        ISSUE_STORE_RECORDS_CONFLICT,
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_STORE_RECORDS_CONFLICT,
    )


def _issue(issue_registry: ir.IssueRegistry) -> ir.IssueEntry | None:
    return issue_registry.async_get_issue(DOMAIN, ISSUE_STORE_RECORDS_CONFLICT)


@pytest.fixture
def checks(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Counts the checks the scheduler runs, which still run for real."""
    spy = MagicMock(wraps=store_records.async_check_store_records)
    monkeypatch.setattr(store_records, "async_check_store_records", spy)
    return spy


async def test_two_installed_records_file_the_repair(
    hass: HomeAssistant, issue_registry: ir.IssueRegistry, marketplace: Marketplace
) -> None:
    await marketplace(main=True, mirror=True)

    store_records.async_check_store_records(hass)

    issue = _issue(issue_registry)
    assert issue is not None
    assert issue.is_fixable is True
    # The check skips an unloaded store, so the issue has to outlive a restart.
    assert issue.is_persistent is True
    assert issue.translation_placeholders is not None
    assert set(issue.translation_placeholders["records"].split(" and ")) == {
        store_records.MAIN_REPOSITORY,
        store_records.MIRROR_REPOSITORY,
    }


async def test_one_installed_record_clears_the_repair(
    hass: HomeAssistant, issue_registry: ir.IssueRegistry, marketplace: Marketplace
) -> None:
    _file_issue(hass)
    await marketplace(main=False, mirror=True)

    store_records.async_check_store_records(hass)

    assert _issue(issue_registry) is None


def test_no_store_leaves_no_issue(
    hass: HomeAssistant, issue_registry: ir.IssueRegistry
) -> None:
    assert store_records.async_check_store_records(hass) is False

    assert _issue(issue_registry) is None


async def test_fix_keeps_the_main_repository_and_forgets_the_mirror(
    hass: HomeAssistant, hass_storage: dict[str, Any], marketplace: Marketplace
) -> None:
    built = await marketplace(main=True, mirror=True)

    kept = await store_records.async_resolve_store_records(hass)

    assert kept == store_records.MAIN_REPOSITORY
    assert _installed(built, store_records.MAIN_REPOSITORY) is True
    assert _installed(built, store_records.MIRROR_REPOSITORY) is False
    # What the Marketplace saved, which it reads back at the next start.
    assert (
        stored_record(hass_storage, store_records.MAIN_REPOSITORY)["installed"] is True
    )
    mirror = stored_record(hass_storage, store_records.MIRROR_REPOSITORY)
    assert "installed" not in mirror
    assert "version_installed" not in mirror


async def test_fix_with_one_record_changes_nothing(
    hass: HomeAssistant, marketplace: Marketplace
) -> None:
    built = await marketplace(main=False, mirror=True)

    with patch.object(built.data, "async_write", wraps=built.data.async_write) as save:
        assert await store_records.async_resolve_store_records(hass) is None

    assert _installed(built, store_records.MIRROR_REPOSITORY) is True
    save.assert_not_awaited()


async def test_an_unknown_manager_shape_is_unknown_not_clean(
    hass: HomeAssistant, issue_registry: ir.IssueRegistry
) -> None:
    """A manager the lookup cannot read (another Core release's shape) says
    nothing about the records, so an existing repair stays and the check
    reports it could not look."""
    _file_issue(hass)
    entry = store_entry(hass, "marketplace", ConfigEntryState.LOADED)
    entry.runtime_data = object()

    assert store_records.async_check_store_records(hass) is False

    assert _issue(issue_registry) is not None


async def test_a_failed_save_puts_the_records_back(
    hass: HomeAssistant, marketplace: Marketplace
) -> None:
    """Nothing reached disk, so the duplicate stays installed in memory too and
    the repair can be run again."""
    built = await marketplace(main=True, mirror=True)

    with (
        patch(
            "homeassistant.components.marketplace.utils.data.async_save_to_storage",
            AsyncMock(side_effect=OSError("disk full")),
        ),
        pytest.raises(OSError),
    ):
        await store_records.async_resolve_store_records(hass)

    mirror = built.repositories.get_by_full_name(store_records.MIRROR_REPOSITORY)
    assert mirror is not None
    assert mirror.data.installed is True
    assert mirror.data.installed_version == "v9.1.0"


async def test_the_check_is_scheduled_once_per_start(
    hass: HomeAssistant, checks: MagicMock
) -> None:
    """Both entry types register through the same device helper; the check
    and its warning must not run twice."""
    hass.set_state(CoreState.not_running)
    store_records.async_schedule_store_records_check(hass)
    store_records.async_schedule_store_records_check(hass)
    assert checks.call_count == 0

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()

    assert checks.call_count == 1


async def test_a_reload_after_the_check_ran_schedules_a_fresh_one(
    hass: HomeAssistant, checks: MagicMock, marketplace: Marketplace
) -> None:
    """Once the pending check has run, a later entry reload gets its own."""
    await marketplace(mirror=True)

    store_records.async_schedule_store_records_check(hass)
    store_records.async_schedule_store_records_check(hass)

    assert checks.call_count == 2


async def test_startup_files_the_repair_once_home_assistant_has_started(
    hass: HomeAssistant, issue_registry: ir.IssueRegistry, marketplace: Marketplace
) -> None:
    await marketplace(main=True, mirror=True)
    hass.set_state(CoreState.not_running)

    store_records.async_schedule_store_records_check(hass)
    assert _issue(issue_registry) is None

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()

    assert _issue(issue_registry) is not None


async def test_a_disabled_hacs_is_unknown_not_clean(
    hass: HomeAssistant,
    issue_registry: ir.IssueRegistry,
    hass_storage: dict[str, Any],
) -> None:
    """HACS that failed its restore has an empty repository list until it
    retries; the issue must not be cleared on that."""
    _file_issue(hass)
    entry = store_entry(hass, "hacs", ConfigEntryState.LOADED)
    hacs = await build_manager(hass, hass_storage, entry, restore=False)
    # HACS names its own reason (a failed restore); only "disabled" is read.
    hacs.system.disabled_reason = DisabledReason.RATE_LIMIT
    hass.data["hacs"] = hacs

    assert store_records.conflicting_records(hass) is None
    assert store_records.async_check_store_records(hass) is False
    assert _issue(issue_registry) is not None


def test_no_store_integration_means_nothing_to_wait_for(
    hass: HomeAssistant, issue_registry: ir.IssueRegistry, checks: MagicMock
) -> None:
    """A Core without HACS or the Marketplace: the check ends at once and the
    guard clears, with an existing repair left alone."""
    _file_issue(hass)

    store_records.async_schedule_store_records_check(hass)
    assert _issue(issue_registry) is not None

    # The guard cleared: the next registration runs a check of its own.
    store_records.async_schedule_store_records_check(hass)
    assert checks.call_count == 2


@pytest.mark.parametrize("store_domain", ["hacs", "marketplace"])
async def test_an_unloaded_store_entry_is_checked_when_its_state_changes(
    hass: HomeAssistant,
    issue_registry: ir.IssueRegistry,
    checks: MagicMock,
    hass_storage: dict[str, Any],
    store_domain: str,
) -> None:
    """The store entry still setting up, retrying or re-enabled later: the
    check runs again on its state change, files the repair once the manager
    answers, then lets go of the entry."""
    entry = store_entry(hass, store_domain, ConfigEntryState.NOT_LOADED)

    store_records.async_schedule_store_records_check(hass)

    entry.mock_state(hass, ConfigEntryState.SETUP_RETRY)  # still no manager
    assert _issue(issue_registry) is None

    built = await build_manager(hass, hass_storage, entry, main=True, mirror=True)
    if store_domain == "hacs":
        hass.data["hacs"] = built
    else:
        entry.runtime_data = built
    entry.mock_state(hass, ConfigEntryState.LOADED)
    await hass.async_block_till_done()

    assert _issue(issue_registry) is not None
    ran = checks.call_count
    entry.mock_state(hass, ConfigEntryState.LOADED)
    assert checks.call_count == ran, "the entry's listener was not removed"
    # The guard cleared: the next registration runs a check of its own.
    store_records.async_schedule_store_records_check(hass)
    assert checks.call_count == ran + 1


def test_a_registration_while_waiting_adds_no_second_listener(
    hass: HomeAssistant, checks: MagicMock
) -> None:
    """Two entries setting up while the store is not loaded share the one
    pending check instead of each subscribing to the store."""
    entry = store_entry(hass, "hacs", ConfigEntryState.NOT_LOADED)

    store_records.async_schedule_store_records_check(hass)
    store_records.async_schedule_store_records_check(hass)
    ran = checks.call_count
    entry.mock_state(hass, ConfigEntryState.SETUP_RETRY)

    assert checks.call_count == ran + 1
