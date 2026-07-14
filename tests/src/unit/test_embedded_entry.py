"""Unit tests for the server entry-point wiring (issue #1760: update entity).

Focuses on what ``async_setup_server_entry`` / ``async_unload_server_entry``
wire up around the :class:`~.coordinator.ServerVersionCoordinator`: creating
it with the right interval, registering its auto-update listener (as a
background task, never a synchronous reload from inside the listener),
kicking off an initial (non-blocking) refresh, and forwarding/unloading the
``update`` platform.

Home Assistant / aiohttp are stubbed via ``_embedded_stubs`` (imported first so
the fakes are installed before the component module binds them). The lazily
imported ``embedded_setup`` / ``ui_panel`` / ``coordinator`` collaborators are
replaced with fakes so the entry-point wiring is exercised in isolation.
"""

from __future__ import annotations

import asyncio
import secrets
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ._embedded_stubs import install

install()

import custom_components.ha_mcp_tools.embedded_entry as eentry  # noqa: E402
from custom_components.ha_mcp_tools import const  # noqa: E402
from custom_components.ha_mcp_tools.const import (  # noqa: E402
    DATA_SECRET_PATH,
    DATA_UPDATE_COORDINATOR,
    DATA_WEBHOOK_ID,
    DOMAIN,
    UPDATE_CHECK_INTERVAL,
)


def _make_hass() -> MagicMock:
    hass = MagicMock(name="hass")
    hass.data = {}
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)

    background_tasks: list[asyncio.Task] = []

    def _create_bg_task(coro, name, eager_start=True):
        # Mirrors HomeAssistant.async_create_background_task: schedule the
        # coroutine so it actually runs (a bare MagicMock would leak it as a
        # "was never awaited" warning and the drain helper would never see it).
        task = asyncio.ensure_future(coro)
        background_tasks.append(task)
        return task

    hass.async_create_background_task = MagicMock(side_effect=_create_bg_task)
    hass.background_tasks = background_tasks
    return hass


def _make_entry() -> MagicMock:
    entry = MagicMock(name="entry")
    entry.options = {}
    entry.data = {DATA_SECRET_PATH: "/private_x", DATA_WEBHOOK_ID: "mcp_x"}
    entry.entry_id = "entry-1"
    entry.async_on_unload = MagicMock()
    entry.add_update_listener = MagicMock(return_value=MagicMock(name="listener"))

    background_tasks: list[asyncio.Task] = []

    def _create_bg_task(hass, coro, name):
        # Real ConfigEntry.async_create_background_task schedules the
        # coroutine immediately; a bare MagicMock would instead leak it (an
        # "was never awaited" warning) since nothing would ever run it. The
        # fake bring-up coroutine below is a plain MagicMock return value (not
        # a real coroutine), so it is left alone — same as before.
        if asyncio.iscoroutine(coro):
            task = asyncio.ensure_future(coro)
            background_tasks.append(task)
            return task
        return MagicMock(name=f"bg_task:{name}")

    entry.async_create_background_task = MagicMock(side_effect=_create_bg_task)
    entry.background_tasks = background_tasks
    return entry


async def _drain_background_tasks(hass, entry) -> None:
    """Run every background task scheduled so far, including ones a still-
    draining task schedules itself (the auto-update listener schedules its own
    hass-owned background task from inside the entry-owned initial-refresh
    task — hence draining BOTH pools)."""
    seen: set[asyncio.Task] = set()
    while True:
        pending = [
            t
            for t in (*entry.background_tasks, *hass.background_tasks)
            if t not in seen
        ]
        if not pending:
            break
        seen.update(pending)
        await asyncio.gather(*pending)


class _FakeCoordinator:
    """Real (non-Mock) fake so ``isinstance``/listener wiring behave for real."""

    def __init__(self, hass, entry) -> None:
        self.hass = hass
        self.entry = entry
        self.data = None
        self.update_interval = UPDATE_CHECK_INTERVAL
        self._listeners: list = []

    def async_add_listener(self, update_callback):
        self._listeners.append(update_callback)

        def _unsub() -> None:
            self._listeners.remove(update_callback)

        return _unsub

    async def async_refresh(self) -> None:
        self.data = SimpleNamespace(installed="1.0.0", latest="1.1.0", dist="ha-mcp")
        for listener in list(self._listeners):
            listener()


