"""The in-process server's bring-up orchestration (issue #1527), on a real ``hass``.

``embedded_setup`` is the glue between the server manager and the webhook
ingress: the background bring-up sequence, repair issues, connect-URL surfacing,
teardown and credential revocation on removal. The integration is always-on, so
there is no enable/disable gate here.

A real server config entry, issue registry, persistent notifications and
network adapters are Core's. Only this component's own collaborators (the
server thread, the webhook and LLM API registration, the Nabu Casa lookup) are
replaced (``Collaborators``); ``Notices`` records the persistent notifications
Core's own service raises and dismisses.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import AsyncIterator, Callable, Mapping
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import ifaddr
import jwt
import pytest
from homeassistant.components import persistent_notification
from homeassistant.components.persistent_notification import (
    Notification,
    UpdateType,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.core_config import async_process_ha_core_config
from homeassistant.helpers import issue_registry as ir
from homeassistant.loader import (
    Integration,
    IntegrationNotFound,
    async_get_integration,
)
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.typing import WebSocketGenerator

from custom_components.ha_mcp_tools import embedded_setup as esetup
from custom_components.ha_mcp_tools.const import (
    BIND_HOST_ALL,
    DATA_DCR_SIGNING_KEY,
    DATA_MANAGER,
    DATA_OAUTH_CLIENT_ID,
    DATA_OAUTH_CLIENT_SECRET,
    DATA_SECRET_PATH,
    DATA_WEBHOOK_ID,
    DEFAULT_SERVER_PORT,
    DOMAIN,
    ISSUE_COMPONENT_OUTDATED,
    ISSUE_LEGACY_OAUTH_RESTART,
    ISSUE_PACKAGE_FAILED,
    ISSUE_START_FAILED,
    ISSUE_TOKEN_NEEDED,
    OPT_BIND_HOST,
    OPT_ENABLE_LLM_API,
    OPT_ENABLE_SIDEBAR_PANEL,
    OPT_ENABLE_STARTUP_NOTIFICATION,
    OPT_ENABLE_WEBHOOK,
    OPT_EXTERNAL_URL,
    OPT_SERVER_PORT,
    OPT_WEBHOOK_AUTH,
    SERVER_UPDATES_DOCS_URL,
    WEBHOOK_AUTH_HA,
    WEBHOOK_AUTH_LEGACY,
)

from ._server_entry import add_server_entry

SERVER_MODULE = "ha_mcp.tools.tools_filesystem"
LAN_URL = "http://192.168.1.5:8123"
NABU_URL = "https://abc.ui.nabu.casa"
PANEL_LINK = "[HA-MCP settings panel](/ha-mcp)"
WEBHOOK_DATA = {DATA_WEBHOOK_ID: "mcp_id", DATA_SECRET_PATH: "/priv"}
STALE_ISSUES = (
    ISSUE_PACKAGE_FAILED,
    ISSUE_START_FAILED,
    ISSUE_TOKEN_NEEDED,
    ISSUE_LEGACY_OAUTH_RESTART,
)


class Collaborators:
    """The replaced collaborators a bring-up test asserts on."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.start = AsyncMock()
        self.stop = AsyncMock()
        self.register_webhook = AsyncMock(return_value=False)
        self.register_llm_api = AsyncMock()
        self.legacy_active = MagicMock(return_value=True)
        replaced: dict[tuple[object, str], object] = {
            (esetup.EmbeddedServerManager, "async_start"): self.start,
            (esetup.EmbeddedServerManager, "async_stop"): self.stop,
            (esetup, "async_register_webhook"): self.register_webhook,
            (esetup, "async_register_llm_api"): self.register_llm_api,
            (esetup, "legacy_credentials_active"): self.legacy_active,
        }
        for (target, name), spy in replaced.items():
            monkeypatch.setattr(target, name, spy)
        server_not_installed(monkeypatch)

    @property
    def webhook_kwargs(self) -> Mapping[str, Any]:
        """The keyword arguments the webhook was last registered with."""
        call = self.register_webhook.await_args
        assert call is not None
        return call.kwargs

    @property
    def llm_api_kwargs(self) -> Mapping[str, Any]:
        """The keyword arguments the LLM API was last registered with."""
        call = self.register_llm_api.await_args
        assert call is not None
        return call.kwargs


