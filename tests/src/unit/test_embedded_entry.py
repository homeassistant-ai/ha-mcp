"""The server entry-point wiring, on a real ``hass``.

Home Assistant sets the server entry up and unloads it through its own
config-entry machinery, with its real registries. What that wires up: the
background bring-up, the sidebar panel, the removal of the retired server
update entity (the server now arrives with the component release, #2427), the
entry's device, and the ``ha_mcp_tools/*`` WebSocket command surface the
server entry registers up front (issue #2289).

The server itself and the sidebar panel are replaced (``_server_entry``).
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

import custom_components.ha_mcp_tools.embedded_entry as eentry
from custom_components.ha_mcp_tools import const, oauth_legacy
from custom_components.ha_mcp_tools.const import DOMAIN

from ._server_entry import add_server_entry, replace_server, set_up


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    return replace_server(monkeypatch)


def _ws_commands(hass: HomeAssistant) -> set[str]:
    """The ``ha_mcp_tools/*`` commands registered with Home Assistant."""
    return {
        command
        for command in hass.data.get("websocket_api", {})
        if command.startswith(f"{DOMAIN}/")
    }


class TestSetup:
    async def test_schedules_bring_up_without_update_platform(
        self,
        hass: HomeAssistant,
        entity_registry: er.EntityRegistry,
        server: SimpleNamespace,
    ) -> None:
        # The server arrives with the component release (#2427): no update
        # platform, no PyPI poll, no automatic reinstall.
        entry = add_server_entry(hass)

        assert await set_up(hass, entry)

        server.bring_up.assert_called_once_with(hass, entry, announce=True)
        entities = er.async_entries_for_config_entry(entity_registry, entry.entry_id)
        assert {entity.domain for entity in entities} == {"switch"}

    async def test_removes_the_retired_server_update_entity(
        self,
        hass: HomeAssistant,
        entity_registry: er.EntityRegistry,
        server: SimpleNamespace,
    ) -> None:
        entry = add_server_entry(hass)
        retired = entity_registry.async_get_or_create(
            "update", DOMAIN, f"{entry.entry_id}_server_update", config_entry=entry
        )

        assert await set_up(hass, entry)

        assert entity_registry.async_get(retired.entity_id) is None

    async def test_server_entry_has_a_current_device(
        self,
        hass: HomeAssistant,
        device_registry: dr.DeviceRegistry,
        server: SimpleNamespace,
    ) -> None:
        # The retired update entity used to create the server's device; an
        # upgraded install must not keep it at its old version, and a new one
        # must not lack it.
        entry = add_server_entry(hass)
        identifiers = {(DOMAIN, entry.entry_id)}
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers=identifiers, sw_version="2.0.0"
        )

        assert await set_up(hass, entry)

        device = device_registry.async_get_device_by_identifier(
            (DOMAIN, entry.entry_id), entry.entry_id
        )
        assert device is not None
        manifest = Path(eentry.__file__).with_name("manifest.json")
        assert device.sw_version == json.loads(manifest.read_text())["version"]

    async def test_default_registers_ui_panel(
        self, hass: HomeAssistant, server: SimpleNamespace
    ) -> None:
        # enable_sidebar_panel absent (default on): the admin-only "Open Web UI"
        # sidebar panel is registered during entry setup.
        assert await set_up(hass, add_server_entry(hass))

        server.register_panel.assert_awaited_once()

    async def test_sidebar_panel_off_skips_ui_panel_registration(
        self, hass: HomeAssistant, server: SimpleNamespace
    ) -> None:
        # enable_sidebar_panel=False: entry setup must not register the sidebar
        # panel (the user opted out of the sidebar entry point).
        entry = add_server_entry(hass, options={const.OPT_ENABLE_SIDEBAR_PANEL: False})

        assert await set_up(hass, entry)

        server.register_panel.assert_not_awaited()


class TestWebSocketCommandRegistration:
    """The server entry registers the ``ha_mcp_tools/*`` WS commands (#2289).

    They used to be registered only from the tools entry, so an install with
    just the server (embedded) entry had no ``ha_mcp_tools/*`` commands at all
    and every ``ha_search`` silently fell back to the legacy path.
    """

    async def test_setup_registers_ws_commands(
        self, hass: HomeAssistant, server: SimpleNamespace
    ) -> None:
        assert not _ws_commands(hass)

        assert await set_up(hass, add_server_entry(hass))

        assert _ws_commands(hass)

    async def test_ws_commands_registered_before_fallible_setup_steps(
        self, hass: HomeAssistant, server: SimpleNamespace
    ) -> None:
        # Placement pin: registration runs first, so a later setup step failing
        # (the sidebar panel here, the background bring-up in the field) never
        # costs a server-only install its command surface.
        server.register_panel.side_effect = RuntimeError("panel registration failed")
        entry = add_server_entry(hass)

        assert not await set_up(hass, entry)

        assert entry.state is ConfigEntryState.SETUP_ERROR
        assert _ws_commands(hass)


@pytest.fixture
async def register_view(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> MagicMock:
    """Home Assistant's own view registration, counted."""
    assert await async_setup_component(hass, "http", {})
    spy = MagicMock(wraps=hass.http.register_view)
    monkeypatch.setattr(hass.http, "register_view", spy)
    return spy


class TestPrebindOAuthViews:
    """Prebind every applicable OAuth route before slow background bring-up."""

    def _legacy_entry(self, hass: HomeAssistant, **options: object) -> MockConfigEntry:
        return add_server_entry(
            hass,
            options={
                const.OPT_WEBHOOK_AUTH: const.WEBHOOK_AUTH_LEGACY,
                const.OPT_ENABLE_WEBHOOK: True,
                **options,
            },
            data={
                const.DATA_OAUTH_CLIENT_ID: "cid",
                const.DATA_OAUTH_CLIENT_SECRET: "secret",
                const.DATA_OAUTH_SIGNING_KEY: secrets.token_hex(32),
            },
        )

    async def test_legacy_mode_binds_root_views_at_setup(
        self, hass: HomeAssistant, register_view: MagicMock
    ) -> None:
        hass.set_state(CoreState.not_running)

        eentry._prebind_oauth_views(hass, self._legacy_entry(hass))

        # 6 discovery + 3 scoped authorize/token/revoke + 2 root legacy aliases.
        assert register_view.call_count == 11
        assert hass.data.get(oauth_legacy.OAUTH_ROUTE_OWNER_KEY) == oauth_legacy._DOMAIN

    @pytest.mark.parametrize(
        "auth_mode",
        [const.WEBHOOK_AUTH_NONE, const.WEBHOOK_AUTH_HA],
    )
    async def test_none_and_ha_auth_modes_prebind_full_scoped_surface(
        self, hass: HomeAssistant, register_view: MagicMock, auth_mode: str
    ) -> None:
        entry = add_server_entry(
            hass,
            options={
                const.OPT_WEBHOOK_AUTH: auth_mode,
                const.OPT_ENABLE_WEBHOOK: True,
            },
        )

        eentry._prebind_oauth_views(hass, entry)

        # 6 discovery + 3 scoped authorize/token/revoke + 1 DCR registration
        # route.
        assert register_view.call_count == 10

    async def test_unsaved_options_prebind_the_secret_url_surface(
        self, hass: HomeAssistant, register_view: MagicMock
    ) -> None:
        # An entry that never saved its options runs in the secret-URL mode
        # bring-up defaults to (embedded_setup), so its OAuth routes must be
        # bound at boot too, or they never go live until a restart.
        eentry._prebind_oauth_views(hass, add_server_entry(hass))

        assert register_view.call_count == 10

    async def test_webhook_disabled_binds_nothing(
        self, hass: HomeAssistant, register_view: MagicMock
    ) -> None:
        entry = self._legacy_entry(hass, **{const.OPT_ENABLE_WEBHOOK: False})

        eentry._prebind_oauth_views(hass, entry)

        register_view.assert_not_called()

    async def test_partial_legacy_credentials_still_prebind_scoped_routes(
        self, hass: HomeAssistant, register_view: MagicMock
    ) -> None:
        # _ensure_secrets mints all three whenever legacy is configured; a gap
        # means a partial config, deferred to the bring-up path to surface.
        entry = self._legacy_entry(hass)
        hass.config_entries.async_update_entry(
            entry,
            data={
                k: v
                for k, v in entry.data.items()
                if k != const.DATA_OAUTH_CLIENT_SECRET
            },
        )

        eentry._prebind_oauth_views(hass, entry)

        # Root aliases wait for credentials, but advertised scoped routes must
        # still beat Home Assistant's HTTP freeze: 6 discovery + 3 scoped
        # authorize/token/revoke.
        assert register_view.call_count == 9

    async def test_route_conflict_is_swallowed_at_setup(
        self, hass: HomeAssistant, register_view: MagicMock
    ) -> None:
        # The webhook-proxy add-on owning the root routes makes
        # bind_legacy_views raise; setup must survive (the bring-up
        # re-encounters the conflict and files the user-facing repair).
        hass.set_state(CoreState.not_running)
        hass.data[oauth_legacy.OAUTH_ROUTE_OWNER_KEY] = "webhook_proxy_addon"

        eentry._prebind_oauth_views(hass, self._legacy_entry(hass))

        assert hass.data[oauth_legacy.OAUTH_ROUTE_OWNER_KEY] == "webhook_proxy_addon"
        # The foreign owner blocks only the root aliases; scoped routes remain
        # (6 discovery + 3 scoped authorize/token/revoke).
        assert register_view.call_count == 9


class TestEnsureSecretsLegacyWiring:
    """`_ensure_secrets` is the persistence path for the legacy credential
    lifecycle: it must fold `_ensure_legacy_oauth_secrets` mutations into the
    same entry update — including OPTIONS (the one-shot regenerate flag lives
    there), which pre-legacy code never persisted."""

    def test_legacy_regenerate_persists_new_creds_and_clears_flag(
        self, hass: HomeAssistant
    ) -> None:
        entry = add_server_entry(
            hass,
            options={
                const.OPT_WEBHOOK_AUTH: const.WEBHOOK_AUTH_LEGACY,
                const.OPT_OAUTH_REGENERATE: True,
            },
        )

        eentry._ensure_secrets(hass, entry)

        assert entry.data[const.DATA_OAUTH_CLIENT_ID].startswith("hamcp-")
        assert entry.data[const.DATA_OAUTH_CLIENT_SECRET]
        assert entry.data[const.DATA_OAUTH_SIGNING_KEY]
        assert entry.options[const.OPT_OAUTH_REGENERATE] is False

    def test_non_legacy_mode_mints_no_oauth_credentials(
        self, hass: HomeAssistant
    ) -> None:
        entry = add_server_entry(
            hass, options={const.OPT_WEBHOOK_AUTH: const.WEBHOOK_AUTH_NONE}
        )
        before = dict(entry.data)

        eentry._ensure_secrets(hass, entry)

        # Webhook id + secret path already present -> nothing changed (and in
        # particular no OAuth minting).
        assert entry.data == before


class TestUnload:
    async def test_unload_tears_down_the_server(
        self, hass: HomeAssistant, server: SimpleNamespace
    ) -> None:
        entry = add_server_entry(hass)
        assert await set_up(hass, entry)

        assert await hass.config_entries.async_unload(entry.entry_id)

        server.teardown.assert_awaited_once_with(hass)
        server.unregister_panel.assert_called_once_with(hass)