@pytest.fixture
def fake_collaborators(monkeypatch):
    """Inject fake ``embedded_setup`` / ``ui_panel`` / ``coordinator`` modules.

    ``async_setup_server_entry`` imports ``async_bring_up_server`` /
    ``async_maybe_auto_update`` from ``embedded_setup``,
    ``async_register_ui_panel`` from ``ui_panel``, and ``ServerVersionCoordinator``
    from ``coordinator`` at call time; the fakes keep the full HA chain out of
    this test.
    """
    fake_setup = ModuleType("custom_components.ha_mcp_tools.embedded_setup")
    fake_setup.async_bring_up_server = MagicMock(
        name="async_bring_up_server", return_value=MagicMock(name="bringup_task_arg")
    )
    fake_setup.async_maybe_auto_update = AsyncMock(name="async_maybe_auto_update")
    fake_setup.async_teardown_server = AsyncMock(name="async_teardown_server")

    fake_panel = ModuleType("custom_components.ha_mcp_tools.ui_panel")
    fake_panel.async_register_ui_panel = AsyncMock(name="async_register_ui_panel")
    fake_panel.async_unregister_ui_panel = MagicMock(name="async_unregister_ui_panel")

    fake_coord_mod = ModuleType("custom_components.ha_mcp_tools.coordinator")
    fake_coord_mod.ServerVersionCoordinator = _FakeCoordinator

    monkeypatch.setitem(
        sys.modules, "custom_components.ha_mcp_tools.embedded_setup", fake_setup
    )
    monkeypatch.setitem(
        sys.modules, "custom_components.ha_mcp_tools.ui_panel", fake_panel
    )
    monkeypatch.setitem(
        sys.modules, "custom_components.ha_mcp_tools.coordinator", fake_coord_mod
    )
    return SimpleNamespace(
        setup=fake_setup, panel=fake_panel, coordinator_cls=_FakeCoordinator
    )


class TestSetup:
    async def test_creates_coordinator_with_update_interval(self, fake_collaborators):
        hass = _make_hass()
        entry = _make_entry()

        result = await eentry.async_setup_server_entry(hass, entry)
        await _drain_background_tasks(hass, entry)

        assert result is True
        coordinator = hass.data[DOMAIN][DATA_UPDATE_COORDINATOR]
        assert isinstance(coordinator, fake_collaborators.coordinator_cls)
        assert coordinator.update_interval == UPDATE_CHECK_INTERVAL

    async def test_registers_listener_cleaned_up_on_unload(self, fake_collaborators):
        hass = _make_hass()
        entry = _make_entry()

        await eentry.async_setup_server_entry(hass, entry)
        await _drain_background_tasks(hass, entry)

        coordinator = hass.data[DOMAIN][DATA_UPDATE_COORDINATOR]
        assert coordinator._listeners  # the auto-update listener was registered
        # ...and its unsub handed to async_on_unload for cleanup.
        unload_args = [c.args[0] for c in entry.async_on_unload.call_args_list]
        assert any(callable(arg) for arg in unload_args)

    async def test_initial_refresh_runs_as_background_task(self, fake_collaborators):
        hass = _make_hass()
        entry = _make_entry()

        await eentry.async_setup_server_entry(hass, entry)
        await _drain_background_tasks(hass, entry)

        coordinator = hass.data[DOMAIN][DATA_UPDATE_COORDINATOR]
        # The fake coordinator's async_refresh sets .data - proves it actually
        # ran (not just constructed) via the background-task path, not an
        # awaited call inline in async_setup_server_entry.
        assert coordinator.data is not None
        assert coordinator.data.installed == "1.0.0"

    async def test_listener_schedules_auto_update_as_background_task(
        self, fake_collaborators
    ):
        hass = _make_hass()
        entry = _make_entry()

        await eentry.async_setup_server_entry(hass, entry)
        await _drain_background_tasks(hass, entry)

        fake_collaborators.setup.async_maybe_auto_update.assert_awaited_once()
        args = fake_collaborators.setup.async_maybe_auto_update.await_args.args
        assert args[0] is hass
        assert args[1] is entry
        assert args[2].installed == "1.0.0"
        assert args[2].latest == "1.1.0"

    async def test_auto_update_task_is_hass_owned_not_entry_owned(
        self, fake_collaborators
    ):
        # Regression: entry background tasks are cancelled by the very unload
        # that async_maybe_auto_update's reload performs, so an entry-owned
        # task would cancel itself mid-reload and leave the entry unloaded
        # (server down until restart). The auto-update task must be hass-owned.
        hass = _make_hass()
        entry = _make_entry()

        await eentry.async_setup_server_entry(hass, entry)
        await _drain_background_tasks(hass, entry)

        hass_task_names = [
            c.args[1] for c in hass.async_create_background_task.call_args_list
        ]
        assert f"{DOMAIN}_server_auto_update" in hass_task_names
        entry_task_names = [
            c.args[2] for c in entry.async_create_background_task.call_args_list
        ]
        assert f"{DOMAIN}_server_auto_update" not in entry_task_names

    async def test_forwards_update_platform(self, fake_collaborators):
        hass = _make_hass()
        entry = _make_entry()

        await eentry.async_setup_server_entry(hass, entry)
        await _drain_background_tasks(hass, entry)

        hass.config_entries.async_forward_entry_setups.assert_awaited_once_with(
            entry, [eentry.Platform.UPDATE]
        )

    async def test_default_registers_ui_panel(self, fake_collaborators):
        # enable_sidebar_panel absent (default on): the admin-only "Open Web UI"
        # sidebar panel is registered during entry setup.
        hass = _make_hass()
        entry = _make_entry()

        await eentry.async_setup_server_entry(hass, entry)
        await _drain_background_tasks(hass, entry)

        fake_collaborators.panel.async_register_ui_panel.assert_awaited_once()

    async def test_sidebar_panel_off_skips_ui_panel_registration(
        self, fake_collaborators
    ):
        # enable_sidebar_panel=False: entry setup must not register the sidebar
        # panel (the user opted out of the sidebar entry point).
        hass = _make_hass()
        entry = _make_entry()
        entry.options = {const.OPT_ENABLE_SIDEBAR_PANEL: False}

        await eentry.async_setup_server_entry(hass, entry)
        await _drain_background_tasks(hass, entry)

        fake_collaborators.panel.async_register_ui_panel.assert_not_awaited()


