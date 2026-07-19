"""Unit tests for the ``update`` platform (issue #1760).

Covers the entity's properties (installed/latest/auto_update/release_url per
channel, supported_features per channel) and ``async_install``'s pending-marker
write + reload. Home Assistant / aiohttp are stubbed via ``_embedded_stubs``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ._embedded_stubs import install

install()

import custom_components.ha_mcp_tools.update as upd  # noqa: E402
from custom_components.ha_mcp_tools.const import (  # noqa: E402
    DATA_BRINGUP_TASK,
    DATA_PENDING_INSTALL_VERSION,
    DIST_NAME_DEV,
    DIST_NAME_STABLE,
    DOMAIN,
    OPT_AUTO_UPDATE,
)


def _info(*, installed="1.0.0", latest="1.1.0", dist=DIST_NAME_STABLE):
    return SimpleNamespace(installed=installed, latest=latest, dist=dist)


def _make_coordinator(data=None) -> MagicMock:
    coordinator = MagicMock(name="coordinator")
    coordinator.data = data
    coordinator.hass = MagicMock(name="hass")
    return coordinator


def _make_entry(*, options=None, data=None) -> MagicMock:
    entry = MagicMock(name="entry")
    entry.entry_id = "entry-1"
    entry.options = {} if options is None else dict(options)
    entry.data = {} if data is None else dict(data)
    return entry


def _make_entity(coordinator=None, entry=None) -> upd.ServerUpdateEntity:
    coordinator = coordinator or _make_coordinator(_info())
    entry = entry or _make_entry()
    return upd.ServerUpdateEntity(coordinator, entry)


def _make_install_hass(*, bringup=None, installed_version=None) -> MagicMock:
    """A hass wired for async_install's post-reload flow: awaits the bring-up
    task (if any) then reads the installed version via the executor."""
    hass = MagicMock(name="hass")
    hass.config_entries.async_reload = AsyncMock()
    hass.data = {DOMAIN: {DATA_BRINGUP_TASK: bringup}}

    async def _executor(_func, *_args):
        return installed_version

    hass.async_add_executor_job = AsyncMock(side_effect=_executor)
    return hass


class TestProperties:
    def test_unique_id_and_entity_name(self):
        entity = _make_entity(entry=_make_entry())
        assert entity._attr_unique_id == "entry-1_server_update"
        assert entity._attr_has_entity_name is True
        assert entity._attr_translation_key == "server_update"

    def test_installed_and_latest_version(self):
        entity = _make_entity(
            coordinator=_make_coordinator(_info(installed="1.0.0", latest="1.2.0"))
        )
        assert entity.installed_version == "1.0.0"
        assert entity.latest_version == "1.2.0"

    def test_versions_none_when_coordinator_data_absent(self):
        entity = _make_entity(coordinator=_make_coordinator(None))
        assert entity.installed_version is None
        assert entity.latest_version is None

    def test_device_info_sw_version_is_installed_version(self):
        entity = _make_entity(
            coordinator=_make_coordinator(_info(installed="1.0.0")),
            entry=_make_entry(),
        )
        info = entity.device_info
        assert info["sw_version"] == "1.0.0"
        assert info["identifiers"] == {("ha_mcp_tools", "entry-1")}
        assert info["name"] == "HA-MCP Server"

    @pytest.mark.parametrize(
        "option_value, default_used, expected",
        [
            (True, False, True),
            (False, False, False),
            (None, True, True),
        ],
    )
    def test_auto_update_reflects_entry_option(
        self, option_value, default_used, expected
    ):
        options = {} if default_used else {OPT_AUTO_UPDATE: option_value}
        entity = _make_entity(entry=_make_entry(options=options))
        assert entity.auto_update is expected

    def test_release_url_stable_channel(self):
        entity = _make_entity(
            coordinator=_make_coordinator(_info(latest="1.2.0", dist=DIST_NAME_STABLE))
        )
        assert entity.release_url == (
            "https://github.com/homeassistant-ai/ha-mcp/releases/tag/v1.2.0"
        )

    def test_release_url_stable_channel_none_when_latest_unknown(self):
        entity = _make_entity(
            coordinator=_make_coordinator(_info(latest=None, dist=DIST_NAME_STABLE))
        )
        assert entity.release_url is None

    def test_release_url_dev_channel_is_commit_history(self):
        entity = _make_entity(
            coordinator=_make_coordinator(_info(latest=None, dist=DIST_NAME_DEV))
        )
        assert entity.release_url == (
            "https://github.com/homeassistant-ai/ha-mcp/commits/master"
        )

    def test_supported_features_stable_includes_release_notes(self):
        entity = _make_entity(
            coordinator=_make_coordinator(_info(dist=DIST_NAME_STABLE))
        )
        assert entity.supported_features & upd.UpdateEntityFeature.INSTALL
        assert entity.supported_features & upd.UpdateEntityFeature.RELEASE_NOTES

    def test_supported_features_dev_excludes_release_notes(self):
        entity = _make_entity(coordinator=_make_coordinator(_info(dist=DIST_NAME_DEV)))
        assert entity.supported_features & upd.UpdateEntityFeature.INSTALL
        assert not (entity.supported_features & upd.UpdateEntityFeature.RELEASE_NOTES)

    def test_release_url_none_when_coordinator_data_absent(self):
        entity = _make_entity(coordinator=_make_coordinator(None))
        assert entity.release_url is None

    def test_supported_features_install_only_when_coordinator_data_absent(self):
        entity = _make_entity(coordinator=_make_coordinator(None))
        assert entity.supported_features == upd.UpdateEntityFeature.INSTALL


class _FakeResp:
    def __init__(self, payload, *, raise_exc=None):
        self._payload = payload
        self._raise_exc = raise_exc

    def raise_for_status(self):
        if self._raise_exc is not None:
            raise self._raise_exc

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, resp):
        self._resp = resp

    def get(self, _url):
        return self._resp


class TestReleaseNotes:
    @pytest.fixture(autouse=True)
    def _not_held(self, monkeypatch):
        # These tests cover the plain notes path; neutralize the component-hold
        # check (covered by TestReleaseNotesComponentHold) so they exercise the
        # unchanged not-held behaviour without a network hold probe.
        monkeypatch.setattr(
            upd, "_async_update_held_by_component", AsyncMock(return_value=None)
        )

    async def test_concatenates_bodies_between_installed_and_latest(self, monkeypatch):
        releases = [
            {"tag_name": "v1.3.0", "body": "too new"},
            {"tag_name": "v1.2.0", "body": "second"},
            {"tag_name": "v1.1.0", "body": "first"},
            {"tag_name": "v1.0.0", "body": "already installed"},
        ]
        session = _FakeSession(_FakeResp(releases))
        monkeypatch.setattr(
            upd, "async_get_clientsession", MagicMock(return_value=session)
        )
        entity = _make_entity(
            coordinator=_make_coordinator(_info(installed="1.0.0", latest="1.2.0"))
        )

        notes = await entity.async_release_notes()

        assert notes is not None
        assert "second" in notes
        assert "first" in notes
        assert "too new" not in notes
        assert "already installed" not in notes
        # Newest first.
        assert notes.index("second") < notes.index("first")

    async def test_no_coordinator_data_returns_none(self):
        entity = _make_entity(coordinator=_make_coordinator(None))
        assert await entity.async_release_notes() is None

    async def test_fetch_failure_returns_none(self, monkeypatch):
        # async_release_notes is advisory-only and catches broadly (any
        # network/parsing error), so any exception type demonstrates the gate.
        session = _FakeSession(_FakeResp(None, raise_exc=RuntimeError("boom")))
        monkeypatch.setattr(
            upd, "async_get_clientsession", MagicMock(return_value=session)
        )
        entity = _make_entity(coordinator=_make_coordinator(_info()))

        assert await entity.async_release_notes() is None

    async def test_malformed_payload_returns_none_and_logs_warning(
        self, monkeypatch, caplog
    ):
        import logging

        # A list of bare strings instead of release dicts - .get() on a str
        # raises AttributeError, an unexpected-shape bug/API-change, not a
        # transient - must hit the broad except and log at WARNING.
        session = _FakeSession(_FakeResp(["not", "a", "release", "dict"]))
        monkeypatch.setattr(
            upd, "async_get_clientsession", MagicMock(return_value=session)
        )
        entity = _make_entity(coordinator=_make_coordinator(_info()))

        with caplog.at_level(logging.WARNING):
            notes = await entity.async_release_notes()

        assert notes is None
        assert "release-notes fetch failed" in caplog.text

    async def test_no_qualifying_releases_returns_none(self, monkeypatch):
        session = _FakeSession(_FakeResp([{"tag_name": "v1.0.0", "body": "x"}]))
        monkeypatch.setattr(
            upd, "async_get_clientsession", MagicMock(return_value=session)
        )
        entity = _make_entity(
            coordinator=_make_coordinator(_info(installed="1.0.0", latest="1.0.0"))
        )

        assert await entity.async_release_notes() is None


class TestReleaseNotesComponentHold:
    """The release-notes dialog leads with a component-update warning while the
    pending server update is held on a newer custom component (#1783/#1785)."""

    def _held(self, monkeypatch, value):
        monkeypatch.setattr(
            upd, "_async_update_held_by_component", AsyncMock(return_value=value)
        )

    async def test_held_prepends_warning_before_notes(self, monkeypatch):
        self._held(monkeypatch, ("1.2.0", "1.0.0"))
        releases = [{"tag_name": "v1.2.0", "body": "server notes"}]
        session = _FakeSession(_FakeResp(releases))
        monkeypatch.setattr(
            upd, "async_get_clientsession", MagicMock(return_value=session)
        )
        entity = _make_entity(
            coordinator=_make_coordinator(_info(installed="1.0.0", latest="1.2.0"))
        )

        notes = await entity.async_release_notes()

        assert notes is not None
        assert notes.startswith('<ha-alert alert-type="warning">')
        # Names the shipped and running component versions, and the HACS action.
        assert "1.2.0" in notes
        assert "1.0.0" in notes
        assert "HACS" in notes
        # The real release notes still follow the warning, in that order.
        assert "server notes" in notes
        assert notes.index("warning") < notes.index("server notes")

    async def test_held_with_notes_none_returns_warning_alone(self, monkeypatch):
        # The warning must not vanish when the GitHub notes fetch fails while the
        # update is held — surfacing it is the whole point of the dialog.
        self._held(monkeypatch, ("1.2.0", "1.0.0"))
        session = _FakeSession(_FakeResp(None, raise_exc=RuntimeError("boom")))
        monkeypatch.setattr(
            upd, "async_get_clientsession", MagicMock(return_value=session)
        )
        entity = _make_entity(
            coordinator=_make_coordinator(_info(installed="1.0.0", latest="1.2.0"))
        )

        notes = await entity.async_release_notes()

        assert notes is not None
        assert notes.startswith('<ha-alert alert-type="warning">')
        assert "1.0.0" in notes
        assert "---" not in notes  # no notes body appended

    async def test_hold_check_failure_degrades_to_plain_notes(self, monkeypatch):
        # An unexpected error escaping the held-check must not break the dialog:
        # it degrades to the plain notes (as if not held).
        monkeypatch.setattr(
            upd,
            "_async_update_held_by_component",
            AsyncMock(side_effect=RuntimeError("gate boom")),
        )
        releases = [{"tag_name": "v1.2.0", "body": "server notes"}]
        session = _FakeSession(_FakeResp(releases))
        monkeypatch.setattr(
            upd, "async_get_clientsession", MagicMock(return_value=session)
        )
        entity = _make_entity(
            coordinator=_make_coordinator(_info(installed="1.0.0", latest="1.2.0"))
        )

        notes = await entity.async_release_notes()

        assert notes == "server notes"

    async def test_both_probes_failing_degrades_to_none(self, monkeypatch):
        # Both gathered probes failing at once must degrade to None (the UI's
        # release_url fallback), never propagate out of the gather.
        monkeypatch.setattr(
            upd,
            "_async_update_held_by_component",
            AsyncMock(side_effect=RuntimeError("gate boom")),
        )
        session = _FakeSession(_FakeResp(None, raise_exc=RuntimeError("boom")))
        monkeypatch.setattr(
            upd, "async_get_clientsession", MagicMock(return_value=session)
        )
        entity = _make_entity(
            coordinator=_make_coordinator(_info(installed="1.0.0", latest="1.2.0"))
        )

        notes = await entity.async_release_notes()

        assert notes is None


class TestAsyncInstall:
    async def test_writes_pending_marker_and_reloads(self):
        entry = _make_entry(data={"existing": "kept"})
        hass = _make_install_hass(installed_version="1.2.0")
        coordinator = _make_coordinator(_info(dist=DIST_NAME_STABLE, latest="1.2.0"))
        entity = _make_entity(coordinator=coordinator, entry=entry)
        entity.hass = hass

        await entity.async_install("1.2.0", backup=False)

        updated_data = hass.config_entries.async_update_entry.call_args.kwargs["data"]
        assert updated_data[DATA_PENDING_INSTALL_VERSION] == "1.2.0"
        assert updated_data["existing"] == "kept"  # existing entry.data preserved
        hass.config_entries.async_reload.assert_awaited_once_with("entry-1")

    async def test_no_version_falls_back_to_latest(self):
        entry = _make_entry()
        hass = _make_install_hass(installed_version="1.3.0")
        coordinator = _make_coordinator(_info(dist=DIST_NAME_STABLE, latest="1.3.0"))
        entity = _make_entity(coordinator=coordinator, entry=entry)
        entity.hass = hass

        await entity.async_install(None, backup=False)

        updated_data = hass.config_entries.async_update_entry.call_args.kwargs["data"]
        assert updated_data[DATA_PENDING_INSTALL_VERSION] == "1.3.0"

    async def test_no_target_version_raises_home_assistant_error(self):
        entry = _make_entry()
        hass = MagicMock(name="hass")
        coordinator = _make_coordinator(_info(latest=None))
        entity = _make_entity(coordinator=coordinator, entry=entry)
        entity.hass = hass

        with pytest.raises(upd.HomeAssistantError):
            await entity.async_install(None, backup=False)

    async def test_reload_failure_wrapped_in_home_assistant_error(self):
        entry = _make_entry()
        hass = MagicMock(name="hass")
        hass.config_entries.async_reload = AsyncMock(side_effect=RuntimeError("boom"))
        coordinator = _make_coordinator(_info(latest="1.2.0"))
        entity = _make_entity(coordinator=coordinator, entry=entry)
        entity.hass = hass

        with pytest.raises(upd.HomeAssistantError):
            await entity.async_install("1.2.0", backup=False)

    async def test_version_mismatch_after_install_raises_naming_target(self):
        # The reload only completes entry SETUP; the executor read below is the
        # actual success check (review finding) - a version that landed
        # different from the target must surface as a failure naming it.
        entry = _make_entry()
        hass = _make_install_hass(installed_version="1.1.0")  # target was 1.2.0
        coordinator = _make_coordinator(_info(dist=DIST_NAME_STABLE, latest="1.2.0"))
        entity = _make_entity(coordinator=coordinator, entry=entry)
        entity.hass = hass

        with pytest.raises(upd.HomeAssistantError, match=r"1\.2\.0"):
            await entity.async_install("1.2.0", backup=False)

    async def test_async_update_entry_raising_wrapped_and_logged(self, caplog):
        import logging

        entry = _make_entry()
        hass = _make_install_hass(installed_version="1.2.0")
        hass.config_entries.async_update_entry = MagicMock(
            side_effect=RuntimeError("entry store boom")
        )
        coordinator = _make_coordinator(_info(dist=DIST_NAME_STABLE, latest="1.2.0"))
        entity = _make_entity(coordinator=coordinator, entry=entry)
        entity.hass = hass

        with caplog.at_level(logging.ERROR), pytest.raises(upd.HomeAssistantError):
            await entity.async_install("1.2.0", backup=False)

        assert "install failed" in caplog.text
