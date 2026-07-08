"""Unit tests for the legacy-HACS-source detection (issue #1760).

Before the dedicated HACS mirror existed, the README told users to add the
main ha-mcp server repository as a HACS custom repository. Those installs
still work but HACS shows the server's version numbers and release notes for
this component. ``install_source_check`` detects that and files an advisory
repair issue pointing at the mirror.

Home Assistant / aiohttp are stubbed via ``_embedded_stubs``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

from ._embedded_stubs import install

install()

import custom_components.ha_mcp_tools.install_source_check as isc  # noqa: E402
from custom_components.ha_mcp_tools.const import (  # noqa: E402
    DOMAIN,
    HACS_COMPONENT_URL,
    ISSUE_LEGACY_HACS_SOURCE,
)


def _make_hass(*, running: bool = True) -> MagicMock:
    hass = MagicMock(name="hass")
    hass.data = {}
    hass.state = isc.CoreState.running if running else isc.CoreState.starting

    def _create_task(coro, name):
        # These tests assert scheduling decisions, not the scheduled check's
        # own behavior (covered by TestCheckInstallSource) - close the real
        # coroutine rather than run it, so it is never left un-awaited.
        if asyncio.iscoroutine(coro):
            coro.close()

    hass.async_create_task = MagicMock(side_effect=_create_task)
    return hass


def _hacs_with_repo(*, installed: bool) -> MagicMock:
    hacs = MagicMock(name="hacs")
    repo = SimpleNamespace(data=SimpleNamespace(installed=installed))
    hacs.repositories.get_by_full_name = MagicMock(return_value=repo)
    return hacs


def _hacs_without_repo() -> MagicMock:
    hacs = MagicMock(name="hacs")
    hacs.repositories.get_by_full_name = MagicMock(return_value=None)
    return hacs


class TestCheckInstallSource:
    async def test_no_hacs_deletes_stale_issue(self, monkeypatch):
        hass = _make_hass()
        create = MagicMock()
        delete = MagicMock()
        monkeypatch.setattr(isc.ir, "async_create_issue", create)
        monkeypatch.setattr(isc.ir, "async_delete_issue", delete)

        await isc._async_check_install_source(hass)

        create.assert_not_called()
        delete.assert_called_once_with(hass, DOMAIN, ISSUE_LEGACY_HACS_SOURCE)

    async def test_hacs_present_repo_not_found_deletes_issue(self, monkeypatch):
        hass = _make_hass()
        hass.data["hacs"] = _hacs_without_repo()
        create = MagicMock()
        delete = MagicMock()
        monkeypatch.setattr(isc.ir, "async_create_issue", create)
        monkeypatch.setattr(isc.ir, "async_delete_issue", delete)

        await isc._async_check_install_source(hass)

        create.assert_not_called()
        delete.assert_called_once_with(hass, DOMAIN, ISSUE_LEGACY_HACS_SOURCE)

    async def test_repo_present_but_not_installed_deletes_issue(self, monkeypatch):
        hass = _make_hass()
        hass.data["hacs"] = _hacs_with_repo(installed=False)
        create = MagicMock()
        delete = MagicMock()
        monkeypatch.setattr(isc.ir, "async_create_issue", create)
        monkeypatch.setattr(isc.ir, "async_delete_issue", delete)

        await isc._async_check_install_source(hass)

        create.assert_not_called()
        delete.assert_called_once_with(hass, DOMAIN, ISSUE_LEGACY_HACS_SOURCE)

    async def test_repo_installed_creates_warning_issue(self, monkeypatch):
        hass = _make_hass()
        hass.data["hacs"] = _hacs_with_repo(installed=True)
        create = MagicMock()
        delete = MagicMock()
        monkeypatch.setattr(isc.ir, "async_create_issue", create)
        monkeypatch.setattr(isc.ir, "async_delete_issue", delete)

        await isc._async_check_install_source(hass)

        delete.assert_not_called()
        create.assert_called_once()
        args = create.call_args.args
        kwargs = create.call_args.kwargs
        assert args[:3] == (hass, DOMAIN, ISSUE_LEGACY_HACS_SOURCE)
        assert kwargs["is_fixable"] is False
        assert kwargs["severity"] == isc.ir.IssueSeverity.WARNING
        assert kwargs["translation_key"] == ISSUE_LEGACY_HACS_SOURCE
        assert kwargs["learn_more_url"] == HACS_COMPONENT_URL

    async def test_hacs_lookup_error_logs_and_leaves_registry_untouched(
        self, monkeypatch, caplog
    ):
        import logging

        hass = _make_hass()
        broken_hacs = MagicMock(name="hacs")
        broken_hacs.repositories.get_by_full_name = MagicMock(
            side_effect=AttributeError("HACS internals changed")
        )
        hass.data["hacs"] = broken_hacs
        create = MagicMock()
        delete = MagicMock()
        monkeypatch.setattr(isc.ir, "async_create_issue", create)
        monkeypatch.setattr(isc.ir, "async_delete_issue", delete)

        with caplog.at_level(logging.WARNING):
            await isc._async_check_install_source(hass)  # must not raise

        create.assert_not_called()
        delete.assert_not_called()
        assert "could not determine the HACS install source" in caplog.text


class TestScheduling:
    def test_already_running_schedules_immediately(self):
        hass = _make_hass(running=True)

        isc.async_schedule_install_source_check(hass)

        hass.async_create_task.assert_called_once()
        hass.bus.async_listen_once.assert_not_called()

    def test_not_yet_started_registers_one_shot_listener(self):
        hass = _make_hass(running=False)

        isc.async_schedule_install_source_check(hass)

        hass.async_create_task.assert_not_called()
        hass.bus.async_listen_once.assert_called_once()
        assert hass.bus.async_listen_once.call_args.args[0] == (
            isc.EVENT_HOMEASSISTANT_STARTED
        )

    def test_listener_fires_check_when_started_event_arrives(self):
        hass = _make_hass(running=False)

        isc.async_schedule_install_source_check(hass)
        listener = hass.bus.async_listen_once.call_args.args[1]
        listener(MagicMock(name="event"))

        hass.async_create_task.assert_called_once()

    def test_second_call_schedules_only_once(self):
        hass = _make_hass(running=True)

        isc.async_schedule_install_source_check(hass)
        isc.async_schedule_install_source_check(hass)

        hass.async_create_task.assert_called_once()

    def test_second_call_before_started_does_not_register_second_listener(self):
        hass = _make_hass(running=False)

        isc.async_schedule_install_source_check(hass)
        isc.async_schedule_install_source_check(hass)

        hass.bus.async_listen_once.assert_called_once()
