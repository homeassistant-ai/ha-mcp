"""Unit tests for :class:`ServerVersionCoordinator` (issue #1760).

Covers channel/dist selection, the PyPI-latest fetch (moved here from the old
``embedded_setup.async_check_for_update``), and the pip-spec-override skip.
Home Assistant / aiohttp are stubbed via ``_embedded_stubs``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from ._embedded_stubs import install

install()

import custom_components.ha_mcp_tools.coordinator as coord  # noqa: E402
from custom_components.ha_mcp_tools.const import (  # noqa: E402
    CHANNEL_DEV,
    DEFAULT_PIP_SPEC,
    DIST_NAME_DEV,
    DIST_NAME_STABLE,
    OPT_CHANNEL,
    OPT_PIP_SPEC,
    UPDATE_CHECK_INTERVAL,
)


def _make_hass() -> MagicMock:
    hass = MagicMock(name="hass")

    async def _executor(func, *args):
        return func(*args)

    hass.async_add_executor_job = AsyncMock(side_effect=_executor)
    return hass


def _make_entry(*, options=None) -> MagicMock:
    entry = MagicMock(name="entry")
    entry.options = {} if options is None else dict(options)
    entry.data = {}
    entry.entry_id = "entry-1"
    return entry


class _FakeResp:
    """aiohttp response stand-in: an async context manager with json/raise."""

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
    """aiohttp ClientSession stand-in recording the URLs fetched."""

    def __init__(self, resp):
        self._resp = resp
        self.get_urls: list[str] = []

    def get(self, url):
        self.get_urls.append(url)
        return self._resp


def _patch_session(monkeypatch, session):
    monkeypatch.setattr(
        coord, "async_get_clientsession", MagicMock(return_value=session)
    )


class TestConstruction:
    def test_schedules_on_update_check_interval(self):
        hass = _make_hass()
        entry = _make_entry()
        coordinator = coord.ServerVersionCoordinator(hass, entry)
        assert coordinator.update_interval == UPDATE_CHECK_INTERVAL
        assert coordinator.config_entry is entry


class TestUpdateData:
    async def test_stable_channel_fetches_stable_dist(self, monkeypatch):
        hass = _make_hass()
        entry = _make_entry()
        session = _FakeSession(_FakeResp({"info": {"version": "7.10.0"}}))
        _patch_session(monkeypatch, session)
        monkeypatch.setattr(coord, "_installed_dist_version", lambda dist: "7.9.0")

        info = await coord.ServerVersionCoordinator(hass, entry)._async_update_data()

        assert session.get_urls == [coord.PYPI_JSON_URL.format(dist=DIST_NAME_STABLE)]
        assert info == coord.ServerVersionInfo(
            installed="7.9.0", latest="7.10.0", dist=DIST_NAME_STABLE
        )

    async def test_dev_channel_fetches_dev_dist(self, monkeypatch):
        hass = _make_hass()
        entry = _make_entry(options={OPT_CHANNEL: CHANNEL_DEV})
        session = _FakeSession(_FakeResp({"info": {"version": "7.10.0.dev1"}}))
        _patch_session(monkeypatch, session)
        monkeypatch.setattr(coord, "_installed_dist_version", lambda dist: "7.9.0.dev1")

        info = await coord.ServerVersionCoordinator(hass, entry)._async_update_data()

        assert session.get_urls == [coord.PYPI_JSON_URL.format(dist=DIST_NAME_DEV)]
        assert info.dist == DIST_NAME_DEV

    async def test_not_installed_yet_still_fetches_latest(self, monkeypatch):
        # Visibility must not depend on the package being installed yet - the
        # entity should show "latest available" even before the first install.
        hass = _make_hass()
        entry = _make_entry()
        session = _FakeSession(_FakeResp({"info": {"version": "7.10.0"}}))
        _patch_session(monkeypatch, session)
        monkeypatch.setattr(coord, "_installed_dist_version", lambda dist: None)

        info = await coord.ServerVersionCoordinator(hass, entry)._async_update_data()

        assert info.installed is None
        assert info.latest == "7.10.0"

    async def test_override_skips_pypi_fetch(self, monkeypatch):
        # An explicit pip-spec override makes a PyPI-latest comparison
        # meaningless - no fetch at all, latest stays None.
        hass = _make_hass()
        entry = _make_entry(options={OPT_PIP_SPEC: "ha-mcp==7.8.0"})
        get_session = MagicMock()
        monkeypatch.setattr(coord, "async_get_clientsession", get_session)
        monkeypatch.setattr(coord, "_installed_dist_version", lambda dist: "7.8.0")

        info = await coord.ServerVersionCoordinator(hass, entry)._async_update_data()

        get_session.assert_not_called()
        assert info == coord.ServerVersionInfo(
            installed="7.8.0", latest=None, dist=DIST_NAME_STABLE
        )

    async def test_default_pip_spec_value_is_not_an_override(self, monkeypatch):
        # The default pip-spec ("ha-mcp") stored verbatim still means "no
        # override" - the fetch must run, not skip.
        hass = _make_hass()
        entry = _make_entry(options={OPT_PIP_SPEC: DEFAULT_PIP_SPEC})
        session = _FakeSession(_FakeResp({"info": {"version": "7.10.0"}}))
        _patch_session(monkeypatch, session)
        monkeypatch.setattr(coord, "_installed_dist_version", lambda dist: "7.9.0")

        info = await coord.ServerVersionCoordinator(hass, entry)._async_update_data()

        assert info.latest == "7.10.0"

    @pytest.mark.parametrize(
        "raise_exc",
        [
            coord.ClientError("boom"),
            TimeoutError(),
            KeyError("info"),
            ValueError("bad json"),
        ],
    )
    async def test_pypi_fetch_failure_returns_latest_none(self, monkeypatch, raise_exc):
        hass = _make_hass()
        entry = _make_entry()
        resp = _FakeResp(None, raise_exc=raise_exc)
        session = _FakeSession(resp)
        _patch_session(monkeypatch, session)
        monkeypatch.setattr(coord, "_installed_dist_version", lambda dist: "7.9.0")

        info = await coord.ServerVersionCoordinator(hass, entry)._async_update_data()

        assert info.installed == "7.9.0"
        assert info.latest is None
