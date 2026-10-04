"""Unit tests for the server entry-point wiring.

Focuses on what ``async_setup_server_entry`` / ``async_unload_server_entry``
wire up: the background bring-up, the sidebar panel, the removal of the
retired server update entity (the server now arrives with the component
release, #2427), and the ``ha_mcp_tools/*`` WebSocket command surface the
server entry registers up front (issue #2289).

Home Assistant / aiohttp are stubbed via ``_embedded_stubs`` (imported first so
the fakes are installed before the component module binds them). The lazily
imported ``embedded_setup`` / ``ui_panel`` collaborators are replaced with
fakes so the entry-point wiring is exercised in isolation.
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
    DATA_WEBHOOK_ID,
    DOMAIN,
)


def _make_hass() -> MagicMock:
    hass = MagicMock(name="hass")
    hass.data = {}
    hass.config_entries.async_forward_entry_setups = AsyncMock()

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
    draining task schedules itself (both the entry's and hass's pools)."""
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


class _FakeEntityRegistry:
    """Just enough of the entity registry for the retired-entity cleanup."""

    def __init__(self, existing: dict[tuple[str, str, str], str]) -> None:
        self.existing = existing
        self.removed: list[str] = []

    def async_get_entity_id(self, domain, platform, unique_id):
        return self.existing.get((domain, platform, unique_id))

    def async_remove(self, entity_id) -> None:
        self.removed.append(entity_id)


@pytest.fixture
def fake_collaborators(monkeypatch):
    """Inject fake ``embedded_setup`` / ``ui_panel`` / entity-registry modules.

    ``async_setup_server_entry`` imports ``async_bring_up_server`` from
    ``embedded_setup``, ``async_register_ui_panel`` from ``ui_panel``,
    ``async_register_commands`` from ``websocket_api`` and the entity
    registry at call time; the fakes keep the full HA chain out of this test.
    The registry holds the update entity a component 2.x install registered.
    """
    fake_setup = ModuleType("custom_components.ha_mcp_tools.embedded_setup")
    fake_setup.async_bring_up_server = MagicMock(
        name="async_bring_up_server", return_value=MagicMock(name="bringup_task_arg")
    )
    fake_setup.async_teardown_server = AsyncMock(name="async_teardown_server")

    fake_panel = ModuleType("custom_components.ha_mcp_tools.ui_panel")
    fake_panel.async_register_ui_panel = AsyncMock(name="async_register_ui_panel")
    fake_panel.async_unregister_ui_panel = MagicMock(name="async_unregister_ui_panel")

    registry = _FakeEntityRegistry(
        {("update", DOMAIN, "entry-1_server_update"): "update.ha_mcp_server_update"}
    )
    fake_er = ModuleType("homeassistant.helpers.entity_registry")
    fake_er.async_get = MagicMock(return_value=registry)

    fake_wsapi = ModuleType("custom_components.ha_mcp_tools.websocket_api")
    fake_wsapi.async_register_commands = MagicMock(name="async_register_commands")

    monkeypatch.setitem(
        sys.modules, "custom_components.ha_mcp_tools.embedded_setup", fake_setup
    )
    monkeypatch.setitem(
        sys.modules, "custom_components.ha_mcp_tools.ui_panel", fake_panel
    )
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.entity_registry", fake_er)
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers"], "entity_registry", fake_er, raising=False
    )
    monkeypatch.setitem(
        sys.modules, "custom_components.ha_mcp_tools.websocket_api", fake_wsapi
    )
    return SimpleNamespace(
        setup=fake_setup,
        panel=fake_panel,
        registry=registry,
        websocket_api=fake_wsapi,
    )


class TestSetup:
    async def test_schedules_bring_up_without_update_platform(self, fake_collaborators):
        # The server arrives with the component release (#2427): no update
        # platform, no PyPI poll, no automatic reinstall.
        hass = _make_hass()
        entry = _make_entry()

        result = await eentry.async_setup_server_entry(hass, entry)
        await _drain_background_tasks(hass, entry)

        assert result is True
        fake_collaborators.setup.async_bring_up_server.assert_called_once_with(
            hass, entry
        )
        hass.config_entries.async_forward_entry_setups.assert_not_called()
        assert hass.async_create_background_task.call_count == 0

    async def test_removes_the_retired_server_update_entity(self, fake_collaborators):
        hass = _make_hass()
        entry = _make_entry()

        await eentry.async_setup_server_entry(hass, entry)

        assert fake_collaborators.registry.removed == ["update.ha_mcp_server_update"]

    async def test_no_retired_entity_means_nothing_removed(self, fake_collaborators):
        fake_collaborators.registry.existing.clear()
        hass = _make_hass()
        entry = _make_entry()

        await eentry.async_setup_server_entry(hass, entry)

        assert fake_collaborators.registry.removed == []

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


class TestWebSocketCommandRegistration:
    """The server entry registers the ``ha_mcp_tools/*`` WS commands (#2289).

    They used to be registered only from the tools entry, so an install with
    just the server (embedded) entry had no ``ha_mcp_tools/*`` commands at all
    and every ``ha_search`` silently fell back to the legacy path.
    """

    async def test_setup_registers_ws_commands(self, fake_collaborators):
        hass = _make_hass()
        entry = _make_entry()

        await eentry.async_setup_server_entry(hass, entry)
        await _drain_background_tasks(hass, entry)

        register = fake_collaborators.websocket_api.async_register_commands
        register.assert_called_once_with(hass)

    async def test_ws_commands_registered_before_fallible_setup_steps(
        self, fake_collaborators
    ):
        # Placement pin: registration runs first, so a later setup step failing
        # (the sidebar panel here, the background bring-up in the field) never
        # costs a server-only install its command surface.
        hass = _make_hass()
        entry = _make_entry()
        fake_collaborators.panel.async_register_ui_panel.side_effect = RuntimeError(
            "panel registration failed"
        )

        with pytest.raises(RuntimeError):
            await eentry.async_setup_server_entry(hass, entry)

        register = fake_collaborators.websocket_api.async_register_commands
        register.assert_called_once_with(hass)


