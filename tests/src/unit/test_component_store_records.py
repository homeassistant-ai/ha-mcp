"""Two installed store records for the integration get a fixable repair (#2993).

HACS let a download of the main repository land on top of a mirror install,
and the Marketplace then refuses to update or uninstall either record. The
component files a repair, and its fix forgets the duplicate through the store
manager so the kept record updates again.
"""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

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


@pytest.fixture(autouse=True)
def real_callback_decorator(monkeypatch):
    """Another unit module may have stubbed ``homeassistant.core`` with a bare
    MagicMock, whose ``callback`` would swallow the scheduled check."""
    monkeypatch.setattr(store_records, "callback", lambda func: func)


@pytest.fixture
def issue_registry(monkeypatch):
    registry = MagicMock()
    registry.IssueSeverity = SimpleNamespace(WARNING="warning")
    helpers = ModuleType("homeassistant.helpers")
    helpers.issue_registry = registry
    monkeypatch.setitem(sys.modules, "homeassistant.helpers", helpers)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.issue_registry", registry)
    return registry


def _hass_with(monkeypatch, manager):
    monkeypatch.setattr(store_records, "_store_manager", lambda _hass: manager)
    return MagicMock()


def test_two_installed_records_file_the_repair(monkeypatch, issue_registry):
    manager = _manager(
        _repository(store_records.MAIN_REPOSITORY, True),
        _repository(store_records.MIRROR_REPOSITORY, True),
    )
    hass = _hass_with(monkeypatch, manager)

    store_records.async_check_store_records(hass)

    call = issue_registry.async_create_issue.call_args
    assert call.args[1:] == (DOMAIN, ISSUE_STORE_RECORDS_CONFLICT)
    assert call.kwargs["is_fixable"] is True
    # The check skips an unloaded store, so the issue has to outlive a restart.
    assert call.kwargs["is_persistent"] is True
    assert set(call.kwargs["translation_placeholders"]["records"].split(" and ")) == {
        store_records.MAIN_REPOSITORY,
        store_records.MIRROR_REPOSITORY,
    }


def test_one_installed_record_clears_the_repair(monkeypatch, issue_registry):
    manager = _manager(
        _repository(store_records.MAIN_REPOSITORY, False),
        _repository(store_records.MIRROR_REPOSITORY, True),
    )
    hass = _hass_with(monkeypatch, manager)

    store_records.async_check_store_records(hass)

    issue_registry.async_create_issue.assert_not_called()
    issue_registry.async_delete_issue.assert_called_once_with(
        hass, DOMAIN, ISSUE_STORE_RECORDS_CONFLICT
    )


def test_no_store_leaves_no_issue(monkeypatch, issue_registry):
    hass = _hass_with(monkeypatch, None)

    store_records.async_check_store_records(hass)

    issue_registry.async_create_issue.assert_not_called()


async def test_fix_keeps_the_main_repository_and_forgets_the_mirror(monkeypatch):
    main = _repository(store_records.MAIN_REPOSITORY, True)
    mirror = _repository(store_records.MIRROR_REPOSITORY, True)
    manager = _manager(main, mirror)
    hass = _hass_with(monkeypatch, manager)

    kept = await store_records.async_resolve_store_records(hass)

    assert kept == store_records.MAIN_REPOSITORY
    assert main.data.installed is True
    assert mirror.data.installed is False
    assert mirror.data.installed_version is None
    manager.data.async_write.assert_awaited_once()


async def test_fix_with_one_record_changes_nothing(monkeypatch):
    mirror = _repository(store_records.MIRROR_REPOSITORY, True)
    manager = _manager(_repository(store_records.MAIN_REPOSITORY, False), mirror)
    hass = _hass_with(monkeypatch, manager)

    assert await store_records.async_resolve_store_records(hass) is None
    assert mirror.data.installed is True
    manager.data.async_write.assert_not_awaited()


def test_an_unknown_manager_shape_is_unknown_not_clean(monkeypatch, issue_registry):
    """A manager the lookup cannot read says nothing about the records, so an
    existing repair stays and the check reports it could not look."""
    hass = _hass_with(monkeypatch, SimpleNamespace(repositories=object()))

    assert store_records.async_check_store_records(hass) is False

    issue_registry.async_create_issue.assert_not_called()
    issue_registry.async_delete_issue.assert_not_called()


async def test_a_failed_save_puts_the_records_back(monkeypatch):
    """Nothing reached disk, so the duplicate stays installed in memory too and
    the repair can be run again."""
    main = _repository(store_records.MAIN_REPOSITORY, True)
    mirror = _repository(store_records.MIRROR_REPOSITORY, True)
    manager = _manager(main, mirror)
    manager.data.async_write = AsyncMock(side_effect=OSError("disk full"))
    hass = _hass_with(monkeypatch, manager)

    with pytest.raises(OSError):
        await store_records.async_resolve_store_records(hass)

    assert mirror.data.installed is True
    assert mirror.data.installed_version == "v9.1.0"


def test_the_check_is_scheduled_once_per_start(monkeypatch):
    """Both entry types register through the same device helper; the check
    and its warning must not run twice."""
    hass = MagicMock()
    hass.data = {}
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.start", None)
    store_records.async_schedule_store_records_check(hass)
    store_records.async_schedule_store_records_check(hass)
    assert hass.bus.async_listen_once.call_count == 1


