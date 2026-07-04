"""Unit tests for the ha_mcp_server entry-point wiring (issue #1527).

``__init__`` is intentionally thin: generate the stable secrets, schedule the
bring-up as a config-entry background task (so a minutes-long first pip install
never stalls HA startup), reload only on a genuine options change, tear down on
unload, and revoke credentials on removal. The bring-up / teardown themselves
live in ``embedded_setup`` and are patched here.

Home Assistant / aiohttp are stubbed via ``_embedded_stubs`` (which also puts
homeassistant-integration/ on sys.path).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from ._embedded_stubs import install

install()

import ha_mcp_server as pkg  # noqa: E402
import ha_mcp_server.embedded_setup as esetup  # noqa: E402
from ha_mcp_server.const import (  # noqa: E402
    DATA_BRINGUP_TASK,
    DATA_LAST_OPTIONS,
    DATA_SECRET_PATH,
    DATA_WEBHOOK_ID,
    DOMAIN,
)


def _make_hass() -> MagicMock:
    hass = MagicMock(name="hass")
    hass.data = {}

    def _update_entry(entry, *, data=None, **_kw):
        if data is not None:
            entry.data = data

    hass.config_entries.async_update_entry = MagicMock(side_effect=_update_entry)
    hass.config_entries.async_reload = AsyncMock()
    return hass


def _make_entry(*, options=None, data=None) -> MagicMock:
    entry = MagicMock(name="entry")
    entry.entry_id = "entry-1"
    entry.options = {} if options is None else dict(options)
    entry.data = {} if data is None else dict(data)
    entry.async_create_background_task = MagicMock(return_value="BRINGUP_TASK")
    entry.add_update_listener = MagicMock(return_value="UNSUB")
    entry.async_on_unload = MagicMock()
    return entry


@pytest.fixture(autouse=True)
def _patch_orchestration(monkeypatch):
    # __init__ lazy-imports these from embedded_setup inside each entry function,
    # so patch them on the SOURCE module (the lazy `from .embedded_setup import`
    # resolves the name there at call time). async_bring_up_server is a plain
    # MagicMock (not async) so its result is never an un-awaited coroutine.
    monkeypatch.setattr(esetup, "async_bring_up_server", MagicMock(name="bring_up"))
    monkeypatch.setattr(esetup, "async_teardown_server", AsyncMock(name="teardown"))
    monkeypatch.setattr(
        esetup, "async_revoke_credentials_on_remove", AsyncMock(name="revoke")
    )


class TestEnsureSecrets:
    def test_generates_webhook_id_and_secret_when_missing(self):
        hass = _make_hass()
        entry = _make_entry()
        pkg._ensure_secrets(hass, entry)
        assert entry.data[DATA_WEBHOOK_ID].startswith("mcp_")
        assert entry.data[DATA_SECRET_PATH].startswith("/private_")

    def test_idempotent_when_present(self):
        hass = _make_hass()
        entry = _make_entry(
            data={DATA_WEBHOOK_ID: "mcp_existing", DATA_SECRET_PATH: "/private_keep"}
        )
        pkg._ensure_secrets(hass, entry)
        hass.config_entries.async_update_entry.assert_not_called()
        assert entry.data[DATA_WEBHOOK_ID] == "mcp_existing"


class TestSetupEntry:
    async def test_generates_secrets_snapshots_options_and_schedules_bringup(self):
        hass = _make_hass()
        entry = _make_entry(options={"server_port": 9584})

        result = await pkg.async_setup_entry(hass, entry)

        assert result is True
        # Secrets generated (entry.data was empty).
        assert entry.data[DATA_WEBHOOK_ID].startswith("mcp_")
        domain_data = hass.data[DOMAIN]
        # Options snapshot taken so data writes don't self-reload.
        assert domain_data[DATA_LAST_OPTIONS] == {"server_port": 9584}
        # Bring-up scheduled as a config-entry background task and stored.
        entry.async_create_background_task.assert_called_once()
        assert domain_data[DATA_BRINGUP_TASK] == "BRINGUP_TASK"
        # Reload-on-options-change listener registered under async_on_unload.
        entry.add_update_listener.assert_called_once_with(pkg._async_options_updated)
        entry.async_on_unload.assert_called_once_with("UNSUB")


class TestUnloadEntry:
    async def test_cancels_inflight_bringup_and_tears_down(self):
        hass = _make_hass()
        entry = _make_entry()
        loop = asyncio.get_running_loop()
        pending = loop.create_future()  # a still-running bring-up task
        hass.data[DOMAIN] = {DATA_BRINGUP_TASK: pending, DATA_LAST_OPTIONS: {}}

        result = await pkg.async_unload_entry(hass, entry)

        assert result is True
        assert pending.cancelled()
        esetup.async_teardown_server.assert_awaited_once()
        assert DATA_BRINGUP_TASK not in hass.data[DOMAIN]
        assert DATA_LAST_OPTIONS not in hass.data[DOMAIN]

    async def test_skips_cancel_when_bringup_done(self):
        hass = _make_hass()
        entry = _make_entry()
        loop = asyncio.get_running_loop()
        done = loop.create_future()
        done.set_result(None)
        hass.data[DOMAIN] = {DATA_BRINGUP_TASK: done, DATA_LAST_OPTIONS: {}}

        result = await pkg.async_unload_entry(hass, entry)
        assert result is True
        assert not done.cancelled()
        esetup.async_teardown_server.assert_awaited_once()


class TestRemoveEntry:
    async def test_revokes_credentials(self):
        hass = _make_hass()
        entry = _make_entry()
        await pkg.async_remove_entry(hass, entry)
        esetup.async_revoke_credentials_on_remove.assert_awaited_once_with(hass, entry)


class TestOptionsUpdatedListener:
    async def test_reloads_on_genuine_options_change(self):
        hass = _make_hass()
        entry = _make_entry(options={"server_port": 9999})
        hass.data[DOMAIN] = {DATA_LAST_OPTIONS: {"server_port": 9584}}

        await pkg._async_options_updated(hass, entry)
        hass.config_entries.async_reload.assert_awaited_once_with("entry-1")

    async def test_ignores_data_only_writes(self):
        # The background bring-up writes ids/token to entry.data (not options),
        # firing the same listener — it must NOT reload.
        hass = _make_hass()
        entry = _make_entry(options={"server_port": 9584})
        hass.data[DOMAIN] = {DATA_LAST_OPTIONS: {"server_port": 9584}}

        await pkg._async_options_updated(hass, entry)
        hass.config_entries.async_reload.assert_not_awaited()
