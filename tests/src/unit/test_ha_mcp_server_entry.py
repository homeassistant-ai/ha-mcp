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
import importlib.util
from unittest.mock import AsyncMock, MagicMock

import pytest

from ._embedded_stubs import install

install()

import custom_components.ha_mcp_tools as component  # noqa: E402
import custom_components.ha_mcp_tools.embedded_entry as pkg  # noqa: E402
import custom_components.ha_mcp_tools.embedded_setup as esetup  # noqa: E402
from custom_components.ha_mcp_tools.const import (  # noqa: E402
    CONF_ENTRY_TYPE,
    DATA_ACCESS_ENABLED,
    DATA_BRINGUP_TASK,
    DATA_LAST_OPTIONS,
    DATA_MANAGER,
    DATA_SECRET_PATH,
    DATA_WEBHOOK_ID,
    DOMAIN,
    ENTRY_TYPE_SERVER,
    ENTRY_TYPE_TOOLS,
    OPT_ACCESS_OFF_AFTER_RESTART,
    OPT_OAUTH_REDIRECT_ALLOWLIST,
)
from custom_components.ha_mcp_tools.embedded_server import (  # noqa: E402
    EmbeddedServerManager,
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
        # Close any real coroutine handed in rather than running it, so a
        # scheduled task cannot leave an unawaited-coroutine warning.
        if asyncio.iscoroutine(coro):
            coro.close()
        return "BRINGUP_TASK"

    entry.async_create_background_task = MagicMock(side_effect=_create_background_task)
    entry.add_update_listener = MagicMock(return_value="UNSUB")
    entry.async_on_unload = MagicMock()
    return entry


def _pending_task(*_args: object) -> asyncio.Future[None]:
    """A bring-up still running, for tests that switch or unload after setup."""
    return asyncio.get_running_loop().create_future()


def _finished_future() -> asyncio.Future:
    task = asyncio.get_running_loop().create_future()
    task.set_result(None)
    return task


def _manager(*, running: bool) -> MagicMock:
    manager = MagicMock(spec=EmbeddedServerManager)
    manager.is_running = running
    return manager


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
        # Only the bring-up runs in the background: the server arrives with
        # the component release (#2427), so there is no version refresh.
        assert entry.async_create_background_task.call_count == 1
        assert domain_data[DATA_BRINGUP_TASK] == "BRINGUP_TASK"
        # The reload-on-options-change listener is registered for cleanup.
        entry.add_update_listener.assert_called_once_with(pkg._async_options_updated)
        unload_args = [c.args[0] for c in entry.async_on_unload.call_args_list]
        assert unload_args == ["UNSUB"]


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

    async def test_every_forwarded_platform_exists(self) -> None:
        hass = _make_hass()
        entry = _make_entry()

        await pkg.async_setup_server_entry(hass, entry)

        forwarded = hass.config_entries.async_forward_entry_setups.await_args.args[1]
        assert forwarded
        for platform in forwarded:
            assert importlib.util.find_spec(
                f"custom_components.ha_mcp_tools.{platform}"
            ), platform

    async def test_failed_platform_unload_is_reported_and_still_stops_the_server(
        self,
    ) -> None:
        hass = _make_hass()
        entry = _make_entry()
        hass.config_entries.async_unload_platforms.return_value = False

        assert await pkg.async_unload_server_entry(hass, entry) is False
        esetup.async_teardown_server.assert_awaited_once_with(hass)

    async def test_server_stops_even_when_the_switch_platform_fails_to_unload(
        self,
    ) -> None:
        hass = _make_hass()
        hass.data[DOMAIN] = {DATA_LAST_OPTIONS: {}}
        entry = _make_entry()
        hass.config_entries.async_unload_platforms.side_effect = RuntimeError("boom")

        with pytest.raises(RuntimeError):
            await pkg.async_unload_server_entry(hass, entry)

        esetup.async_teardown_server.assert_awaited_once_with(hass)
        assert DATA_LAST_OPTIONS not in hass.data[DOMAIN]

    async def test_unload_waits_for_a_switch_on_in_progress(self) -> None:
        # A start scheduled after the unload's stop would run for an entry that
        # is no longer loaded.
        hass = _make_hass()
        entry = _make_entry(data={DATA_ACCESS_ENABLED: False})
        started: list[asyncio.Future] = []

        def _record(*_args: object) -> asyncio.Future[None]:
            started.append(_pending_task())
            return started[-1]

        entry.async_create_background_task.side_effect = _record
        release = asyncio.Event()

        async def _slow_first_teardown(_hass: object) -> None:
            if not release.is_set() and not started:
                await release.wait()

        esetup.async_teardown_server.side_effect = _slow_first_teardown

        on = asyncio.ensure_future(pkg.async_set_access(hass, entry, True))
        unload = asyncio.ensure_future(pkg.async_unload_server_entry(hass, entry))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(on, unload)

        assert started
        assert all(task.cancelled() for task in started)


class TestAccessSwitch:
    """The access switch opens and closes the server while the entry stays loaded (#2801)."""

    async def test_access_left_off_keeps_the_server_down_after_a_restart(self) -> None:
        hass = _make_hass()
        entry = _make_entry(data={DATA_ACCESS_ENABLED: False})

        await pkg.async_setup_server_entry(hass, entry)

        esetup.async_bring_up_server.assert_not_called()
        assert DATA_BRINGUP_TASK not in hass.data[DOMAIN]

    async def test_off_after_restart_option_closes_access_at_startup(self) -> None:
        hass = _make_hass()
        entry = _make_entry(options={OPT_ACCESS_OFF_AFTER_RESTART: True})

        await pkg.async_setup_server_entry(hass, entry)

        assert entry.data[DATA_ACCESS_ENABLED] is False
        esetup.async_bring_up_server.assert_not_called()

    async def test_off_after_restart_option_does_not_close_access_on_a_reload(
        self,
    ) -> None:
        # An options save reloads the entry; that is not a restart, so access the
        # user turned on stays on.
        hass = _make_hass()
        entry = _make_entry(options={OPT_ACCESS_OFF_AFTER_RESTART: True})
        entry.async_create_background_task.side_effect = _pending_task
        await pkg.async_setup_server_entry(hass, entry)
        await pkg.async_set_access(hass, entry, True)
        await pkg.async_unload_server_entry(hass, entry)
        esetup.async_bring_up_server.reset_mock()

        await pkg.async_setup_server_entry(hass, entry)

        assert pkg.access_enabled(entry)
        esetup.async_bring_up_server.assert_called_once()

    async def test_switch_starts_out_off_after_a_restart_with_the_option(self) -> None:
        # The switch reports the stored state when it is added; storing after
        # the platform forward would show it on while the server is down.
        hass = _make_hass()
        entry = _make_entry(options={OPT_ACCESS_OFF_AFTER_RESTART: True})
        seen: list[bool] = []
        hass.config_entries.async_forward_entry_setups.side_effect = lambda *_a: (
            seen.append(pkg.access_enabled(entry))
        )

        await pkg.async_setup_server_entry(hass, entry)

        assert seen == [False]

    async def test_switching_on_after_the_entry_unloaded_starts_nothing(self) -> None:
        # A switch-on queued behind the unload (disable, or a slow stop) must
        # not start a server for an entry that is no longer loaded.
        hass = _make_hass()
        entry = _make_entry(data={DATA_ACCESS_ENABLED: False})
        await pkg.async_setup_server_entry(hass, entry)
        await pkg.async_unload_server_entry(hass, entry)

        await pkg.async_set_access(hass, entry, True)

        esetup.async_bring_up_server.assert_not_called()

    async def test_switching_on_works_again_after_a_reload(self) -> None:
        hass = _make_hass()
        entry = _make_entry(data={DATA_ACCESS_ENABLED: False})
        await pkg.async_setup_server_entry(hass, entry)
        await pkg.async_unload_server_entry(hass, entry)
        await pkg.async_setup_server_entry(hass, entry)

        await pkg.async_set_access(hass, entry, True)

        esetup.async_bring_up_server.assert_called_once()

    async def test_setup_starts_no_second_server_when_one_is_starting(self) -> None:
        # A defensive guard: the unload cancels the bring-up and a switch-on
        # queued behind it starts nothing, but a bring-up still in flight at
        # setup must not get a second server next to it.
        hass = _make_hass()
        hass.data[DOMAIN] = {DATA_BRINGUP_TASK: _pending_task()}
        entry = _make_entry()

        await pkg.async_setup_server_entry(hass, entry)

        esetup.async_bring_up_server.assert_not_called()

    async def test_setup_waits_for_a_stop_in_progress(self) -> None:
        # A reload right after a switch-off: the new setup must not start the
        # server while the old one still holds the port.
        hass = _make_hass()
        entry = _make_entry()
        release = asyncio.Event()
        order: list[str] = []

        async def _slow_teardown(_hass: object) -> None:
            if not order:
                order.append("slow_stop")
                await release.wait()
                order.append("slow_stop_done")

        esetup.async_teardown_server.side_effect = _slow_teardown
        esetup.async_bring_up_server.side_effect = lambda *a, **k: order.append(
            "bring_up"
        )

        off = asyncio.ensure_future(pkg.async_set_access(hass, entry, False))
        await asyncio.sleep(0)
        entry.data = {**entry.data, DATA_ACCESS_ENABLED: True}
        setup = asyncio.ensure_future(pkg.async_setup_server_entry(hass, entry))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(off, setup)

        assert order == ["slow_stop", "slow_stop_done", "bring_up"]

    async def test_switching_access_keeps_the_connect_url(self) -> None:
        hass = _make_hass()
        entry = _make_entry(data={DATA_WEBHOOK_ID: "mcp_keep", DATA_SECRET_PATH: "/p"})

        await pkg.async_set_access(hass, entry, False)

        assert entry.data[DATA_WEBHOOK_ID] == "mcp_keep"
        assert entry.data[DATA_SECRET_PATH] == "/p"

    async def test_switching_off_cancels_a_running_bring_up_before_teardown(
        self,
    ) -> None:
        # A bring-up still in flight would otherwise register the webhook and the
        # LLM API after the teardown, reopening access.
        hass = _make_hass()
        entry = _make_entry()
        pending = asyncio.get_running_loop().create_future()
        hass.data[DOMAIN] = {DATA_BRINGUP_TASK: pending}
        cancelled_at_teardown: list[bool] = []
        esetup.async_teardown_server.side_effect = lambda _hass: (
            cancelled_at_teardown.append(pending.cancelled())
        )

        await pkg.async_set_access(hass, entry, False)

        assert cancelled_at_teardown == [True]
        assert entry.data[DATA_ACCESS_ENABLED] is False

    async def test_switching_on_starts_the_server_without_a_notification(self) -> None:
        hass = _make_hass()
        entry = _make_entry(data={DATA_ACCESS_ENABLED: False})

        await pkg.async_set_access(hass, entry, True)

        esetup.async_bring_up_server.assert_called_once_with(
            hass, entry, announce=False
        )
        assert hass.data[DOMAIN][DATA_BRINGUP_TASK] == "BRINGUP_TASK"
        assert pkg.access_enabled(entry)

    async def test_switching_on_while_a_start_runs_starts_no_second_server(
        self,
    ) -> None:
        hass = _make_hass()
        entry = _make_entry()
        hass.data[DOMAIN] = {
            DATA_BRINGUP_TASK: asyncio.get_running_loop().create_future()
        }

        await pkg.async_set_access(hass, entry, True)

        esetup.async_bring_up_server.assert_not_called()
        esetup.async_teardown_server.assert_not_awaited()

    @pytest.mark.parametrize(
        "manager_running", [None, False], ids=["failed", "crashed"]
    )
    async def test_switching_on_again_retries_a_failed_start(
        self, manager_running: bool | None
    ) -> None:
        # A failed bring-up, or a server that crashed later, leaves access on
        # with nothing running; turning the switch on again retries the start.
        hass = _make_hass()
        entry = _make_entry()
        hass.data[DOMAIN] = {DATA_BRINGUP_TASK: _finished_future()}
        if manager_running is not None:
            hass.data[DOMAIN][DATA_MANAGER] = _manager(running=manager_running)

        await pkg.async_set_access(hass, entry, True)

        esetup.async_teardown_server.assert_awaited_once_with(hass)
        esetup.async_bring_up_server.assert_called_once()

    async def test_switching_on_leaves_a_running_server_alone(self) -> None:
        # An automation turning the switch on while it is on must not drop the
        # connected clients.
        hass = _make_hass()
        entry = _make_entry()
        hass.data[DOMAIN] = {
            DATA_BRINGUP_TASK: _finished_future(),
            DATA_MANAGER: _manager(running=True),
        }

        await pkg.async_set_access(hass, entry, True)

        esetup.async_teardown_server.assert_not_awaited()
        esetup.async_bring_up_server.assert_not_called()

    async def test_switching_on_keeps_access_off_when_the_cleanup_fails(self) -> None:
        hass = _make_hass()
        entry = _make_entry(data={DATA_ACCESS_ENABLED: False})
        esetup.async_teardown_server.side_effect = RuntimeError("stop failed")

        with pytest.raises(RuntimeError):
            await pkg.async_set_access(hass, entry, True)

        assert not pkg.access_enabled(entry)

    async def test_switching_off_again_retries_the_stop(self) -> None:
        # A stop that raised leaves access stored off; the next off must not be
        # skipped as already done.
        hass = _make_hass()
        entry = _make_entry(data={DATA_ACCESS_ENABLED: False})

        await pkg.async_set_access(hass, entry, False)

        esetup.async_teardown_server.assert_awaited_once_with(hass)

    async def test_switching_on_waits_for_a_stop_in_progress(self) -> None:
        # A start overlapping a slow stop would claim the server port while the
        # old server still holds it.
        hass = _make_hass()
        entry = _make_entry()
        release = asyncio.Event()
        order: list[str] = []

        async def _slow_teardown(_hass: object) -> None:
            # Only the first stop is slow: a start that did not wait for it
            # would finish first.
            if not order:
                order.append("slow_stop")
                await release.wait()
                order.append("slow_stop_done")

        esetup.async_teardown_server.side_effect = _slow_teardown
        esetup.async_bring_up_server.side_effect = lambda *a, **k: order.append(
            "bring_up"
        )

        off = asyncio.ensure_future(pkg.async_set_access(hass, entry, False))
        on = asyncio.ensure_future(pkg.async_set_access(hass, entry, True))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(off, on)

        assert order == ["slow_stop", "slow_stop_done", "bring_up"]

    async def test_switching_off_stops_the_server_when_the_cancelled_start_fails(
        self,
    ) -> None:
        # A start cancelled mid-way tears down its partial state itself; when
        # that raises, the stop must still run.
        async def _start_failing_on_cancel() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise RuntimeError("partial teardown failed") from None

        hass = _make_hass()
        entry = _make_entry()
        task = asyncio.ensure_future(_start_failing_on_cancel())
        await asyncio.sleep(0)
        hass.data[DOMAIN] = {DATA_BRINGUP_TASK: task}

        with pytest.raises(RuntimeError):
            await pkg.async_set_access(hass, entry, False)

        esetup.async_teardown_server.assert_awaited_once_with(hass)

    async def test_switching_on_during_setup_starts_one_server(self) -> None:
        hass = _make_hass()
        entry = _make_entry()
        entry.async_create_background_task.side_effect = _pending_task

        async def _forward(*_args: object) -> None:
            # The switch exists from here on; a turn_on can arrive at once.
            await pkg.async_set_access(hass, entry, True)

        hass.config_entries.async_forward_entry_setups.side_effect = _forward

        await pkg.async_setup_server_entry(hass, entry)

        esetup.async_bring_up_server.assert_called_once()

    async def test_changing_the_restart_option_does_not_reload_the_server(self) -> None:
        hass = _make_hass()
        entry = _make_entry(
            options={"server_port": 9584, OPT_ACCESS_OFF_AFTER_RESTART: True}
        )
        hass.data[DOMAIN] = {DATA_LAST_OPTIONS: {"server_port": 9584}}

        await pkg._async_options_updated(hass, entry)

        hass.config_entries.async_reload.assert_not_awaited()


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

    async def test_callback_allowlist_edit_applies_without_a_reload(self):
        # The settings panel saves the list through the running server; a
        # reload would tear that server down mid-request (#2427).
        hass = _make_hass()
        entry = _make_entry(
            options={"server_port": 9584, OPT_OAUTH_REDIRECT_ALLOWLIST: []}
        )
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
    rename migration and the tools-entry device registration must stay present
    (the device itself is tested in ``test_entry_device.py``).
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
        assert "async_register_entry_device(" in src
        assert "File & YAML editing services" in src
