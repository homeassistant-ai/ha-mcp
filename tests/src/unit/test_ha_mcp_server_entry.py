"""The server entry's lifecycle and the entry-type dispatch, on a real ``hass``.

Core sets the entries up, unloads, reloads and removes them through its own
config-entry machinery. What this pins: the stable connect URL secrets, the
background bring-up, the access switch's start / stop ordering (#2801), the
reload only on a genuine options change, credential revocation on removal, and
the routing of the two entry types (``tools`` / ``server``) the domain shares.

Only the server's bring-up / teardown and the sidebar panel are replaced
(``_server_entry``), so no server starts.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.components import switch as switch_component
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant.common import MockConfigEntry

import custom_components.ha_mcp_tools.embedded_entry as eentry
import custom_components.ha_mcp_tools.embedded_setup as esetup
from custom_components.ha_mcp_tools import const
from custom_components.ha_mcp_tools.config_flow import HaMcpToolsConfigFlow
from custom_components.ha_mcp_tools.embedded_server import EmbeddedServerManager

from ._server_entry import add_server_entry, replace_server, set_up


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    return replace_server(monkeypatch)


class BringUp:
    """A bring-up that stays running until Core cancels it."""

    def __init__(self, *, fail_on_cancel: bool = False) -> None:
        self.started = asyncio.Event()
        self.cancelled = False
        self._fail_on_cancel = fail_on_cancel

    async def run(self, *_args: object, **_kwargs: object) -> None:
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            if self._fail_on_cancel:
                raise RuntimeError("partial teardown failed") from None
            raise


def _entry_without_secrets(
    hass: HomeAssistant, options: dict[str, Any] | None = None
) -> MockConfigEntry:
    """A server entry that has never generated its webhook id or secret path."""
    entry = add_server_entry(hass, options=options)
    hass.config_entries.async_update_entry(
        entry, data={const.CONF_ENTRY_TYPE: const.ENTRY_TYPE_SERVER}
    )
    return entry


def _add_tools_entry(
    hass: HomeAssistant,
    *,
    data: dict[str, Any] | None = None,
    title: str = const.TOOLS_ENTRY_TITLE,
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=const.DOMAIN,
        title=title,
        version=HaMcpToolsConfigFlow.VERSION,
        minor_version=HaMcpToolsConfigFlow.MINOR_VERSION,
        data=data or {},
    )
    entry.add_to_hass(hass)
    return entry


def _manager(*, running: bool) -> MagicMock:
    manager = MagicMock(spec=EmbeddedServerManager)
    manager.is_running = running
    return manager


TOOLS_ENTRY_DATA = pytest.mark.parametrize(
    "data",
    [{}, {const.CONF_ENTRY_TYPE: const.ENTRY_TYPE_TOOLS}],
    ids=["no_entry_type", "explicit_tools"],
)


class TestConnectUrlSecrets:
    async def test_a_first_setup_generates_the_webhook_id_and_secret_path(
        self, hass: HomeAssistant, server: SimpleNamespace
    ) -> None:
        entry = _entry_without_secrets(hass)

        assert await set_up(hass, entry)

        assert entry.data[const.DATA_WEBHOOK_ID].startswith("mcp_")
        assert entry.data[const.DATA_SECRET_PATH].startswith("/private_")

    async def test_a_webhook_override_replaces_the_stored_id(
        self, hass: HomeAssistant, server: SimpleNamespace
    ) -> None:
        entry = add_server_entry(
            hass,
            data={const.DATA_WEBHOOK_ID: "mcp_old"},
            options={const.OPT_WEBHOOK_ID_OVERRIDE: "my_custom_hook"},
        )
        path = entry.data[const.DATA_SECRET_PATH]

        assert await set_up(hass, entry)

        assert entry.data[const.DATA_WEBHOOK_ID] == "my_custom_hook"
        assert entry.data[const.DATA_SECRET_PATH] == path

    async def test_a_secret_path_override_gets_a_leading_slash(
        self, hass: HomeAssistant, server: SimpleNamespace
    ) -> None:
        entry = add_server_entry(
            hass, options={const.OPT_SECRET_PATH_OVERRIDE: "custom_path"}
        )

        assert await set_up(hass, entry)

        assert entry.data[const.DATA_SECRET_PATH] == "/custom_path"

    async def test_regenerating_rotates_both_secrets_and_clears_the_overrides(
        self, hass: HomeAssistant, server: SimpleNamespace
    ) -> None:
        # A surviving override would silently undo the rotation on the next reload.
        entry = add_server_entry(
            hass,
            options={
                const.OPT_REGENERATE_SECRETS: True,
                const.OPT_WEBHOOK_ID_OVERRIDE: "stale_override",
                const.OPT_SECRET_PATH_OVERRIDE: "/stale_path",
            },
        )
        old = dict(entry.data)

        assert await set_up(hass, entry)

        assert entry.data[const.DATA_WEBHOOK_ID] != old[const.DATA_WEBHOOK_ID]
        assert entry.data[const.DATA_WEBHOOK_ID].startswith("mcp_")
        assert entry.data[const.DATA_SECRET_PATH] != old[const.DATA_SECRET_PATH]
        assert entry.data[const.DATA_SECRET_PATH].startswith("/private_")
        assert entry.options[const.OPT_REGENERATE_SECRETS] is False
        assert entry.options[const.OPT_WEBHOOK_ID_OVERRIDE] == ""
        assert entry.options[const.OPT_SECRET_PATH_OVERRIDE] == ""

    async def test_regenerating_wins_over_an_override(
        self, hass: HomeAssistant, server: SimpleNamespace
    ) -> None:
        entry = add_server_entry(
            hass,
            options={
                const.OPT_REGENERATE_SECRETS: True,
                const.OPT_WEBHOOK_ID_OVERRIDE: "must_not_apply",
            },
        )

        assert await set_up(hass, entry)

        assert entry.data[const.DATA_WEBHOOK_ID] != "must_not_apply"


class TestUnload:
    async def test_unload_cancels_a_running_bring_up_before_the_stop(
        self, hass: HomeAssistant, server: SimpleNamespace
    ) -> None:
        # A bring-up left running would register the webhook after the stop.
        held = BringUp()
        server.bring_up.side_effect = held.run
        seen: list[bool] = []
        server.teardown.side_effect = lambda _hass: seen.append(held.cancelled)
        entry = add_server_entry(hass)
        assert await set_up(hass, entry)
        await held.started.wait()

        assert await hass.config_entries.async_unload(entry.entry_id)

        assert seen == [True]
        server.teardown.assert_awaited_once_with(hass)

    async def test_unload_after_the_bring_up_finished_stops_the_server(
        self, hass: HomeAssistant, server: SimpleNamespace
    ) -> None:
        entry = add_server_entry(hass)
        assert await set_up(hass, entry)
        await asyncio.sleep(0)

        assert await hass.config_entries.async_unload(entry.entry_id)

        server.teardown.assert_awaited_once_with(hass)

    @pytest.mark.parametrize(
        "failure",
        [{"return_value": False}, {"side_effect": RuntimeError("boom")}],
        ids=["refused", "raised"],
    )
    async def test_the_server_stops_even_when_the_switch_platform_fails_to_unload(
        self,
        hass: HomeAssistant,
        server: SimpleNamespace,
        monkeypatch: pytest.MonkeyPatch,
        failure: dict[str, Any],
    ) -> None:
        entry = add_server_entry(hass)
        assert await set_up(hass, entry)
        monkeypatch.setattr(
            switch_component, "async_unload_entry", AsyncMock(**failure)
        )

        assert not await hass.config_entries.async_unload(entry.entry_id)

        server.teardown.assert_awaited_once_with(hass)

    async def test_unload_waits_for_a_switch_on_in_progress(
        self, hass: HomeAssistant, server: SimpleNamespace
    ) -> None:
        # A start scheduled after the unload's stop would run for an entry that
        # is no longer loaded.
        held = BringUp()
        server.bring_up.side_effect = held.run
        entry = add_server_entry(hass, data={const.DATA_ACCESS_ENABLED: False})
        assert await set_up(hass, entry)
        stopping = asyncio.Event()
        release = asyncio.Event()

        async def slow_first_stop(_hass: HomeAssistant) -> None:
            if not stopping.is_set():
                stopping.set()
                await release.wait()

        server.teardown.side_effect = slow_first_stop
        on = hass.async_create_task(eentry.async_set_access(hass, entry, True))
        await stopping.wait()
        unload = hass.async_create_task(
            hass.config_entries.async_unload(entry.entry_id)
        )
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(on, unload)

        assert server.bring_up.call_count == 1
        assert held.cancelled


class TestAccessSwitch:
    """The access switch opens and closes the server while the entry stays loaded (#2801)."""

    async def test_access_left_off_keeps_the_server_down_after_a_restart(
        self, hass: HomeAssistant, server: SimpleNamespace
    ) -> None:
        entry = add_server_entry(hass, data={const.DATA_ACCESS_ENABLED: False})

        assert await set_up(hass, entry)

        server.bring_up.assert_not_called()

    async def test_off_after_restart_option_closes_access_at_startup(
        self, hass: HomeAssistant, server: SimpleNamespace
    ) -> None:
        entry = add_server_entry(
            hass, options={const.OPT_ACCESS_OFF_AFTER_RESTART: True}
        )

        assert await set_up(hass, entry)

        assert entry.data[const.DATA_ACCESS_ENABLED] is False
        server.bring_up.assert_not_called()

    async def test_off_after_restart_option_does_not_close_access_on_a_reload(
        self, hass: HomeAssistant, server: SimpleNamespace
    ) -> None:
        # An options save reloads the entry; that is not a restart, so access the
        # user turned on stays on.
        server.bring_up.side_effect = BringUp().run
        entry = add_server_entry(
            hass, options={const.OPT_ACCESS_OFF_AFTER_RESTART: True}
        )
        assert await set_up(hass, entry)
        await eentry.async_set_access(hass, entry, True)
        server.bring_up.reset_mock()

        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

        assert entry.state is ConfigEntryState.LOADED
        assert eentry.access_enabled(entry)
        server.bring_up.assert_called_once()

    async def test_switch_starts_out_off_after_a_restart_with_the_option(
        self,
        hass: HomeAssistant,
        server: SimpleNamespace,
        entity_registry_enabled_by_default: None,
    ) -> None:
        # The switch reports the stored state when it is added; storing after
        # the platform forward would show it on while the server is down.
        entry = add_server_entry(
            hass, options={const.OPT_ACCESS_OFF_AFTER_RESTART: True}
        )

        assert await set_up(hass, entry)

        [state] = hass.states.async_all("switch")
        assert state.state == STATE_OFF

    async def test_switching_on_after_the_entry_unloaded_starts_nothing(
        self, hass: HomeAssistant, server: SimpleNamespace
    ) -> None:
        # A switch-on queued behind the unload (disable, or a slow stop) must
        # not start a server for an entry that is no longer loaded.
        entry = add_server_entry(hass, data={const.DATA_ACCESS_ENABLED: False})
        assert await set_up(hass, entry)
        assert await hass.config_entries.async_unload(entry.entry_id)

        await eentry.async_set_access(hass, entry, True)

        server.bring_up.assert_not_called()

    async def test_switching_on_works_again_after_a_reload(
        self, hass: HomeAssistant, server: SimpleNamespace
    ) -> None:
        entry = add_server_entry(hass, data={const.DATA_ACCESS_ENABLED: False})
        assert await set_up(hass, entry)
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

        await eentry.async_set_access(hass, entry, True)

        server.bring_up.assert_called_once()

    async def test_a_second_entry_starts_no_server_next_to_one_already_starting(
        self, hass: HomeAssistant, server: SimpleNamespace
    ) -> None:
        server.bring_up.side_effect = BringUp().run
        assert await set_up(hass, add_server_entry(hass))

        assert await set_up(hass, add_server_entry(hass))

        assert server.bring_up.call_count == 1

    async def test_a_second_entry_set_up_during_a_stop_waits_for_it(
        self, hass: HomeAssistant, server: SimpleNamespace
    ) -> None:
        # A start while the old server still holds the port would fail to bind.
        first = add_server_entry(hass)
        assert await set_up(hass, first)
        stopping = asyncio.Event()
        release = asyncio.Event()
        order: list[str] = []

        async def slow_first_stop(_hass: HomeAssistant) -> None:
            if not order:
                order.append("stop")
                stopping.set()
                await release.wait()
                order.append("stop_done")

        async def record_bring_up(*_args: object, **_kwargs: object) -> None:
            order.append("bring_up")

        server.teardown.side_effect = slow_first_stop
        server.bring_up.side_effect = record_bring_up
        unload = hass.async_create_task(
            hass.config_entries.async_unload(first.entry_id)
        )
        await stopping.wait()
        setup = hass.async_create_task(set_up(hass, add_server_entry(hass)))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(unload, setup)

        assert order == ["stop", "stop_done", "bring_up"]

    async def test_switching_access_keeps_the_connect_url(
        self, hass: HomeAssistant, server: SimpleNamespace
    ) -> None:
        entry = add_server_entry(hass)
        assert await set_up(hass, entry)
        url = (entry.data[const.DATA_WEBHOOK_ID], entry.data[const.DATA_SECRET_PATH])

        await eentry.async_set_access(hass, entry, False)

        assert (
            entry.data[const.DATA_WEBHOOK_ID],
            entry.data[const.DATA_SECRET_PATH],
        ) == url

    async def test_switching_off_cancels_a_running_bring_up_before_teardown(
        self, hass: HomeAssistant, server: SimpleNamespace
    ) -> None:
        # A bring-up still in flight would otherwise register the webhook and the
        # LLM API after the teardown, reopening access.
        held = BringUp()
        server.bring_up.side_effect = held.run
        seen: list[bool] = []
        server.teardown.side_effect = lambda _hass: seen.append(held.cancelled)
        entry = add_server_entry(hass)
        assert await set_up(hass, entry)
        await held.started.wait()

        await eentry.async_set_access(hass, entry, False)

        assert seen == [True]
        assert entry.data[const.DATA_ACCESS_ENABLED] is False

    async def test_switching_on_starts_the_server_without_a_notification(
        self, hass: HomeAssistant, server: SimpleNamespace
    ) -> None:
        entry = add_server_entry(hass, data={const.DATA_ACCESS_ENABLED: False})
        assert await set_up(hass, entry)

        await eentry.async_set_access(hass, entry, True)

        server.bring_up.assert_called_once_with(hass, entry, announce=False)
        assert eentry.access_enabled(entry)

    async def test_switching_on_while_a_start_runs_starts_no_second_server(
        self, hass: HomeAssistant, server: SimpleNamespace
    ) -> None:
        server.bring_up.side_effect = BringUp().run
        entry = add_server_entry(hass)
        assert await set_up(hass, entry)

        await eentry.async_set_access(hass, entry, True)

        assert server.bring_up.call_count == 1
        server.teardown.assert_not_awaited()

    @pytest.mark.parametrize(
        "failed_start", [True, False], ids=["no_server", "crashed"]
    )
    async def test_switching_on_again_retries_a_failed_start(
        self, hass: HomeAssistant, server: SimpleNamespace, failed_start: bool
    ) -> None:
        # A failed bring-up, or a server that crashed later, leaves access on
        # with nothing running; turning the switch on again retries the start.
        entry = add_server_entry(hass)
        assert await set_up(hass, entry)
        await asyncio.sleep(0)
        if not failed_start:
            hass.data[const.DOMAIN][const.DATA_MANAGER] = _manager(running=False)

        await eentry.async_set_access(hass, entry, True)

        server.teardown.assert_awaited_once_with(hass)
        assert server.bring_up.call_count == 2

    async def test_switching_on_leaves_a_running_server_alone(
        self, hass: HomeAssistant, server: SimpleNamespace
    ) -> None:
        # An automation turning the switch on while it is on must not drop the
        # connected clients.
        entry = add_server_entry(hass)
        assert await set_up(hass, entry)
        await asyncio.sleep(0)
        hass.data[const.DOMAIN][const.DATA_MANAGER] = _manager(running=True)

        await eentry.async_set_access(hass, entry, True)

        server.teardown.assert_not_awaited()
        assert server.bring_up.call_count == 1

    async def test_switching_on_keeps_access_off_when_the_cleanup_fails(
        self, hass: HomeAssistant, server: SimpleNamespace
    ) -> None:
        entry = add_server_entry(hass, data={const.DATA_ACCESS_ENABLED: False})
        assert await set_up(hass, entry)
        server.teardown.side_effect = RuntimeError("stop failed")

        with pytest.raises(RuntimeError):
            await eentry.async_set_access(hass, entry, True)

        assert not eentry.access_enabled(entry)

    async def test_switching_off_again_retries_the_stop(
        self, hass: HomeAssistant, server: SimpleNamespace
    ) -> None:
        # A stop that raised leaves access stored off; the next off must not be
        # skipped as already done.
        entry = add_server_entry(hass, data={const.DATA_ACCESS_ENABLED: False})
        assert await set_up(hass, entry)

        await eentry.async_set_access(hass, entry, False)

        server.teardown.assert_awaited_once_with(hass)

    async def test_switching_on_waits_for_a_stop_in_progress(
        self, hass: HomeAssistant, server: SimpleNamespace
    ) -> None:
        # A start overlapping a slow stop would claim the server port while the
        # old server still holds it.
        entry = add_server_entry(hass)
        assert await set_up(hass, entry)
        stopping = asyncio.Event()
        release = asyncio.Event()
        order: list[str] = []

        async def slow_first_stop(_hass: HomeAssistant) -> None:
            if not order:
                order.append("stop")
                stopping.set()
                await release.wait()
                order.append("stop_done")

        async def record_bring_up(*_args: object, **_kwargs: object) -> None:
            order.append("bring_up")

        server.teardown.side_effect = slow_first_stop
        server.bring_up.side_effect = record_bring_up
        off = hass.async_create_task(eentry.async_set_access(hass, entry, False))
        await stopping.wait()
        on = hass.async_create_task(eentry.async_set_access(hass, entry, True))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(off, on)

        assert order == ["stop", "stop_done", "bring_up"]

    async def test_switching_off_stops_the_server_when_the_cancelled_start_fails(
        self, hass: HomeAssistant, server: SimpleNamespace
    ) -> None:
        # A start cancelled mid-way tears down its partial state itself; when
        # that raises, the stop must still run.
        held = BringUp(fail_on_cancel=True)
        server.bring_up.side_effect = held.run
        entry = add_server_entry(hass)
        assert await set_up(hass, entry)
        await held.started.wait()

        with pytest.raises(RuntimeError):
            await eentry.async_set_access(hass, entry, False)

        server.teardown.assert_awaited_once_with(hass)


class TestOptionsChanges:
    async def test_a_changed_server_option_reloads_the_server(
        self, hass: HomeAssistant, server: SimpleNamespace
    ) -> None:
        entry = add_server_entry(hass, options={"server_port": 9584})
        assert await set_up(hass, entry)

        hass.config_entries.async_update_entry(entry, options={"server_port": 9999})
        await hass.async_block_till_done()

        server.teardown.assert_awaited_once_with(hass)
        assert server.bring_up.call_count == 2

    async def test_the_bring_ups_data_writes_do_not_reload_the_server(
        self, hass: HomeAssistant, server: SimpleNamespace
    ) -> None:
        # The background bring-up writes ids and tokens to entry.data, which
        # fires the same update listener.
        entry = add_server_entry(hass, options={"server_port": 9584})
        assert await set_up(hass, entry)

        hass.config_entries.async_update_entry(
            entry, data={**entry.data, "adopted": "x"}
        )
        await hass.async_block_till_done()

        server.teardown.assert_not_awaited()
        assert server.bring_up.call_count == 1

    @pytest.mark.parametrize(
        ("option", "value"),
        [
            (const.OPT_OAUTH_REDIRECT_ALLOWLIST, []),
            (const.OPT_ACCESS_OFF_AFTER_RESTART, True),
        ],
        ids=["callback_allowlist", "off_after_restart"],
    )
    async def test_options_read_per_request_do_not_reload_the_server(
        self,
        hass: HomeAssistant,
        server: SimpleNamespace,
        option: str,
        value: object,
    ) -> None:
        # The settings panel saves the allowlist through the running server; a
        # reload would tear that server down mid-request (#2427).
        entry = add_server_entry(hass, options={"server_port": 9584})
        assert await set_up(hass, entry)

        hass.config_entries.async_update_entry(
            entry, options={**entry.options, option: value}
        )
        await hass.async_block_till_done()

        server.teardown.assert_not_awaited()
        assert server.bring_up.call_count == 1


class TestRemoveEntry:
    async def test_removing_the_server_entry_revokes_its_credentials(
        self,
        hass: HomeAssistant,
        server: SimpleNamespace,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        revoke = AsyncMock(wraps=esetup.async_revoke_credentials_on_remove)
        monkeypatch.setattr(esetup, "async_revoke_credentials_on_remove", revoke)
        entry = add_server_entry(hass)
        assert await set_up(hass, entry)

        await hass.config_entries.async_remove(entry.entry_id)

        revoke.assert_awaited_once_with(hass, entry)

    async def test_removing_the_tools_entry_revokes_nothing(
        self,
        hass: HomeAssistant,
        server: SimpleNamespace,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        revoke = AsyncMock(wraps=esetup.async_revoke_credentials_on_remove)
        monkeypatch.setattr(esetup, "async_revoke_credentials_on_remove", revoke)
        entry = _add_tools_entry(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)

        await hass.config_entries.async_remove(entry.entry_id)

        revoke.assert_not_awaited()


class TestDomainDispatch:
    """One domain holds both entry types, told apart by ``entry_type``."""

    @TOOLS_ENTRY_DATA
    async def test_a_tools_entry_sets_up_the_services_and_no_server(
        self, hass: HomeAssistant, server: SimpleNamespace, data: dict[str, Any]
    ) -> None:
        # Entries from before the server entry carry no entry_type and must
        # route to the tools setup with no migration.
        entry = _add_tools_entry(hass, data=data)

        assert await hass.config_entries.async_setup(entry.entry_id)

        assert entry.state is ConfigEntryState.LOADED
        assert hass.services.async_services_internal().get(const.DOMAIN)
        server.bring_up.assert_not_called()

    @TOOLS_ENTRY_DATA
    async def test_unloading_a_tools_entry_removes_the_services_and_stops_no_server(
        self, hass: HomeAssistant, server: SimpleNamespace, data: dict[str, Any]
    ) -> None:
        entry = _add_tools_entry(hass, data=data)
        assert await hass.config_entries.async_setup(entry.entry_id)

        assert await hass.config_entries.async_unload(entry.entry_id)

        assert entry.state is ConfigEntryState.NOT_LOADED
        assert not hass.services.async_services_internal().get(const.DOMAIN)
        server.teardown.assert_not_awaited()


class TestToolsEntrySetup:
    async def test_the_pre_rename_default_title_is_migrated(
        self, hass: HomeAssistant, server: SimpleNamespace
    ) -> None:
        entry = _add_tools_entry(hass, title=const.TOOLS_ENTRY_LEGACY_TITLE)

        assert await hass.config_entries.async_setup(entry.entry_id)

        assert entry.title == const.TOOLS_ENTRY_TITLE

    async def test_a_title_the_user_chose_is_kept(
        self, hass: HomeAssistant, server: SimpleNamespace
    ) -> None:
        entry = _add_tools_entry(hass, title="My file tools")

        assert await hass.config_entries.async_setup(entry.entry_id)

        assert entry.title == "My file tools"

    async def test_the_tools_entry_gets_a_device(
        self,
        hass: HomeAssistant,
        server: SimpleNamespace,
        device_registry: dr.DeviceRegistry,
    ) -> None:
        entry = _add_tools_entry(hass)

        assert await hass.config_entries.async_setup(entry.entry_id)

        [device] = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
        assert device.name == const.TOOLS_ENTRY_TITLE
