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
    assert set(call.kwargs["translation_placeholders"]["records"].split(", ")) == {
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


def test_an_unknown_manager_shape_is_left_alone(monkeypatch, issue_registry):
    hass = _hass_with(monkeypatch, SimpleNamespace(repositories=object()))

    store_records.async_check_store_records(hass)

    issue_registry.async_create_issue.assert_not_called()