def test_a_reload_after_the_check_ran_schedules_a_fresh_one(
    monkeypatch, issue_registry
):
    """Once the pending check has run, a later entry reload gets its own."""
    manager = _manager(_repository(store_records.MIRROR_REPOSITORY, True))
    hass = _hass_with(monkeypatch, manager)
    hass.data = {}
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.start", None)
    store_records.async_schedule_store_records_check(hass)
    hass.bus.async_listen_once.call_args.args[1]()
    store_records.async_schedule_store_records_check(hass)
    assert hass.bus.async_listen_once.call_count == 2


def test_startup_runs_the_check_through_at_started(monkeypatch, issue_registry):
    """The normal path: the callback handed to async_at_started files the
    repair for two installed records."""
    manager = _manager(
        _repository(store_records.MAIN_REPOSITORY, True),
        _repository(store_records.MIRROR_REPOSITORY, True),
    )
    hass = _hass_with(monkeypatch, manager)
    hass.data = {}
    start = ModuleType("homeassistant.helpers.start")
    captured = {}
    start.async_at_started = lambda _hass, cb: captured.setdefault("cb", cb)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.start", start)

    store_records.async_schedule_store_records_check(hass)
    captured["cb"](hass)

    assert (
        issue_registry.async_create_issue.call_args.args[2]
        == ISSUE_STORE_RECORDS_CONFLICT
    )


def test_a_disabled_hacs_is_unknown_not_clean(monkeypatch, issue_registry):
    """HACS that failed its restore has an empty repository list until it
    retries; the issue must not be cleared on that."""
    hass = MagicMock()
    hass.config_entries.async_loaded_entries.return_value = []
    hacs = SimpleNamespace(
        system=SimpleNamespace(disabled=True),
        repositories=SimpleNamespace(get_by_full_name=lambda _name: None),
    )
    hass.data = {"hacs": hacs}

    assert store_records.conflicting_records(hass) is None
    assert store_records.async_check_store_records(hass) is False
    issue_registry.async_delete_issue.assert_not_called()


def _listening_hass(monkeypatch, store_entries):
    """A hass whose store manager is absent and whose store entries can be
    subscribed to; returns hass and the list of listeners registered."""
    hass = _hass_with(monkeypatch, None)
    hass.data = {}
    # The real loop runs a call_soon callback on its next turn; here at once.
    hass.loop.call_soon.side_effect = lambda cb, *args: cb(*args)
    listeners = []

    def entry(domain):
        e = MagicMock()
        e.domain = domain
        unsubscribe = MagicMock()
        e.async_on_state_change.side_effect = lambda cb: (
            listeners.append((e, cb, unsubscribe)),
            unsubscribe,
        )[1]
        return e

    entries = {d: [entry(d)] for d in store_entries}
    hass.config_entries.async_entries.side_effect = lambda d: entries.get(d, [])
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.start", None)
    return hass, listeners


def test_no_store_integration_means_nothing_to_wait_for(monkeypatch, issue_registry):
    """A Core without HACS or the Marketplace: the check ends at once and the
    guard clears."""
    hass, listeners = _listening_hass(monkeypatch, [])

    store_records.async_schedule_store_records_check(hass)
    hass.bus.async_listen_once.call_args.args[1]()

    assert listeners == []
    issue_registry.async_delete_issue.assert_not_called()
    assert store_records._SCHEDULED_KEY not in hass.data[DOMAIN]


@pytest.mark.parametrize("store_domain", ["hacs", "marketplace"])
def test_an_unloaded_store_entry_is_checked_when_its_state_changes(
    monkeypatch, issue_registry, store_domain
):
    """The store entry still setting up, retrying or re-enabled later: the
    check runs again on its state change, files the repair once the manager
    answers, then lets go of the entry."""
    hass, listeners = _listening_hass(monkeypatch, [store_domain])
    manager = _manager(
        _repository(store_records.MAIN_REPOSITORY, True),
        _repository(store_records.MIRROR_REPOSITORY, True),
    )

    store_records.async_schedule_store_records_check(hass)
    hass.bus.async_listen_once.call_args.args[1]()
    assert len(listeners) == 1
    issue_registry.async_delete_issue.assert_not_called()
    assert hass.data[DOMAIN][store_records._SCHEDULED_KEY] is True

    _entry, on_change, unsubscribe = listeners[0]
    on_change()  # a state change while the store still is not loaded
    issue_registry.async_create_issue.assert_not_called()
    unsubscribe.assert_not_called()

    monkeypatch.setattr(store_records, "_store_manager", lambda _hass: manager)
    on_change()  # the entry reached loaded

    assert (
        issue_registry.async_create_issue.call_args.args[2]
        == ISSUE_STORE_RECORDS_CONFLICT
    )
    unsubscribe.assert_called_once()
    assert store_records._SCHEDULED_KEY not in hass.data[DOMAIN]


def test_a_registration_while_waiting_adds_no_second_listener(
    monkeypatch, issue_registry
):
    """Two entries setting up while the store is not loaded share the one
    pending check instead of each subscribing to the store."""
    hass, listeners = _listening_hass(monkeypatch, ["hacs"])

    store_records.async_schedule_store_records_check(hass)
    hass.bus.async_listen_once.call_args.args[1]()
    store_records.async_schedule_store_records_check(hass)

    assert hass.bus.async_listen_once.call_count == 1
    assert len(listeners) == 1
