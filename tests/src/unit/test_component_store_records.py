"""Two installed store records for the integration get a fixable repair (#2993).

HACS let a download of the main repository land on top of a mirror install,
and the Marketplace then refuses to update or uninstall either record. The
component files a repair, and its fix forgets the duplicate through the store
manager so the kept record updates again.

The store's manager is private to the store, so it is a stand-in here; Home
Assistant itself (config entries, the issue registry, the started event) is
real.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant.common import MockConfigEntry, MockModule, mock_integration

from custom_components.ha_mcp_tools import store_records
from custom_components.ha_mcp_tools.const import DOMAIN, ISSUE_STORE_RECORDS_CONFLICT


def _repository(full_name, installed):
    return SimpleNamespace(
        data=SimpleNamespace(
            full_name=full_name,
            installed=installed,
            installed_version="v9.1.0" if installed else None,
            installed_commit="abc" if installed else None,
        )
    )


def _manager(*repositories):
    by_name = {r.data.full_name: r for r in repositories}
    return SimpleNamespace(
        repositories=SimpleNamespace(get_by_full_name=by_name.get),
        data=SimpleNamespace(async_write=AsyncMock()),
    )


def _both_installed():
    return _manager(
        _repository(store_records.MAIN_REPOSITORY, True),
        _repository(store_records.MIRROR_REPOSITORY, True),
    )


def _store_entry(
    hass: HomeAssistant, domain: str, state: ConfigEntryState
) -> MockConfigEntry:
    """A config entry of the store integration, which the test does not load."""
    mock_integration(
        hass, MockModule(domain, async_unload_entry=AsyncMock(return_value=True))
    )
    entry = MockConfigEntry(domain=domain, state=state)
    entry.add_to_hass(hass)
    return entry


def _marketplace(hass: HomeAssistant, manager) -> MockConfigEntry:
    """The Marketplace as loaded: its manager sits on its config entry."""
    entry = _store_entry(hass, "marketplace", ConfigEntryState.LOADED)
    entry.runtime_data = manager
    return entry


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


def test_two_installed_records_file_the_repair(
    hass: HomeAssistant, issue_registry: ir.IssueRegistry
) -> None:
    _marketplace(hass, _both_installed())

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


def test_one_installed_record_clears_the_repair(
    hass: HomeAssistant, issue_registry: ir.IssueRegistry
) -> None:
    _file_issue(hass)
    _marketplace(
        hass,
        _manager(
            _repository(store_records.MAIN_REPOSITORY, False),
            _repository(store_records.MIRROR_REPOSITORY, True),
        ),
    )

    store_records.async_check_store_records(hass)

    assert _issue(issue_registry) is None


def test_no_store_leaves_no_issue(
    hass: HomeAssistant, issue_registry: ir.IssueRegistry
) -> None:
    assert store_records.async_check_store_records(hass) is False

    assert _issue(issue_registry) is None


async def test_fix_keeps_the_main_repository_and_forgets_the_mirror(
    hass: HomeAssistant,
) -> None:
    main = _repository(store_records.MAIN_REPOSITORY, True)
    mirror = _repository(store_records.MIRROR_REPOSITORY, True)
    manager = _manager(main, mirror)
    _marketplace(hass, manager)

    kept = await store_records.async_resolve_store_records(hass)

    assert kept == store_records.MAIN_REPOSITORY
    assert main.data.installed is True
    assert mirror.data.installed is False
    assert mirror.data.installed_version is None
    manager.data.async_write.assert_awaited_once()


async def test_fix_with_one_record_changes_nothing(hass: HomeAssistant) -> None:
    mirror = _repository(store_records.MIRROR_REPOSITORY, True)
    manager = _manager(_repository(store_records.MAIN_REPOSITORY, False), mirror)
    _marketplace(hass, manager)

    assert await store_records.async_resolve_store_records(hass) is None
    assert mirror.data.installed is True
    manager.data.async_write.assert_not_awaited()


def test_an_unknown_manager_shape_is_unknown_not_clean(
    hass: HomeAssistant, issue_registry: ir.IssueRegistry
) -> None:
    """A manager the lookup cannot read says nothing about the records, so an
    existing repair stays and the check reports it could not look."""
    _file_issue(hass)
    _marketplace(hass, SimpleNamespace(repositories=object()))

    assert store_records.async_check_store_records(hass) is False

    assert _issue(issue_registry) is not None


async def test_a_failed_save_puts_the_records_back(hass: HomeAssistant) -> None:
    """Nothing reached disk, so the duplicate stays installed in memory too and
    the repair can be run again."""
    main = _repository(store_records.MAIN_REPOSITORY, True)
    mirror = _repository(store_records.MIRROR_REPOSITORY, True)
    manager = _manager(main, mirror)
    manager.data.async_write = AsyncMock(side_effect=OSError("disk full"))
    _marketplace(hass, manager)

    with pytest.raises(OSError):
        await store_records.async_resolve_store_records(hass)

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


def test_a_reload_after_the_check_ran_schedules_a_fresh_one(
    hass: HomeAssistant, checks: MagicMock
) -> None:
    """Once the pending check has run, a later entry reload gets its own."""
    _marketplace(hass, _manager(_repository(store_records.MIRROR_REPOSITORY, True)))

    store_records.async_schedule_store_records_check(hass)
    store_records.async_schedule_store_records_check(hass)

    assert checks.call_count == 2


async def test_startup_files_the_repair_once_home_assistant_has_started(
    hass: HomeAssistant, issue_registry: ir.IssueRegistry
) -> None:
    hass.set_state(CoreState.not_running)
    _marketplace(hass, _both_installed())

    store_records.async_schedule_store_records_check(hass)
    assert _issue(issue_registry) is None

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()

    assert _issue(issue_registry) is not None


def test_a_disabled_hacs_is_unknown_not_clean(
    hass: HomeAssistant, issue_registry: ir.IssueRegistry
) -> None:
    """HACS that failed its restore has an empty repository list until it
    retries; the issue must not be cleared on that."""
    _file_issue(hass)
    hass.data["hacs"] = SimpleNamespace(
        system=SimpleNamespace(disabled=True),
        repositories=SimpleNamespace(get_by_full_name=lambda _name: None),
    )

    assert store_records.conflicting_records(hass) is None
    assert store_records.async_check_store_records(hass) is False
    assert _issue(issue_registry) is not None


def test_no_store_integration_means_nothing_to_wait_for(
    hass: HomeAssistant, issue_registry: ir.IssueRegistry
) -> None:
    """A Core without HACS or the Marketplace: the check ends at once and the
    guard clears, with an existing repair left alone."""
    _file_issue(hass)

    store_records.async_schedule_store_records_check(hass)

    assert _issue(issue_registry) is not None
    assert store_records._SCHEDULED_KEY not in hass.data[DOMAIN]


@pytest.mark.parametrize("store_domain", ["hacs", "marketplace"])
async def test_an_unloaded_store_entry_is_checked_when_its_state_changes(
    hass: HomeAssistant,
    issue_registry: ir.IssueRegistry,
    checks: MagicMock,
    store_domain: str,
) -> None:
    """The store entry still setting up, retrying or re-enabled later: the
    check runs again on its state change, files the repair once the manager
    answers, then lets go of the entry."""
    entry = _store_entry(hass, store_domain, ConfigEntryState.NOT_LOADED)

    store_records.async_schedule_store_records_check(hass)
    assert hass.data[DOMAIN][store_records._SCHEDULED_KEY] is True

    entry.mock_state(hass, ConfigEntryState.SETUP_RETRY)  # still no manager
    assert _issue(issue_registry) is None
    assert hass.data[DOMAIN][store_records._SCHEDULED_KEY] is True

    if store_domain == "hacs":
        hass.data["hacs"] = _both_installed()
    else:
        entry.runtime_data = _both_installed()
    entry.mock_state(hass, ConfigEntryState.LOADED)
    await hass.async_block_till_done()

    assert _issue(issue_registry) is not None
    assert store_records._SCHEDULED_KEY not in hass.data[DOMAIN]
    ran = checks.call_count
    entry.mock_state(hass, ConfigEntryState.LOADED)
    assert checks.call_count == ran, "the entry's listener was not removed"


def test_a_registration_while_waiting_adds_no_second_listener(
    hass: HomeAssistant, checks: MagicMock
) -> None:
    """Two entries setting up while the store is not loaded share the one
    pending check instead of each subscribing to the store."""
    entry = _store_entry(hass, "hacs", ConfigEntryState.NOT_LOADED)

    store_records.async_schedule_store_records_check(hass)
    store_records.async_schedule_store_records_check(hass)
    ran = checks.call_count
    entry.mock_state(hass, ConfigEntryState.SETUP_RETRY)

    assert checks.call_count == ran + 1