def server_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ha-mcp package is not importable, so no component minimum is read."""
    monkeypatch.setitem(sys.modules, SERVER_MODULE, None)


def server_requires_component(
    monkeypatch: pytest.MonkeyPatch, version: str | None
) -> None:
    """The installed server declares ``version`` as its minimum component, or
    ``None`` for an older server layout that declares no minimum."""
    module = ModuleType(SERVER_MODULE)
    if version is not None:
        module.__dict__["MIN_COMPONENT_VERSION"] = version
    monkeypatch.setitem(sys.modules, SERVER_MODULE, module)


async def log_in_to_nabu_casa(hass: HomeAssistant, url: str) -> None:
    """Home Assistant Cloud set up and logged in, with remote access on ``url``.

    The login is the account token Core's cloud tests use; nothing reaches
    Nabu Casa's servers.
    """
    from homeassistant.components.cloud.const import DATA_CLOUD

    assert await async_setup_component(hass, "cloud", {"cloud": {}})
    cloud = hass.data[DATA_CLOUD]
    cloud.id_token = jwt.encode(
        {"email": "user@example.com", "custom:sub-exp": "2099-01-01"},
        "an-unverified-signing-key-of-32-bytes",
    )
    await cloud.client.prefs.async_update(
        remote_enabled=True, remote_domain=url.removeprefix("https://")
    )


async def set_internal_url(hass: HomeAssistant, url: str) -> None:
    """Configure Home Assistant's internal URL through Core's own config."""
    await async_process_ha_core_config(hass, {"internal_url": url})


class Notices:
    """The persistent notifications Core's service raised or dismissed."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.added: list[Notification] = []
        self.removed: list[str] = []
        self.stop = persistent_notification.async_register_callback(hass, self._record)

    @callback
    def _record(
        self, update_type: UpdateType, notifications: dict[str, Notification]
    ) -> None:
        if update_type in (UpdateType.ADDED, UpdateType.UPDATED):
            self.added.extend(notifications.values())
        elif update_type is UpdateType.REMOVED:
            self.removed.extend(notifications)

    @property
    def message(self) -> str:
        """The message of the one notification that was raised."""
        assert len(self.added) == 1, self.added
        return self.added[0]["message"]


@pytest.fixture
def collaborators(
    monkeypatch: pytest.MonkeyPatch, enable_custom_integrations: None
) -> Collaborators:
    return Collaborators(monkeypatch)


@pytest.fixture
async def notices(hass: HomeAssistant) -> AsyncIterator[Notices]:
    recorded = Notices(hass)
    yield recorded
    recorded.stop()


def _file_issue(hass: HomeAssistant, issue_id: str) -> None:
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=issue_id,
    )


def _issue(hass: HomeAssistant, issue_id: str) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, issue_id)