class TestPrebindOAuthViews:
    """Prebind every applicable OAuth route before slow background bring-up."""

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

        eentry._prebind_oauth_views(hass, self._legacy_entry())

        # 6 discovery + 3 scoped authorize/token/revoke + 2 root legacy aliases.
        assert hass.http.register_view.call_count == 11
        assert hass.data.get(oauth_legacy.OAUTH_ROUTE_OWNER_KEY) == oauth_legacy._DOMAIN

    @pytest.mark.parametrize(
        "auth_mode",
        [const.WEBHOOK_AUTH_NONE, const.WEBHOOK_AUTH_HA],
    )
    def test_none_and_ha_auth_modes_prebind_full_scoped_surface(self, auth_mode):
        hass = _make_hass()
        hass.http = MagicMock()
        entry = _make_entry()
        entry.options = {
            const.OPT_WEBHOOK_AUTH: auth_mode,
            const.OPT_ENABLE_WEBHOOK: True,
        }

        eentry._prebind_oauth_views(hass, entry)

        # 6 discovery + 3 scoped authorize/token/revoke + 1 DCR registration
        # route.
        assert hass.http.register_view.call_count == 10

    def test_webhook_disabled_binds_nothing(self):
        hass = _make_hass()
        hass.http = MagicMock()
        entry = self._legacy_entry()
        entry.options = {**entry.options, const.OPT_ENABLE_WEBHOOK: False}

        eentry._prebind_oauth_views(hass, entry)

        hass.http.register_view.assert_not_called()

    def test_partial_legacy_credentials_still_prebind_scoped_routes(self):
        # _ensure_secrets mints all three whenever legacy is configured; a gap
        # means a partial config, deferred to the bring-up path to surface.
        hass = _make_hass()
        hass.http = MagicMock()
        entry = self._legacy_entry()
        entry.data = {
            k: v for k, v in entry.data.items() if k != const.DATA_OAUTH_CLIENT_SECRET
        }

        eentry._prebind_oauth_views(hass, entry)

        # Root aliases wait for credentials, but advertised scoped routes must
        # still beat Home Assistant's HTTP freeze: 6 discovery + 3 scoped
        # authorize/token/revoke.
        assert hass.http.register_view.call_count == 9

    def test_route_conflict_is_swallowed_at_setup(self):
        # The webhook-proxy add-on owning the root routes makes
        # bind_legacy_views raise; setup must survive (the bring-up
        # re-encounters the conflict and files the user-facing repair).
        from custom_components.ha_mcp_tools import oauth_legacy

        hass = _make_hass()
        hass.is_running = False
        hass.http = MagicMock()
        hass.data[oauth_legacy.OAUTH_ROUTE_OWNER_KEY] = "webhook_proxy_addon"

        eentry._prebind_oauth_views(hass, self._legacy_entry())

        assert hass.data[oauth_legacy.OAUTH_ROUTE_OWNER_KEY] == "webhook_proxy_addon"
        # The foreign owner blocks only the root aliases; scoped routes remain
        # (6 discovery + 3 scoped authorize/token/revoke).
        assert hass.http.register_view.call_count == 9


class TestEnsureSecretsLegacyWiring:
    """`_ensure_secrets` is the persistence path for the legacy credential
    lifecycle: it must fold `_ensure_legacy_oauth_secrets` mutations into the
    same `async_update_entry` call — including OPTIONS (the one-shot
    regenerate flag lives there), which pre-legacy code never persisted."""

    def _capturing_hass(self) -> tuple[MagicMock, dict]:
        hass = _make_hass()
        captured: dict = {}

        def _update(entry, *, data=None, options=None):
            captured["data"] = data
            captured["options"] = options

        hass.config_entries.async_update_entry = MagicMock(side_effect=_update)
        return hass, captured

    def test_legacy_regenerate_persists_new_creds_and_clears_flag(self):
        hass, captured = self._capturing_hass()
        entry = _make_entry()
        entry.options = {
            const.OPT_WEBHOOK_AUTH: const.WEBHOOK_AUTH_LEGACY,
            const.OPT_OAUTH_REGENERATE: True,
        }

        eentry._ensure_secrets(hass, entry)

        assert captured["data"][const.DATA_OAUTH_CLIENT_ID].startswith("hamcp-")
        assert captured["data"][const.DATA_OAUTH_CLIENT_SECRET]
        assert captured["data"][const.DATA_OAUTH_SIGNING_KEY]
        assert captured["options"][const.OPT_OAUTH_REGENERATE] is False

    def test_non_legacy_mode_mints_no_oauth_credentials(self):
        hass, captured = self._capturing_hass()
        entry = _make_entry()
        entry.options = {const.OPT_WEBHOOK_AUTH: const.WEBHOOK_AUTH_NONE}

        eentry._ensure_secrets(hass, entry)

        # Webhook id + secret path already present -> nothing changed, no
        # update call at all (and in particular no OAuth minting).
        assert captured == {}


class TestUnload:
    async def test_unload_tears_down_the_server(self, fake_collaborators):
        hass = _make_hass()
        entry = _make_entry()
        await eentry.async_setup_server_entry(hass, entry)
        await _drain_background_tasks(hass, entry)

        result = await eentry.async_unload_server_entry(hass, entry)

        assert result is True
        fake_collaborators.setup.async_teardown_server.assert_awaited_once_with(hass)
        fake_collaborators.panel.async_unregister_ui_panel.assert_called_once_with(hass)