class TestPrebindLegacyOAuthViews:
    """`_prebind_legacy_oauth_views` binds the root /authorize + /token views
    synchronously at setup — before the background bring-up's slow install — so
    they are live at boot instead of racing HA reaching RUNNING."""

    def _legacy_entry(self) -> MagicMock:
        entry = _make_entry()
        entry.options = {
            const.OPT_WEBHOOK_AUTH: const.WEBHOOK_AUTH_LEGACY,
            const.OPT_ENABLE_WEBHOOK: True,
        }
        entry.data = {
            **entry.data,
            const.DATA_OAUTH_CLIENT_ID: "cid",
            const.DATA_OAUTH_CLIENT_SECRET: "secret",
            const.DATA_OAUTH_SIGNING_KEY: secrets.token_hex(32),
        }
        return entry

    def test_legacy_mode_binds_root_views_at_setup(self):
        from custom_components.ha_mcp_tools import oauth_legacy

        hass = _make_hass()
        hass.is_running = False
        hass.http = MagicMock()

        eentry._prebind_legacy_oauth_views(hass, self._legacy_entry())

        assert hass.http.register_view.call_count == 2  # /authorize + /token
        assert hass.data.get(oauth_legacy.OAUTH_ROUTE_OWNER_KEY) == oauth_legacy._DOMAIN

    def test_non_legacy_mode_binds_nothing(self):
        hass = _make_hass()
        hass.http = MagicMock()
        entry = _make_entry()
        entry.options = {const.OPT_WEBHOOK_AUTH: const.WEBHOOK_AUTH_NONE}

        eentry._prebind_legacy_oauth_views(hass, entry)

        hass.http.register_view.assert_not_called()

    def test_webhook_disabled_binds_nothing(self):
        hass = _make_hass()
        hass.http = MagicMock()
        entry = self._legacy_entry()
        entry.options = {**entry.options, const.OPT_ENABLE_WEBHOOK: False}

        eentry._prebind_legacy_oauth_views(hass, entry)

        hass.http.register_view.assert_not_called()


class TestUnload:
    async def test_unloads_update_platform_and_pops_coordinator(
        self, fake_collaborators
    ):
        hass = _make_hass()
        entry = _make_entry()
        await eentry.async_setup_server_entry(hass, entry)
        await _drain_background_tasks(hass, entry)

        result = await eentry.async_unload_server_entry(hass, entry)

        assert result is True
        hass.config_entries.async_unload_platforms.assert_awaited_once_with(
            entry, [eentry.Platform.UPDATE]
        )
        assert DATA_UPDATE_COORDINATOR not in hass.data.get(DOMAIN, {})

    async def test_platform_unload_happens_before_teardown(self, fake_collaborators):
        # Ordering matters (see the design note in async_unload_server_entry):
        # the platform (and the entity it drives) must be gone before the
        # server/webhook teardown and the bring-up-task cancellation run.
        hass = _make_hass()
        entry = _make_entry()
        await eentry.async_setup_server_entry(hass, entry)
        await _drain_background_tasks(hass, entry)

        calls: list[str] = []
        hass.config_entries.async_unload_platforms.side_effect = lambda *a, **k: (
            calls.append("unload_platforms") or True
        )

        async def _teardown(*_a, **_k):
            calls.append("teardown_server")

        fake_collaborators.setup.async_teardown_server.side_effect = _teardown

        await eentry.async_unload_server_entry(hass, entry)

        assert calls == ["unload_platforms", "teardown_server"]