class TestBringUp:
    async def test_success_starts_registers_and_announces_the_server(
        self,
        hass: HomeAssistant,
        collaborators: Collaborators,
        notices: Notices,
    ) -> None:
        entry = add_server_entry(hass)

        await esetup.async_bring_up_server(hass, entry)

        collaborators.start.assert_awaited_once()
        collaborators.register_webhook.assert_awaited_once()
        assert isinstance(hass.data[DOMAIN][DATA_MANAGER], esetup.EmbeddedServerManager)
        assert not any(_issue(hass, issue_id) for issue_id in STALE_ISSUES)
        assert notices.added[0]["title"] == "HA-MCP Server"
        llm_api = collaborators.llm_api_kwargs
        assert llm_api == {"port": DEFAULT_SERVER_PORT, "secret_path": "/private_x"}

    async def test_a_started_from_the_access_switch_raises_no_notification(
        self,
        hass: HomeAssistant,
        collaborators: Collaborators,
        notices: Notices,
    ) -> None:
        """Turning the access switch on (#2801) must not repeat itself in a
        "running" notification on every toggle."""
        entry = add_server_entry(hass)

        await esetup.async_bring_up_server(hass, entry, announce=False)

        collaborators.start.assert_awaited_once()
        assert notices.added == []

    async def test_a_fixed_install_stops_showing_the_old_repairs(
        self, hass: HomeAssistant, collaborators: Collaborators
    ) -> None:
        for issue_id in STALE_ISSUES:
            _file_issue(hass, issue_id)
        entry = add_server_entry(hass)

        await esetup.async_bring_up_server(hass, entry)

        assert [i for i in STALE_ISSUES if _issue(hass, i)] == []

    async def test_passes_auth_mode_port_and_secret_to_the_webhook(
        self, hass: HomeAssistant, collaborators: Collaborators
    ) -> None:
        entry = add_server_entry(
            hass,
            data={DATA_SECRET_PATH: "/private_secret"},
            options={OPT_WEBHOOK_AUTH: WEBHOOK_AUTH_HA},
        )

        await esetup.async_bring_up_server(hass, entry)

        kwargs = collaborators.webhook_kwargs
        assert kwargs["auth_mode"] == WEBHOOK_AUTH_HA
        assert kwargs["port"] == DEFAULT_SERVER_PORT
        assert kwargs["secret_path"] == "/private_secret"
        assert kwargs["register_endpoint"] is True

    async def test_an_entry_without_a_signing_key_gets_one_before_registration(
        self, hass: HomeAssistant, collaborators: Collaborators
    ) -> None:
        """An entry created before 2.0.0 has no DCR key: registration must get
        the key the entry now stores, not a missing one."""
        entry = add_server_entry(hass)
        assert DATA_DCR_SIGNING_KEY not in entry.data

        await esetup.async_bring_up_server(hass, entry)

        stored = entry.data[DATA_DCR_SIGNING_KEY]
        assert stored
        kwargs = collaborators.webhook_kwargs
        assert kwargs["dcr_signing_key"] == stored

    async def test_a_custom_port_reaches_the_webhook_and_the_llm_api(
        self, hass: HomeAssistant, collaborators: Collaborators
    ) -> None:
        entry = add_server_entry(hass, options={OPT_SERVER_PORT: 9999})

        await esetup.async_bring_up_server(hass, entry)

        assert collaborators.webhook_kwargs["port"] == 9999
        assert collaborators.llm_api_kwargs["port"] == 9999

    async def test_local_only_skips_the_endpoint_but_keeps_the_forwarding(
        self,
        hass: HomeAssistant,
        collaborators: Collaborators,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """enable_webhook=False never registers the public endpoint, yet the
        forwarding config is still set up or the sidebar panel 503s (#1803)."""
        entry = add_server_entry(hass, options={OPT_ENABLE_WEBHOOK: False})

        with caplog.at_level(logging.INFO):
            await esetup.async_bring_up_server(hass, entry)

        collaborators.start.assert_awaited_once()
        kwargs = collaborators.webhook_kwargs
        assert kwargs["register_endpoint"] is False
        assert "local-only" in caplog.text

    async def test_llm_api_option_off_skips_only_the_registration(
        self,
        hass: HomeAssistant,
        collaborators: Collaborators,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        entry = add_server_entry(hass, options={OPT_ENABLE_LLM_API: False})

        with caplog.at_level(logging.INFO):
            await esetup.async_bring_up_server(hass, entry)

        collaborators.start.assert_awaited_once()
        collaborators.register_webhook.assert_awaited_once()
        collaborators.register_llm_api.assert_not_awaited()
        assert "LLM API disabled by option" in caplog.text

    async def test_cancelled_tears_down_and_reraises(
        self, hass: HomeAssistant, collaborators: Collaborators
    ) -> None:
        entry = add_server_entry(hass)
        collaborators.start.side_effect = asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await esetup.async_bring_up_server(hass, entry)

        collaborators.stop.assert_awaited_once()
        assert not any(_issue(hass, issue_id) for issue_id in STALE_ISSUES)


class TestLegacyOAuthBringUp:
    """What a legacy-OAuth bring-up tells the admin about the credentials."""

    async def test_a_pending_rotation_logs_that_the_previous_credentials_stay_active(
        self,
        hass: HomeAssistant,
        collaborators: Collaborators,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The bring-up asks whether the bound provider serves the configured
        credentials and says so in the log (review finding on #1880)."""
        collaborators.legacy_active.return_value = False
        entry = add_server_entry(hass, options={OPT_WEBHOOK_AUTH: WEBHOOK_AUTH_LEGACY})

        with caplog.at_level(logging.INFO):
            await esetup.async_bring_up_server(hass, entry)

        collaborators.legacy_active.assert_called_once()
        assert "previous credentials remain active" in caplog.text
        assert "Configure" in caplog.text

    async def test_a_restart_needed_files_the_repair_and_logs_the_caveat(
        self,
        hass: HomeAssistant,
        collaborators: Collaborators,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """First enable mid-session late-binds the views, so /authorize is not
        live until the restart the repair asks for."""
        collaborators.register_webhook.return_value = True
        entry = add_server_entry(hass, options={OPT_WEBHOOK_AUTH: WEBHOOK_AUTH_LEGACY})

        with caplog.at_level(logging.INFO):
            await esetup.async_bring_up_server(hass, entry)

        issue = _issue(hass, ISSUE_LEGACY_OAUTH_RESTART)
        assert issue is not None
        assert issue.is_fixable is True
        assert "not live until the restart" in caplog.text

    async def test_live_credentials_log_no_caveat_and_never_leak(
        self,
        hass: HomeAssistant,
        collaborators: Collaborators,
        caplog: pytest.LogCaptureFixture,
        notices: Notices,
    ) -> None:
        """The legacy client secret was once logged in cleartext (#2427): the
        log says where the Configure screen shows it, and neither the log nor
        the notification every user can read carries it."""
        entry = add_server_entry(
            hass,
            data={
                DATA_OAUTH_CLIENT_ID: "cid-abc123",
                DATA_OAUTH_CLIENT_SECRET: "sec-xyz789",
            },
            options={OPT_WEBHOOK_AUTH: WEBHOOK_AUTH_LEGACY},
        )

        with caplog.at_level(logging.INFO):
            await esetup.async_bring_up_server(hass, entry)

        for surface in (caplog.text, notices.message):
            assert "cid-abc123" not in surface
            assert "sec-xyz789" not in surface
        assert "Client Secret" in caplog.text
        assert "Configure" in caplog.text
        assert "not live until the restart" not in caplog.text
        assert _issue(hass, ISSUE_LEGACY_OAUTH_RESTART) is None


class TestConnectUrlSurfaces:
    """The start-up log and notification point at the Configure screen and carry
    no connect URL, which in the default mode is the credential (#2427)."""

    async def test_neither_log_nor_notification_carries_urls_or_secrets(
        self,
        hass: HomeAssistant,
        collaborators: Collaborators,
        caplog: pytest.LogCaptureFixture,
        notices: Notices,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await log_in_to_nabu_casa(hass, NABU_URL)
        await set_internal_url(hass, LAN_URL)
        entry = add_server_entry(
            hass,
            data={DATA_WEBHOOK_ID: "mcp_id", DATA_SECRET_PATH: "/private_x"},
            options={OPT_BIND_HOST: BIND_HOST_ALL},
        )

        with caplog.at_level(logging.INFO):
            await esetup.async_bring_up_server(hass, entry)

        for surface in (caplog.text, notices.message):
            assert "mcp_id" not in surface
            assert "/private_x" not in surface
            assert "/api/webhook/" not in surface
            assert "Configure" in surface
        assert "HA-MCP in-process server is running" in caplog.text
        # The URLs still resolve for the Configure screen.
        urls = esetup.build_connect_urls(hass, entry)
        assert f"{NABU_URL}/api/webhook/mcp_id" in urls
        assert f"{LAN_URL}/api/webhook/mcp_id" in urls

    async def test_notification_links_the_panel_and_explains_how_to_silence_it(
        self,
        hass: HomeAssistant,
        collaborators: Collaborators,
        notices: Notices,
    ) -> None:
        entry = add_server_entry(hass)

        await esetup.async_bring_up_server(hass, entry)

        assert PANEL_LINK in notices.message
        assert notices.message.endswith(
            "\n\nTo disable this notification, uncheck the startup notification "
            "box on that same configuration screen.\n"
        )

    async def test_sidebar_panel_off_drops_the_panel_link(
        self,
        hass: HomeAssistant,
        collaborators: Collaborators,
        notices: Notices,
    ) -> None:
        """With the panel option off the /ha-mcp route does not exist, so the
        notification must not link it."""
        entry = add_server_entry(hass, options={OPT_ENABLE_SIDEBAR_PANEL: False})

        await esetup.async_bring_up_server(hass, entry)

        assert "(/ha-mcp)" not in notices.message
        assert "Configure" in notices.message

    async def test_startup_notification_off_dismisses_the_old_one_and_still_logs(
        self,
        hass: HomeAssistant,
        collaborators: Collaborators,
        notices: Notices,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        entry = add_server_entry(hass)
        await esetup.async_bring_up_server(hass, entry)
        raised = notices.added[0]["notification_id"]
        hass.config_entries.async_update_entry(
            entry, options={OPT_ENABLE_STARTUP_NOTIFICATION: False}
        )

        with caplog.at_level(logging.INFO):
            await esetup.async_bring_up_server(hass, entry)

        assert notices.removed == [raised]
        assert len(notices.added) == 1
        assert "HA-MCP in-process server is running" in caplog.text

    async def test_local_only_notification_says_the_webhook_is_disabled(
        self,
        hass: HomeAssistant,
        collaborators: Collaborators,
        notices: Notices,
    ) -> None:
        entry = add_server_entry(hass, options={OPT_ENABLE_WEBHOOK: False})

        await esetup.async_bring_up_server(hass, entry)

        assert "disabled" in notices.message


class TestRevokeOnRemove:
    async def test_revokes_credentials_and_clears_every_repair(
        self, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The restart repair is filed only from bring-up, which never runs
        again for a removed entry: removal must clear it, and the others, or a
        gone server leaves dangling warnings."""
        revoke = AsyncMock()
        monkeypatch.setattr(
            esetup.EmbeddedServerManager, "async_revoke_credentials", revoke
        )
        every_issue = (*STALE_ISSUES, ISSUE_COMPONENT_OUTDATED)
        for issue_id in every_issue:
            _file_issue(hass, issue_id)
        entry = add_server_entry(hass)

        await esetup.async_revoke_credentials_on_remove(hass, entry)

        revoke.assert_awaited_once()
        assert [i for i in every_issue if _issue(hass, i)] == []


def _entry(
    hass: HomeAssistant,
    *,
    data: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
) -> MockConfigEntry:
    return add_server_entry(
        hass, data={**WEBHOOK_DATA, **(data or {})}, options=options
    )


class TestBuildConnectUrls:
    """The shared URL resolver behind the Configure screen."""

    async def test_cloud_url_external_url_and_local_url_in_that_order(
        self, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A configured external URL leads the list (webhook-proxy app parity),
        ahead of Nabu Casa and the local address."""
        await log_in_to_nabu_casa(hass, NABU_URL)
        await set_internal_url(hass, LAN_URL)
        entry = _entry(hass, options={OPT_EXTERNAL_URL: "https://ha.example.com/"})

        urls = esetup.build_connect_urls(hass, entry)

        assert urls[:3] == [
            "https://ha.example.com/api/webhook/mcp_id",
            f"{NABU_URL}/api/webhook/mcp_id",
            f"{LAN_URL}/api/webhook/mcp_id",
        ]

    async def test_a_host_without_the_cloud_integration_lists_only_local_urls(
        self, hass: HomeAssistant
    ) -> None:
        """Plain Core has no cloud integration at all: the lookup must degrade
        to the local URL, not raise."""
        await set_internal_url(hass, LAN_URL)

        urls = esetup.build_connect_urls(hass, _entry(hass))

        assert f"{LAN_URL}/api/webhook/mcp_id" in urls

    async def test_without_any_url_the_hint_form_is_listed(
        self, hass: HomeAssistant
    ) -> None:
        urls = esetup.build_connect_urls(hass, _entry(hass))

        assert any("/api/webhook/mcp_id" in url for url in urls)

    async def test_direct_access_line_names_the_resolved_host_and_default_port(
        self, hass: HomeAssistant
    ) -> None:
        """An entry that never saved bind_host inherits the LAN default, so the
        admin gets a URL to paste verbatim, not a placeholder."""
        await set_internal_url(hass, LAN_URL)

        urls = esetup.build_connect_urls(hass, _entry(hass))

        assert [u for u in urls if "(direct access)" in u] == [
            "http://192.168.1.5:9584/priv (direct access)"
        ]

    async def test_direct_access_line_uses_the_configured_port(
        self, hass: HomeAssistant
    ) -> None:
        await set_internal_url(hass, LAN_URL)
        entry = _entry(hass, options={OPT_BIND_HOST: "0.0.0.0", OPT_SERVER_PORT: 9999})

        assert "http://192.168.1.5:9999/priv (direct access)" in (
            esetup.build_connect_urls(hass, entry)
        )

    async def test_loopback_bind_omits_the_direct_access_line(
        self, hass: HomeAssistant
    ) -> None:
        await set_internal_url(hass, LAN_URL)
        entry = _entry(hass, options={OPT_BIND_HOST: "127.0.0.1"})

        urls = esetup.build_connect_urls(hass, entry)

        assert not any("(direct access)" in url for url in urls)

    async def test_a_missing_secret_path_omits_the_direct_access_line(
        self, hass: HomeAssistant
    ) -> None:
        """A URL must never render without its secret segment."""
        await set_internal_url(hass, LAN_URL)
        entry = _entry(hass, data={DATA_SECRET_PATH: ""})

        urls = esetup.build_connect_urls(hass, entry)

        assert not any("(direct access)" in url for url in urls)

    async def test_local_only_lists_the_direct_url_and_no_webhook_url(
        self, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The webhook is never registered, so the external, Nabu Casa and local
        webhook forms are all suppressed while the direct URL stays."""
        await log_in_to_nabu_casa(hass, NABU_URL)
        await set_internal_url(hass, LAN_URL)
        entry = _entry(hass, options={OPT_EXTERNAL_URL: "https://ha.example.com"})

        urls = esetup.build_connect_urls(hass, entry, webhook_enabled=False)

        assert not any("/api/webhook/" in url for url in urls)
        assert "http://192.168.1.5:9584/priv (direct access)" in urls

    async def test_every_interface_gets_a_webhook_and_a_direct_url(
        self, hass: HomeAssistant
    ) -> None:
        """#1862: a multi-VLAN host surfaces each LAN address, the canonical
        host first, and the canonical host repeated in the list is not doubled."""
        await set_internal_url(hass, "http://10.0.2.3:8123")

        urls = esetup.build_connect_urls(
            hass, _entry(hass), extra_hosts=["10.0.2.3", "10.0.1.3"]
        )

        assert [u for u in urls if "/api/webhook/" in u] == [
            "http://10.0.2.3:8123/api/webhook/mcp_id",
            "http://10.0.1.3:8123/api/webhook/mcp_id",
        ]
        assert [u for u in urls if "(direct access)" in u] == [
            "http://10.0.2.3:9584/priv (direct access)",
            "http://10.0.1.3:9584/priv (direct access)",
        ]

    async def test_adapter_hosts_surface_the_direct_line_without_an_internal_url(
        self, hass: HomeAssistant
    ) -> None:
        urls = esetup.build_connect_urls(hass, _entry(hass), extra_hosts=["10.0.1.3"])

        assert [u for u in urls if "(direct access)" in u] == [
            "http://10.0.1.3:9584/priv (direct access)"
        ]

    async def test_a_portless_internal_url_stays_portless_for_other_hosts(
        self, hass: HomeAssistant
    ) -> None:
        """A reverse-proxied internal URL has no port: the extra adapter host
        must not be given one."""
        await set_internal_url(hass, "https://ha.internal")

        urls = esetup.build_connect_urls(hass, _entry(hass), extra_hosts=["10.0.1.3"])

        assert [u for u in urls if "/api/webhook/" in u] == [
            "https://ha.internal/api/webhook/mcp_id",
            "https://10.0.1.3/api/webhook/mcp_id",
        ]

    async def test_no_host_at_all_falls_back_to_the_placeholder(
        self, hass: HomeAssistant
    ) -> None:
        """Neither an internal URL nor adapters: the direct line keeps a
        placeholder host rather than vanishing or rendering host-less."""
        urls = esetup.build_connect_urls(hass, _entry(hass))

        assert [u for u in urls if "(direct access)" in u] == [
            "http://<home-assistant-ip>:9584/priv (direct access)"
        ]


def _adapters() -> list[ifaddr.Adapter]:
    return [
        ifaddr.Adapter("eth0", "eth0", [ifaddr.IP("10.0.2.3", 24, "eth0")], 1),
        ifaddr.Adapter(
            "eth1",
            "eth1",
            [ifaddr.IP("10.0.1.3", 24, "eth1"), ifaddr.IP("10.0.1.4", 24, "eth1")],
            2,
        ),
        ifaddr.Adapter("eth2", "eth2", [ifaddr.IP("10.9.9.9", 24, "eth2")], 3),
    ]


class TestAsyncGetLanHosts:
    """The per-interface IPv4 list that feeds ``build_connect_urls`` (#1862)."""

    async def test_lists_enabled_adapter_ipv4_in_order(
        self, hass: HomeAssistant, hass_ws_client: WebSocketGenerator
    ) -> None:
        with patch(
            "homeassistant.components.network.util.ifaddr.get_adapters",
            return_value=_adapters(),
        ):
            assert await async_setup_component(hass, "network", {})
            client = await hass_ws_client(hass)
            await client.send_json_auto_id(
                {
                    "type": "network/configure",
                    "config": {"configured_adapters": ["eth0", "eth1"]},
                }
            )
            assert (await client.receive_json())["success"]

            hosts = await esetup.async_get_lan_hosts(hass)

        assert hosts == ["10.0.2.3", "10.0.1.3", "10.0.1.4"]

    async def test_degrades_to_empty_when_the_adapter_lookup_fails(
        self, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A failed lookup must yield [] so URL surfacing (and bring-up) never
        breaks for a display-only enumeration."""
        with patch(
            "homeassistant.components.network.util.ifaddr.get_adapters",
            side_effect=OSError("no adapters"),
        ):
            hosts = await esetup.async_get_lan_hosts(hass)

        assert hosts == []
        assert "Adapter enumeration failed" in caplog.text


class TestComponentCompat:
    """The component-outdated repair the bring-up files or clears."""

    async def test_an_outdated_component_files_a_warning_with_both_versions(
        self,
        hass: HomeAssistant,
        collaborators: Collaborators,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        server_requires_component(monkeypatch, "999.0.0")
        installed = (await async_get_integration(hass, DOMAIN)).version

        await esetup.async_bring_up_server(hass, add_server_entry(hass))

        issue = _issue(hass, ISSUE_COMPONENT_OUTDATED)
        assert issue is not None
        assert issue.translation_placeholders == {
            "required": "999.0.0",
            "installed": str(installed),
        }
        assert issue.learn_more_url == SERVER_UPDATES_DOCS_URL
        assert issue.severity == ir.IssueSeverity.WARNING
        assert issue.is_fixable is False

    async def test_a_satisfied_component_clears_the_issue(
        self,
        hass: HomeAssistant,
        collaborators: Collaborators,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _file_issue(hass, ISSUE_COMPONENT_OUTDATED)
        server_requires_component(monkeypatch, "0.0.1")

        await esetup.async_bring_up_server(hass, add_server_entry(hass))

        assert _issue(hass, ISSUE_COMPONENT_OUTDATED) is None

    @pytest.mark.parametrize(
        "no_minimum",
        [
            pytest.param(server_not_installed, id="server-not-installed"),
            pytest.param(
                lambda monkeypatch: server_requires_component(monkeypatch, None),
                id="server-declares-no-minimum",
            ),
        ],
    )
    async def test_a_server_that_declares_no_minimum_leaves_the_issue_alone(
        self,
        hass: HomeAssistant,
        collaborators: Collaborators,
        monkeypatch: pytest.MonkeyPatch,
        no_minimum: Callable[[pytest.MonkeyPatch], None],
    ) -> None:
        """An older server layout or no server at all has nothing to enforce:
        neither file nor clear the issue."""
        _file_issue(hass, ISSUE_COMPONENT_OUTDATED)
        no_minimum(monkeypatch)

        await esetup.async_bring_up_server(hass, add_server_entry(hass))

        assert _issue(hass, ISSUE_COMPONENT_OUTDATED) is not None

    async def test_an_unreadable_component_version_never_blocks_the_server(
        self,
        hass: HomeAssistant,
        collaborators: Collaborators,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The check is advisory: a loader failure is logged and swallowed, the
        server keeps coming up and no repair is filed."""
        server_requires_component(monkeypatch, "999.0.0")
        monkeypatch.setattr(
            esetup,
            "async_get_integration",
            AsyncMock(side_effect=IntegrationNotFound(DOMAIN)),
        )

        await esetup.async_bring_up_server(hass, add_server_entry(hass))

        assert "Could not read the HA-MCP component version" in caplog.text
        assert _issue(hass, ISSUE_COMPONENT_OUTDATED) is None
        collaborators.register_webhook.assert_awaited_once()

    async def test_a_version_less_manifest_is_reported_not_compared_as_text(
        self,
        hass: HomeAssistant,
        collaborators: Collaborators,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A manifest without a version reads as None; without the guard the
        literal "None" would reach the comparison and decide the outcome
        quietly. The warning is what discriminates."""
        server_requires_component(monkeypatch, "999.0.0")
        real = await async_get_integration(hass, DOMAIN)
        manifest = real.manifest.copy()
        del manifest["version"]
        versionless = Integration(hass, real.pkg_path, real.file_path, manifest)
        monkeypatch.setattr(
            esetup, "async_get_integration", AsyncMock(return_value=versionless)
        )

        await esetup.async_bring_up_server(hass, add_server_entry(hass))

        assert "Could not read the HA-MCP component version" in caplog.text
        assert _issue(hass, ISSUE_COMPONENT_OUTDATED) is None
