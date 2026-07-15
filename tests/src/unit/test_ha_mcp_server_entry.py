"""Unit tests for the in-process server entry-point wiring (issue #1527).

``embedded_entry`` is intentionally thin: generate the stable secrets, schedule
the bring-up as a config-entry background task (so a minutes-long first pip
install never stalls HA startup), reload only on a genuine options change, tear
down on unload, and revoke credentials on removal. The bring-up / teardown
themselves live in ``embedded_setup`` and are patched here.

The domain dispatcher in the package ``__init__`` routes the two entry types
(``tools`` / ``server``, discriminated by ``entry.data[entry_type]``, missing =
``tools``) to their respective setup/unload/remove functions — exercised by
``TestDomainDispatch`` below.

Home Assistant / aiohttp are stubbed via ``_embedded_stubs``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from ._embedded_stubs import install

install()

import custom_components.ha_mcp_tools as component  # noqa: E402
import custom_components.ha_mcp_tools.embedded_entry as pkg  # noqa: E402
import custom_components.ha_mcp_tools.embedded_setup as esetup  # noqa: E402
from custom_components.ha_mcp_tools.const import (  # noqa: E402
    CONF_ENTRY_TYPE,
    DATA_BRINGUP_TASK,
    DATA_LAST_OPTIONS,
    DATA_SECRET_PATH,
    DATA_WEBHOOK_ID,
    DOMAIN,
    ENTRY_TYPE_SERVER,
    ENTRY_TYPE_TOOLS,
)


def _make_hass() -> MagicMock:
    hass = MagicMock(name="hass")
    hass.data = {}

    def _update_entry(entry, *, data=None, options=None, **_kw):
        if data is not None:
            entry.data = data
        if options is not None:
            entry.options = options

    hass.config_entries.async_update_entry = MagicMock(side_effect=_update_entry)
    hass.config_entries.async_reload = AsyncMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    return hass


def _make_entry(*, options=None, data=None) -> MagicMock:
    entry = MagicMock(name="entry")
    entry.entry_id = "entry-1"
    entry.options = {} if options is None else dict(options)
    entry.data = {} if data is None else dict(data)

    def _create_background_task(hass, coro, name):
        # issue #1760: async_setup_server_entry now ALSO schedules the
        # coordinator's initial version refresh as a real coroutine here (a
        # second call, alongside the bring-up). This suite doesn't exercise
        # coordinator behavior, so just close it to avoid an unawaited-
        # coroutine warning rather than actually running it.
        if asyncio.iscoroutine(coro):
            coro.close()
        return "BRINGUP_TASK"

    entry.async_create_background_task = MagicMock(side_effect=_create_background_task)
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

    def test_webhook_override_replaces_stored_id(self):
        hass = _make_hass()
        entry = _make_entry(
            data={DATA_WEBHOOK_ID: "mcp_old", DATA_SECRET_PATH: "/private_keep"},
            options={pkg.OPT_WEBHOOK_ID_OVERRIDE: "my_custom_hook"},
        )
        pkg._ensure_secrets(hass, entry)
        assert entry.data[DATA_WEBHOOK_ID] == "my_custom_hook"
        assert entry.data[DATA_SECRET_PATH] == "/private_keep"

    def test_secret_path_override_gets_leading_slash(self):
        hass = _make_hass()
        entry = _make_entry(
            data={DATA_WEBHOOK_ID: "mcp_keep", DATA_SECRET_PATH: "/private_old"},
            options={pkg.OPT_SECRET_PATH_OVERRIDE: "custom_path"},
        )
        pkg._ensure_secrets(hass, entry)
        assert entry.data[DATA_SECRET_PATH] == "/custom_path"

    def test_regenerate_mints_fresh_and_clears_flag_and_overrides(self):
        # One-shot rotation: fresh random values for BOTH secrets, the flag
        # cleared, and the overrides cleared (a surviving override would
        # silently undo the rotation on the next reload).
        hass = _make_hass()
        entry = _make_entry(
            data={DATA_WEBHOOK_ID: "mcp_old", DATA_SECRET_PATH: "/private_old"},
            options={
                pkg.OPT_REGENERATE_SECRETS: True,
                pkg.OPT_WEBHOOK_ID_OVERRIDE: "stale_override",
                pkg.OPT_SECRET_PATH_OVERRIDE: "/stale_path",
            },
        )
        pkg._ensure_secrets(hass, entry)
        assert entry.data[DATA_WEBHOOK_ID].startswith("mcp_")
        assert entry.data[DATA_WEBHOOK_ID] != "mcp_old"
        assert entry.data[DATA_SECRET_PATH].startswith("/private_")
        assert entry.data[DATA_SECRET_PATH] != "/private_old"
        assert entry.options[pkg.OPT_REGENERATE_SECRETS] is False
        assert entry.options[pkg.OPT_WEBHOOK_ID_OVERRIDE] == ""
        assert entry.options[pkg.OPT_SECRET_PATH_OVERRIDE] == ""

    def test_regenerate_wins_over_overrides(self):
        # Priority: regenerate ignores the override values entirely.
        hass = _make_hass()
        entry = _make_entry(
            data={DATA_WEBHOOK_ID: "mcp_old", DATA_SECRET_PATH: "/private_old"},
            options={
                pkg.OPT_REGENERATE_SECRETS: True,
                pkg.OPT_WEBHOOK_ID_OVERRIDE: "must_not_apply",
            },
        )
        pkg._ensure_secrets(hass, entry)
        assert entry.data[DATA_WEBHOOK_ID] != "must_not_apply"


class TestSetupEntry:
    async def test_generates_secrets_snapshots_options_and_schedules_bringup(self):
        hass = _make_hass()
        entry = _make_entry(options={"server_port": 9584})

        result = await pkg.async_setup_server_entry(hass, entry)

        assert result is True
        # Secrets generated (entry.data was empty).
        assert entry.data[DATA_WEBHOOK_ID].startswith("mcp_")
        domain_data = hass.data[DOMAIN]
        # Options snapshot taken so data writes don't self-reload.
        assert domain_data[DATA_LAST_OPTIONS] == {"server_port": 9584}
        # Bring-up AND the coordinator's initial version refresh are both
        # scheduled as config-entry background tasks (issue #1760).
        assert entry.async_create_background_task.call_count == 2
        assert domain_data[DATA_BRINGUP_TASK] == "BRINGUP_TASK"
        # Reload-on-options-change listener AND the coordinator's auto-update
        # listener are both registered under async_on_unload for cleanup.
        entry.add_update_listener.assert_called_once_with(pkg._async_options_updated)
        unload_args = [c.args[0] for c in entry.async_on_unload.call_args_list]
        assert "UNSUB" in unload_args  # options-change listener unsub
        assert len(unload_args) == 2  # + the coordinator listener unsub
        # The update platform entity is forwarded (issue #1760).
        hass.config_entries.async_forward_entry_setups.assert_awaited_once_with(
            entry, [pkg.Platform.UPDATE]
        )


class TestUnloadEntry:
    async def test_cancels_inflight_bringup_and_tears_down(self):
        hass = _make_hass()
        entry = _make_entry()
        loop = asyncio.get_running_loop()
        pending = loop.create_future()  # a still-running bring-up task
        hass.data[DOMAIN] = {DATA_BRINGUP_TASK: pending, DATA_LAST_OPTIONS: {}}

        result = await pkg.async_unload_server_entry(hass, entry)

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

        result = await pkg.async_unload_server_entry(hass, entry)
        assert result is True
        assert not done.cancelled()
        esetup.async_teardown_server.assert_awaited_once()


class TestRemoveEntry:
    async def test_revokes_credentials(self):
        hass = _make_hass()
        entry = _make_entry()
        await pkg.async_remove_server_entry(hass, entry)
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


class TestDomainDispatch:
    """The package ``__init__`` dispatcher routes on ``entry.data[entry_type]``.

    Both entry types live under one domain; the server functions are lazy-imported
    from ``embedded_entry`` inside each dispatcher, so they are patched on that
    module (``pkg``). The tools functions are patched on the package.
    """

    @pytest.fixture(autouse=True)
    def _patch_dispatch_targets(self, monkeypatch):
        monkeypatch.setattr(
            component, "_async_setup_tools_entry", AsyncMock(return_value=True)
        )
        monkeypatch.setattr(
            component, "_async_unload_tools_entry", AsyncMock(return_value=True)
        )
        monkeypatch.setattr(
            pkg, "async_setup_server_entry", AsyncMock(return_value=True)
        )
        monkeypatch.setattr(
            pkg, "async_unload_server_entry", AsyncMock(return_value=True)
        )
        monkeypatch.setattr(pkg, "async_remove_server_entry", AsyncMock())

    async def test_server_entry_setup_routes_to_server(self):
        entry = _make_entry(data={CONF_ENTRY_TYPE: ENTRY_TYPE_SERVER})
        assert await component.async_setup_entry(_make_hass(), entry) is True
        pkg.async_setup_server_entry.assert_awaited_once()
        component._async_setup_tools_entry.assert_not_awaited()

    async def test_missing_entry_type_setup_defaults_to_tools(self):
        # Pre-existing services entries carry no entry_type key — they must route
        # to the tools setup, never the server setup, with no migration.
        entry = _make_entry(data={})
        assert await component.async_setup_entry(_make_hass(), entry) is True
        component._async_setup_tools_entry.assert_awaited_once()
        pkg.async_setup_server_entry.assert_not_awaited()

    async def test_explicit_tools_entry_setup_routes_to_tools(self):
        entry = _make_entry(data={CONF_ENTRY_TYPE: ENTRY_TYPE_TOOLS})
        assert await component.async_setup_entry(_make_hass(), entry) is True
        component._async_setup_tools_entry.assert_awaited_once()
        pkg.async_setup_server_entry.assert_not_awaited()

    async def test_server_entry_unload_routes_to_server(self):
        entry = _make_entry(data={CONF_ENTRY_TYPE: ENTRY_TYPE_SERVER})
        assert await component.async_unload_entry(_make_hass(), entry) is True
        pkg.async_unload_server_entry.assert_awaited_once()
        component._async_unload_tools_entry.assert_not_awaited()

    async def test_missing_entry_type_unload_defaults_to_tools(self):
        entry = _make_entry(data={})
        assert await component.async_unload_entry(_make_hass(), entry) is True
        component._async_unload_tools_entry.assert_awaited_once()
        pkg.async_unload_server_entry.assert_not_awaited()

    async def test_server_entry_remove_revokes(self):
        entry = _make_entry(data={CONF_ENTRY_TYPE: ENTRY_TYPE_SERVER})
        await component.async_remove_entry(_make_hass(), entry)
        pkg.async_remove_server_entry.assert_awaited_once()

    async def test_tools_entry_remove_is_noop(self):
        entry = _make_entry(data={})
        await component.async_remove_entry(_make_hass(), entry)
        pkg.async_remove_server_entry.assert_not_awaited()


class TestToolsEntrySetupFinalization:
    """Source-level guards for the #1853 finalization in the tools setup.

    ``_async_setup_tools_entry`` cannot run in this unit harness (it drives the
    caller-token Store, the legacy-backup migration, ~10 service registrations,
    and the WebSocket registry), so — like the existing security-regression guard
    on the same function — these assert the wiring at the source level: the
    rename migration and the tools-entry device registration must stay present.
    A behavioral retitle/preserve test needs the full setup scaffolding no unit
    harness provides.
    """

    def test_setup_migrates_the_pre_rename_default_title(self):
        import inspect

        src = inspect.getsource(component._async_setup_tools_entry)
        # Retitle only the exact old default, via async_update_entry, to the new
        # title constant — never a hardcoded literal that could drift from it.
        assert "entry.title == TOOLS_ENTRY_LEGACY_TITLE" in src
        assert "TOOLS_ENTRY_TITLE" in src
        assert "async_update_entry" in src

    def test_setup_registers_a_device_for_the_tools_entry(self):
        import inspect

        src = inspect.getsource(component._async_setup_tools_entry)
        assert "async_get_or_create" in src
        assert "config_entry_id=entry.entry_id" in src
        assert "File & YAML editing services" in src
        assert "homeassistant-ai" in src
