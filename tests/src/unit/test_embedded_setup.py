"""Unit tests for the in-process server bring-up orchestration (issue #1527).

``embedded_setup`` is the glue between the server manager and the webhook ingress:
the background bring-up sequence, repair issues on failure (Home Assistant must
keep running), connect-URL surfacing, teardown, and credential revocation on
removal. The integration is always-on — the config entry existing means the
server runs — so there is no enable/disable gate here.

Home Assistant / aiohttp are stubbed via ``_embedded_stubs`` (which also puts
the component package on sys.path). The server manager and webhook
register/unregister functions are patched so these tests exercise only the
orchestration decisions.
"""

from __future__ import annotations

import asyncio
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ._embedded_stubs import install

install()

import custom_components.ha_mcp_tools.embedded_setup as esetup  # noqa: E402

# Captured before any test patches it so the connect-URL tests can restore the
# real implementation regardless of the module-level spy.
_REAL_SURFACE_CONNECT_URLS = esetup._surface_connect_urls

from custom_components.ha_mcp_tools.const import (  # noqa: E402
    DATA_MANAGER,
    DATA_SECRET_PATH,
    DATA_WEBHOOK_ID,
    DOMAIN,
    ISSUE_COMPONENT_OUTDATED,
    ISSUE_PACKAGE_FAILED,
    ISSUE_START_FAILED,
    OPT_WEBHOOK_AUTH,
    SERVER_UPDATES_DOCS_URL,
    WEBHOOK_AUTH_HA,
)


def _make_hass() -> MagicMock:
    hass = MagicMock(name="hass")
    hass.data = {}
    hass.config.skip_pip = False

    def _update_entry(entry, *, data=None, **_kw):
        if data is not None:
            entry.data = data

    hass.config_entries.async_update_entry = MagicMock(side_effect=_update_entry)

    def _create_task(coro, *args, **kwargs):
        # These tests assert scheduling decisions, not the scheduled work —
        # close the real coroutine so it is never left un-awaited.
        if asyncio.iscoroutine(coro):
            coro.close()

    hass.async_create_task = MagicMock(side_effect=_create_task)

    async def _executor(func, *args):
        return func(*args)

    # The bring-up path runs the component-compat check, which offloads the
    # MIN_COMPONENT_VERSION read to the executor; give every hass a working one
    # (the real check then self-skips because ha_mcp is not installed here).
    hass.async_add_executor_job = AsyncMock(side_effect=_executor)
    return hass


def _make_entry(*, options=None, data=None) -> MagicMock:
    entry = MagicMock(name="entry")
    entry.options = {} if options is None else dict(options)
    entry.data = {DATA_SECRET_PATH: "/private_x"} if data is None else dict(data)
    return entry


@pytest.fixture
def fake_manager(monkeypatch):
    """Patch EmbeddedServerManager with a real fake class.

    A real class (not a lambda/MagicMock) is required because
    ``async_teardown_server`` does ``isinstance(manager, EmbeddedServerManager)``.
    The async methods live on the class as shared AsyncMocks so tests assert on
    ``fake_manager.async_start`` regardless of which instance the code built.
    Returns the class.
    """

    class FakeManager:
        port = 9584
        async_start = AsyncMock()
        async_stop = AsyncMock()
        async_revoke_credentials = AsyncMock()

        def __init__(self, hass, entry):
            self.hass = hass
            self.entry = entry

    monkeypatch.setattr(esetup, "EmbeddedServerManager", FakeManager)
    return FakeManager


@pytest.fixture(autouse=True)
def _spy(monkeypatch):
    """Patch webhook register/unregister, issue-registry, and connect-URL
    surfacing to spies (the connect-URL tests restore the real surfacing)."""
    monkeypatch.setattr(esetup, "async_register_webhook", AsyncMock(return_value=False))
    monkeypatch.setattr(esetup, "async_unregister_webhook", AsyncMock())
    monkeypatch.setattr(esetup, "async_register_llm_api", AsyncMock())
    monkeypatch.setattr(esetup, "async_unregister_llm_api", MagicMock())
    monkeypatch.setattr(esetup.ir, "async_create_issue", MagicMock())
    monkeypatch.setattr(esetup.ir, "async_delete_issue", MagicMock())
    monkeypatch.setattr(esetup, "_surface_connect_urls", MagicMock())


class TestBringUp:
    async def test_legacy_bring_up_passes_creds_active_verdict(
        self, fake_manager, monkeypatch
    ):
        # The bring-up must consult legacy_credentials_active and hand its
        # verdict to _surface_connect_urls -- the gate that keeps rotated
        # credentials out of the startup log (review finding on #1880).
        monkeypatch.setattr(
            esetup, "legacy_credentials_active", MagicMock(return_value=False)
        )
        hass = _make_hass()
        entry = _make_entry(
            options={esetup.OPT_WEBHOOK_AUTH: esetup.WEBHOOK_AUTH_LEGACY}
        )

        await esetup.async_bring_up_server(hass, entry)

        esetup.legacy_credentials_active.assert_called_once()
        assert (
            esetup._surface_connect_urls.call_args.kwargs["oauth_creds_active"] is False
        )

    async def test_legacy_restart_needed_files_repair(self, fake_manager, monkeypatch):
        # Review gap: every bring-up test mocked async_register_webhook with
        # restart_needed=False, so the create-issue branch of
        # _async_update_legacy_oauth_issue was never exercised.
        monkeypatch.setattr(
            esetup, "async_register_webhook", AsyncMock(return_value=True)
        )
        hass = _make_hass()
        entry = _make_entry(
            options={esetup.OPT_WEBHOOK_AUTH: esetup.WEBHOOK_AUTH_LEGACY}
        )

        await esetup.async_bring_up_server(hass, entry)

        created = [
            c
            for c in esetup.ir.async_create_issue.call_args_list
            if esetup.ISSUE_LEGACY_OAUTH_RESTART in c.args
        ]
        assert created, "legacy-OAuth restart repair was not filed"
        assert created[0].kwargs["is_fixable"] is True
        cleared = {c.args[2] for c in esetup.ir.async_delete_issue.call_args_list}
        assert esetup.ISSUE_LEGACY_OAUTH_RESTART not in cleared
        # The same restart-needed verdict must thread into the connect-URL
        # surfacing so the log carries the first-enable "not live" caveat --
        # deleting that kwarg would silently drop the caveat.
        assert (
            esetup._surface_connect_urls.call_args.kwargs["oauth_restart_pending"]
            is True
        )

    async def test_success_starts_registers_and_surfaces(self, fake_manager):
        hass = _make_hass()
        entry = _make_entry()

        await esetup.async_bring_up_server(hass, entry)

        fake_manager.async_start.assert_awaited_once()
        esetup.async_register_webhook.assert_awaited_once()
        esetup._surface_connect_urls.assert_called_once()
        assert isinstance(hass.data[DOMAIN][DATA_MANAGER], fake_manager)
        esetup.ir.async_create_issue.assert_not_called()
        # Conversation-agent LLM API (#1745): registered with the running
        # server's port + secret path.
        kwargs = esetup.async_register_llm_api.await_args.kwargs
        assert kwargs["port"] == 9584
        assert kwargs["secret_path"] == "/private_x"

    async def test_success_clears_stale_repair_issues(self, fake_manager):
        # Review gap: a successful bring-up must clear EVERY repair-issue id
        # left by a previous failed attempt, or a fixed install keeps showing
        # a stale repair forever.
        hass = _make_hass()
        entry = _make_entry()

        await esetup.async_bring_up_server(hass, entry)

        cleared = {c.args[2] for c in esetup.ir.async_delete_issue.call_args_list}
        assert cleared == {
            esetup.ISSUE_PACKAGE_FAILED,
            esetup.ISSUE_START_FAILED,
            esetup.ISSUE_TOKEN_NEEDED,
            # A non-legacy bring-up (async_register_webhook returned
            # restart_needed=False) also clears any stale legacy-OAuth restart
            # repair from a prior legacy configuration.
            esetup.ISSUE_LEGACY_OAUTH_RESTART,
        }

    async def test_local_only_skips_endpoint_but_keeps_forwarding(
        self, fake_manager, caplog
    ):
        # Owner request: enable_webhook=False must never register the webhook
        # endpoint (Nabu Casa path dead) while the server still starts; the log
        # carries the local-only note. The forwarding config must still be set
        # up (register_endpoint=False) or the sidebar settings panel 503s
        # forever (#1803).
        import logging

        hass = _make_hass()
        entry = _make_entry(options={esetup.OPT_ENABLE_WEBHOOK: False})

        with caplog.at_level(logging.INFO):
            await esetup.async_bring_up_server(hass, entry)

        fake_manager.async_start.assert_awaited_once()
        esetup.async_register_webhook.assert_awaited_once()
        kwargs = esetup.async_register_webhook.await_args.kwargs
        assert kwargs["register_endpoint"] is False
        esetup._surface_connect_urls.assert_called_once()
        assert "local-only" in caplog.text

    async def test_passes_auth_mode_port_and_secret_to_webhook(self, fake_manager):
        hass = _make_hass()
        entry = _make_entry(
            options={OPT_WEBHOOK_AUTH: WEBHOOK_AUTH_HA},
            data={DATA_SECRET_PATH: "/private_secret"},
        )
        await esetup.async_bring_up_server(hass, entry)
        kwargs = esetup.async_register_webhook.await_args.kwargs
        assert kwargs["auth_mode"] == WEBHOOK_AUTH_HA
        assert kwargs["port"] == 9584
        assert kwargs["secret_path"] == "/private_secret"
        assert kwargs["register_endpoint"] is True

    async def test_llm_api_option_off_skips_registration(self, fake_manager, caplog):
        # The Conversation-agent LLM API toggle (#1745, default on): turning
        # it off must skip the registration while the server itself, the
        # webhook, and the rest of the bring-up run unchanged.
        import logging

        hass = _make_hass()
        entry = _make_entry(options={esetup.OPT_ENABLE_LLM_API: False})

        with caplog.at_level(logging.INFO):
            await esetup.async_bring_up_server(hass, entry)

        fake_manager.async_start.assert_awaited_once()
        esetup.async_register_webhook.assert_awaited_once()
        esetup.async_register_llm_api.assert_not_awaited()
        assert "LLM API disabled by option" in caplog.text

    async def test_package_failure_files_package_issue_and_skips_webhook(
        self, fake_manager
    ):
        hass = _make_hass()
        entry = _make_entry()
        fake_manager.async_start.side_effect = esetup.EmbeddedServerError(
            "pip failed", kind="package"
        )

        await esetup.async_bring_up_server(hass, entry)

        fake_manager.async_stop.assert_awaited_once()  # teardown ran
        assert DATA_MANAGER not in hass.data.get(DOMAIN, {})
        esetup.async_register_webhook.assert_not_awaited()
        esetup.async_register_llm_api.assert_not_awaited()
        # The failure kind selects the package-install repair issue, whose
        # fix flow reinstalls the server for this entry.
        call = esetup.ir.async_create_issue.call_args
        assert call.args[2] == ISSUE_PACKAGE_FAILED
        assert call.kwargs["is_fixable"] is True
        assert call.kwargs["data"] == {
            "entry_id": entry.entry_id,
            "detail": "pip failed",
        }

    async def test_start_failure_files_start_issue(self, fake_manager):
        hass = _make_hass()
        entry = _make_entry()
        fake_manager.async_start.side_effect = esetup.EmbeddedServerError(
            "bind failed", kind="start"
        )

        await esetup.async_bring_up_server(hass, entry)
        assert esetup.ir.async_create_issue.call_args.args[2] == ISSUE_START_FAILED

    async def test_a_missing_credential_asks_for_a_token_in_repairs(self, fake_manager):
        hass = _make_hass()
        entry = _make_entry()
        fake_manager.async_start.side_effect = esetup.EmbeddedServerError(
            "invalid_token", kind="token"
        )

        await esetup.async_bring_up_server(hass, entry)

        call = esetup.ir.async_create_issue.call_args
        assert call.args[2] == esetup.ISSUE_TOKEN_NEEDED
        # The repair's form replaces the token on this entry, and says why the
        # stored one stopped working.
        assert call.kwargs["is_fixable"] is True
        assert call.kwargs["data"] == {
            "entry_id": entry.entry_id,
            "reason": "invalid_token",
        }

    async def test_unexpected_error_files_start_issue(self, fake_manager):
        hass = _make_hass()
        entry = _make_entry()
        # Server started, but webhook registration raised a non-EmbeddedServerError.
        esetup.async_register_webhook.side_effect = RuntimeError("register boom")

        await esetup.async_bring_up_server(hass, entry)

        fake_manager.async_stop.assert_awaited_once()
        assert esetup.ir.async_create_issue.call_args.args[2] == ISSUE_START_FAILED

    async def test_cancelled_tears_down_and_reraises(self, fake_manager):
        hass = _make_hass()
        entry = _make_entry()
        fake_manager.async_start.side_effect = asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await esetup.async_bring_up_server(hass, entry)

        fake_manager.async_stop.assert_awaited_once()  # partial state torn down
        esetup.ir.async_create_issue.assert_not_called()  # cancellation isn't a fault


class TestTeardown:
    async def test_unregisters_and_stops_without_revoking(self, fake_manager):
        hass = _make_hass()
        entry = _make_entry()
        await esetup.async_bring_up_server(hass, entry)
        fake_manager.async_stop.reset_mock()

        await esetup.async_teardown_server(hass)

        esetup.async_unregister_webhook.assert_awaited()
        esetup.async_unregister_llm_api.assert_called()
        fake_manager.async_stop.assert_awaited_once()
        assert DATA_MANAGER not in hass.data.get(DOMAIN, {})
        # A reload must keep the provisioned token.
        fake_manager.async_revoke_credentials.assert_not_awaited()

    async def test_teardown_is_noop_when_not_running(self, fake_manager):
        hass = _make_hass()
        await esetup.async_teardown_server(hass)  # must not raise
        esetup.async_unregister_webhook.assert_awaited_once()


class TestRevokeOnRemove:
    async def test_revokes_credentials_and_clears_issues(self, fake_manager):
        hass = _make_hass()
        entry = _make_entry()
        await esetup.async_revoke_credentials_on_remove(hass, entry)
        fake_manager.async_revoke_credentials.assert_awaited_once()
        esetup.ir.async_delete_issue.assert_called()

    async def test_clears_legacy_oauth_restart_repair(self, fake_manager):
        # The legacy-OAuth restart repair is filed only from bring-up, which
        # never runs again for a removed entry — so removal must clear it too,
        # or a still-pending restart leaves a dangling warning for a gone server.
        hass = _make_hass()
        entry = _make_entry()
        await esetup.async_revoke_credentials_on_remove(hass, entry)
        cleared = {c.args[2] for c in esetup.ir.async_delete_issue.call_args_list}
        assert esetup.ISSUE_LEGACY_OAUTH_RESTART in cleared


# ---------------------------------------------------------------------------
# Connect-URL surfacing (network + cloud lazily imported)
# ---------------------------------------------------------------------------


def _install_network_cloud(*, cloud_url=None, local_url=None):
    """Install fake homeassistant.helpers.network + components.cloud modules.

    ``cloud_url``/``local_url`` None ⇒ the corresponding lookup raises its
    "unavailable" exception (the branch the code guards for).
    """

    class NoURLAvailableError(Exception):
        pass

    class CloudNotAvailable(Exception):
        pass

    net = ModuleType("homeassistant.helpers.network")
    net.NoURLAvailableError = NoURLAvailableError

    def get_url(hass, *, allow_external=False, prefer_external=False):
        if local_url is None:
            raise NoURLAvailableError
        return local_url

    net.get_url = get_url

    cloud = ModuleType("homeassistant.components.cloud")
    cloud.CloudNotAvailable = CloudNotAvailable

    def async_remote_ui_url(hass):
        if cloud_url is None:
            raise CloudNotAvailable
        return cloud_url

    cloud.async_remote_ui_url = async_remote_ui_url

    sys.modules["homeassistant.helpers.network"] = net
    sys.modules["homeassistant.components.cloud"] = cloud


def _install_adapters(adapters=None, *, error=None):
    """Install a fake ``homeassistant.components.network.async_get_adapters``.

    ``error`` set ⇒ the lookup raises it (the degrade-to-empty branch). The
    parent-package attribute is set too: ``homeassistant.components`` is a
    MagicMock here, so ``from homeassistant.components import network`` reads the
    child attribute rather than the ``sys.modules`` entry alone.
    """

    net = ModuleType("homeassistant.components.network")

    async def async_get_adapters(hass):
        if error is not None:
            raise error
        return adapters or []

    net.async_get_adapters = async_get_adapters
    sys.modules["homeassistant.components.network"] = net
    sys.modules["homeassistant.components"].network = net
    return net


class TestSurfaceConnectUrls:
    @pytest.fixture(autouse=True)
    def _restore_surface(self, monkeypatch, _spy):
        # Depend on the module spy so this runs AFTER it, then restore the REAL
        # _surface_connect_urls and spy only the persistent-notification call.
        monkeypatch.setattr(esetup, "_surface_connect_urls", _REAL_SURFACE_CONNECT_URLS)
        self.notif = MagicMock()
        monkeypatch.setattr(esetup.persistent_notification, "async_create", self.notif)
        yield

    def _message(self) -> str:
        return (
            self.notif.call_args.kwargs.get("message") or self.notif.call_args.args[1]
        )

    @staticmethod
    def _urls(hass, entry, **kwargs) -> list[str]:
        # The Configure screen's source of truth for the connect URLs.
        return esetup.build_connect_urls(hass, entry, **kwargs)

    def test_neither_log_nor_notification_carries_urls_or_secrets(self, caplog):
        # #2427 / HACS review: in the default (none) mode the connect URL IS
        # the credential, and the log reaches more than the administrator
        # (the server's own log tools, pasted bug reports). Both surfaces
        # point at the admin-only Configure screen instead.
        import logging

        _install_network_cloud(
            cloud_url="https://abc.ui.nabu.casa", local_url="http://192.168.1.5:8123"
        )
        hass = _make_hass()
        entry = _make_entry(
            data={DATA_WEBHOOK_ID: "mcp_id", DATA_SECRET_PATH: "/private_x"},
            options={esetup.OPT_BIND_HOST: esetup.BIND_HOST_ALL},
        )
        with caplog.at_level(logging.INFO):
            esetup._surface_connect_urls(hass, entry, "none")
        self.notif.assert_called_once()
        message = self._message()
        for surface in (caplog.text, message):
            assert "mcp_id" not in surface
            assert "/private_x" not in surface
            assert "/api/webhook/" not in surface
            assert "Configure" in surface
        assert "HA-MCP in-process server is running" in caplog.text
        assert "[HA-MCP settings panel](/ha-mcp)" in message
        # The URLs still resolve for the Configure screen.
        urls = self._urls(hass, entry)
        assert "https://abc.ui.nabu.casa/api/webhook/mcp_id" in urls
        assert "http://192.168.1.5:8123/api/webhook/mcp_id" in urls

    def test_configure_urls_cover_every_interface(self):
        # #1862: one webhook and one direct-access URL per LAN interface.
        _install_network_cloud(cloud_url=None, local_url="http://10.0.2.3:8123")
        hass = _make_hass()
        entry = _make_entry(
            data={DATA_WEBHOOK_ID: "mcp_id", DATA_SECRET_PATH: "/private_x"},
            options={esetup.OPT_BIND_HOST: esetup.BIND_HOST_ALL},
        )
        urls = self._urls(hass, entry, extra_hosts=["10.0.2.3", "10.0.1.3"])
        assert "http://10.0.2.3:8123/api/webhook/mcp_id" in urls
        assert "http://10.0.1.3:8123/api/webhook/mcp_id" in urls
        assert "http://10.0.1.3:9584/private_x (direct access)" in urls

    def test_external_url_option_leads_the_list(self):
        # Owner request (webhook-proxy app parity): a configured external URL
        # is shown FIRST, ahead of Nabu Casa and the local address.
        _install_network_cloud(
            cloud_url="https://abc.ui.nabu.casa", local_url="http://192.168.1.5:8123"
        )
        hass = _make_hass()
        entry = _make_entry(
            data={DATA_WEBHOOK_ID: "mcp_id", DATA_SECRET_PATH: "/p"},
            options={esetup.OPT_EXTERNAL_URL: "https://ha.example.com/"},
        )
        urls = self._urls(hass, entry)
        assert urls[0] == "https://ha.example.com/api/webhook/mcp_id"
        assert "https://abc.ui.nabu.casa/api/webhook/mcp_id" in urls

    def test_notification_links_panel_and_carries_title(self):
        # The rename commit's discoverability contract: the running
        # notification links the sidebar settings panel and carries the
        # HA-MCP Server title (the only path from "it is running" to the UI).
        _install_network_cloud(cloud_url=None, local_url="http://192.168.1.5:8123")
        hass = _make_hass()
        entry = _make_entry(data={DATA_WEBHOOK_ID: "mcp_id", DATA_SECRET_PATH: "/p"})
        esetup._surface_connect_urls(hass, entry, "none")
        assert "[HA-MCP settings panel](/ha-mcp)" in self._message()
        assert self.notif.call_args.kwargs.get("title") == "HA-MCP Server"

    def test_falls_back_to_relative_url_when_none_available(self):
        _install_network_cloud(cloud_url=None, local_url=None)
        hass = _make_hass()
        entry = _make_entry(data={DATA_WEBHOOK_ID: "mcp_id", DATA_SECRET_PATH: "/p"})
        urls = self._urls(hass, entry)
        assert any("/api/webhook/mcp_id" in url for url in urls)

    def test_lan_bind_lists_direct_access_with_configured_port(self):
        _install_network_cloud(cloud_url=None, local_url="http://192.168.1.5:8123")
        hass = _make_hass()
        entry = _make_entry(
            data={DATA_WEBHOOK_ID: "mcp_id", DATA_SECRET_PATH: "/priv"},
            options={esetup.OPT_BIND_HOST: "0.0.0.0", esetup.OPT_SERVER_PORT: 9999},
        )
        assert "http://192.168.1.5:9999/priv (direct access)" in self._urls(hass, entry)

    def test_default_bind_lists_direct_access_line(self):
        # An entry that never saved bind_host inherits the LAN default, so
        # the Configure screen lists the direct URL.
        _install_network_cloud(cloud_url=None, local_url="http://192.168.1.5:8123")
        hass = _make_hass()
        entry = _make_entry(data={DATA_WEBHOOK_ID: "mcp_id", DATA_SECRET_PATH: "/priv"})
        assert "http://192.168.1.5:9584/priv (direct access)" in self._urls(hass, entry)

    def test_loopback_bind_omits_direct_access_line(self):
        _install_network_cloud(cloud_url=None, local_url="http://192.168.1.5:8123")
        hass = _make_hass()
        entry = _make_entry(
            data={DATA_WEBHOOK_ID: "mcp_id", DATA_SECRET_PATH: "/priv"},
            options={esetup.OPT_BIND_HOST: "127.0.0.1"},
        )
        assert not any("(direct access)" in url for url in self._urls(hass, entry))

    def test_local_only_lists_no_webhook_urls(self):
        _install_network_cloud(
            cloud_url="https://abc.ui.nabu.casa", local_url="http://192.168.1.5:8123"
        )
        hass = _make_hass()
        entry = _make_entry(
            data={DATA_WEBHOOK_ID: "mcp_id", DATA_SECRET_PATH: "/priv"},
            options={esetup.OPT_EXTERNAL_URL: "https://ha.example.com"},
        )
        urls = self._urls(hass, entry, webhook_enabled=False)
        assert not any("/api/webhook/" in url for url in urls)
        assert "http://192.168.1.5:9584/priv (direct access)" in urls
        esetup._surface_connect_urls(hass, entry, "none", webhook_enabled=False)
        assert "disabled" in self._message()

    def test_legacy_creds_never_reach_log_or_notification(self, caplog):
        # #2427 / HACS review: the legacy client secret used to be logged in
        # cleartext. The Configure screen shows it; the log says where.
        import logging

        _install_network_cloud(cloud_url=None, local_url="http://192.168.1.5:8123")
        hass = _make_hass()
        entry = _make_entry(
            data={
                DATA_WEBHOOK_ID: "mcp_id",
                DATA_SECRET_PATH: "/p",
                esetup.DATA_OAUTH_CLIENT_ID: "cid-abc123",
                esetup.DATA_OAUTH_CLIENT_SECRET: "sec-xyz789",
            }
        )
        with caplog.at_level(logging.INFO):
            esetup._surface_connect_urls(hass, entry, esetup.WEBHOOK_AUTH_LEGACY)
        for surface in (caplog.text, self._message()):
            assert "cid-abc123" not in surface
            assert "sec-xyz789" not in surface
        assert "Client Secret" in caplog.text
        assert "Configure" in caplog.text
        # Live views (no pending restart): no not-live caveat.
        assert "not live until the restart" not in caplog.text

    def test_legacy_first_enable_logs_not_live_caveat(self, caplog):
        # Review finding on #1880: first-enable mid-session late-binds the
        # views, so /authorize is not live until the restart the repair asks
        # for -- the log must say so, matching the options hint.
        import logging

        _install_network_cloud(cloud_url=None, local_url="http://192.168.1.5:8123")
        hass = _make_hass()
        entry = _make_entry(data={DATA_WEBHOOK_ID: "mcp_id", DATA_SECRET_PATH: "/p"})
        with caplog.at_level(logging.INFO):
            esetup._surface_connect_urls(
                hass,
                entry,
                esetup.WEBHOOK_AUTH_LEGACY,
                oauth_creds_active=True,
                oauth_restart_pending=True,
            )
        assert "not live until the restart" in caplog.text

    def test_legacy_pending_rotation_says_previous_creds_stay_active(self, caplog):
        import logging

        _install_network_cloud(cloud_url=None, local_url="http://192.168.1.5:8123")
        hass = _make_hass()
        entry = _make_entry(data={DATA_WEBHOOK_ID: "mcp_id", DATA_SECRET_PATH: "/p"})
        with caplog.at_level(logging.INFO):
            esetup._surface_connect_urls(
                hass, entry, esetup.WEBHOOK_AUTH_LEGACY, oauth_creds_active=False
            )
        assert "previous credentials remain active" in caplog.text
        assert "Configure" in caplog.text

    def test_cloud_import_error_falls_back_to_local_url(self, monkeypatch):
        # Review gap: plain HA Core has no cloud integration at all - the
        # ImportError branch must degrade to the local URL, not raise.
        import builtins

        real_import = builtins.__import__

        def _no_cloud(name, *a, **k):
            if name.startswith("homeassistant.components.cloud"):
                raise ImportError(name)
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", _no_cloud)
        _install_network_cloud(cloud_url=None, local_url="http://192.168.1.5:8123")
        hass = _make_hass()
        entry = _make_entry(data={DATA_WEBHOOK_ID: "mcp_id", DATA_SECRET_PATH: "/p"})
        assert "http://192.168.1.5:8123/api/webhook/mcp_id" in self._urls(hass, entry)

    def test_default_options_create_notification_with_panel_line(
        self, monkeypatch, caplog
    ):
        # Baseline for the two UX toggles: with neither option stored, the
        # start-up notification is created (async_create) and its message links
        # the sidebar settings panel; nothing is dismissed.
        import logging

        dismiss = MagicMock()
        monkeypatch.setattr(esetup.persistent_notification, "async_dismiss", dismiss)
        _install_network_cloud(cloud_url=None, local_url="http://192.168.1.5:8123")
        hass = _make_hass()
        entry = _make_entry(data={DATA_WEBHOOK_ID: "mcp_id", DATA_SECRET_PATH: "/priv"})
        with caplog.at_level(logging.INFO):
            esetup._surface_connect_urls(hass, entry, "none")
        self.notif.assert_called_once()
        assert "[HA-MCP settings panel](/ha-mcp)" in self._message()
        dismiss.assert_not_called()

    def test_startup_notification_ends_with_disable_instructions(self):
        _install_network_cloud(cloud_url=None, local_url="http://192.168.1.5:8123")
        hass = _make_hass()
        entry = _make_entry(data={DATA_WEBHOOK_ID: "mcp_id", DATA_SECRET_PATH: "/priv"})

        esetup._surface_connect_urls(hass, entry, "none")

        assert self._message().endswith(
            "\n\n"
            "To disable this notification, uncheck the startup notification box "
            "on that same configuration screen.\n"
        )

    def test_startup_notification_off_dismisses_and_skips_create(
        self, monkeypatch, caplog
    ):
        # enable_startup_notification=False: no persistent notification is
        # created; instead any stale one is dismissed by its id. The connect
        # URLs still reach the admin-only INFO log unchanged.
        import logging

        dismiss = MagicMock()
        monkeypatch.setattr(esetup.persistent_notification, "async_dismiss", dismiss)
        _install_network_cloud(cloud_url=None, local_url="http://192.168.1.5:8123")
        hass = _make_hass()
        entry = _make_entry(
            data={DATA_WEBHOOK_ID: "mcp_id", DATA_SECRET_PATH: "/priv"},
            options={esetup.OPT_ENABLE_STARTUP_NOTIFICATION: False},
        )
        with caplog.at_level(logging.INFO):
            esetup._surface_connect_urls(hass, entry, "none")
        # No notification created.
        self.notif.assert_not_called()
        # The stale one is dismissed by the connect notification's id.
        dismiss.assert_called_once()
        dismissed_id = dismiss.call_args.kwargs.get("notification_id") or (
            dismiss.call_args.args[1] if len(dismiss.call_args.args) > 1 else None
        )
        assert dismissed_id == esetup._NOTIFICATION_ID == "ha_mcp_tools_server_connect"
        assert dismiss.call_args.args[0] is hass
        # The INFO running line still happens.
        assert "HA-MCP in-process server is running" in caplog.text

    def test_sidebar_panel_off_omits_panel_line_from_notification(
        self, monkeypatch, caplog
    ):
        # enable_sidebar_panel=False (start-up notification still on): the
        # notification is created, but its message drops the sidebar panel line
        # (there is no panel to link to). The rest of the notification stays.
        import logging

        dismiss = MagicMock()
        monkeypatch.setattr(esetup.persistent_notification, "async_dismiss", dismiss)
        _install_network_cloud(cloud_url=None, local_url="http://192.168.1.5:8123")
        hass = _make_hass()
        entry = _make_entry(
            data={DATA_WEBHOOK_ID: "mcp_id", DATA_SECRET_PATH: "/priv"},
            options={esetup.OPT_ENABLE_SIDEBAR_PANEL: False},
        )
        with caplog.at_level(logging.INFO):
            esetup._surface_connect_urls(hass, entry, "none")
        self.notif.assert_called_once()
        dismiss.assert_not_called()
        message = self._message()
        assert "[HA-MCP settings panel](/ha-mcp)" not in message
        assert "(/ha-mcp)" not in message
        # Still a real notification: the admin-only Configure pointer remains.
        assert "Configure" in message
        assert self.notif.call_args.kwargs.get("title") == "HA-MCP Server"


class TestBuildConnectUrls:
    """Direct coverage of ``build_connect_urls`` — the shared URL resolver that
    ``_surface_connect_urls`` (log/notification) and the config flow's Configure
    hint both call. Exercised here without the surfacing layer so the resolution
    decisions (host, secret-path guard, webhook-disabled) are asserted directly.
    """

    def test_direct_access_line_carries_resolved_host(self):
        # 0.0.0.0 bind: the direct-access URL must name the ACTUAL resolved host
        # (from get_url), not a placeholder, so an admin can paste it verbatim.
        _install_network_cloud(cloud_url=None, local_url="http://192.168.1.5:8123")
        hass = _make_hass()
        entry = _make_entry(
            data={DATA_WEBHOOK_ID: "mcp_id", DATA_SECRET_PATH: "/private_x"},
            options={esetup.OPT_BIND_HOST: esetup.BIND_HOST_ALL},
        )
        urls = esetup.build_connect_urls(hass, entry)
        direct = [u for u in urls if "(direct access)" in u]
        assert direct == ["http://192.168.1.5:9584/private_x (direct access)"]

    def test_missing_secret_path_omits_direct_access_line(self):
        # Guard added in this PR: a URL must never render without its secret
        # segment, so a missing secret path drops the direct-access line entirely
        # rather than emitting a credential-less (and therefore useless) URL.
        _install_network_cloud(cloud_url=None, local_url="http://192.168.1.5:8123")
        hass = _make_hass()
        entry = _make_entry(
            data={DATA_WEBHOOK_ID: "mcp_id"},  # no DATA_SECRET_PATH
            options={esetup.OPT_BIND_HOST: esetup.BIND_HOST_ALL},
        )
        urls = esetup.build_connect_urls(hass, entry)
        assert not any("(direct access)" in u for u in urls)

    def test_webhook_disabled_returns_no_webhook_urls(self):
        # Local-only mode: the webhook is never registered, so no /api/webhook/
        # URL may be surfaced — the external, Nabu Casa, and local webhook forms
        # are all suppressed even though every source is otherwise available.
        _install_network_cloud(
            cloud_url="https://abc.ui.nabu.casa", local_url="http://192.168.1.5:8123"
        )
        hass = _make_hass()
        entry = _make_entry(
            data={DATA_WEBHOOK_ID: "mcp_id", DATA_SECRET_PATH: "/private_x"},
            options={esetup.OPT_EXTERNAL_URL: "https://ha.example.com"},
        )
        urls = esetup.build_connect_urls(hass, entry, webhook_enabled=False)
        assert not any("/api/webhook/" in u for u in urls)

    def test_multiple_interfaces_expand_both_lines(self):
        # #1862: a multi-interface / multi-VLAN host surfaces one webhook and one
        # direct-access URL per enabled LAN address, canonical get_url host first.
        _install_network_cloud(cloud_url=None, local_url="http://10.0.2.3:8123")
        hass = _make_hass()
        entry = _make_entry(
            data={DATA_WEBHOOK_ID: "mcp_id", DATA_SECRET_PATH: "/private_x"},
            options={esetup.OPT_BIND_HOST: esetup.BIND_HOST_ALL},
        )
        urls = esetup.build_connect_urls(
            hass, entry, extra_hosts=["10.0.2.3", "10.0.1.3"]
        )
        webhook = [u for u in urls if "/api/webhook/" in u]
        direct = [u for u in urls if "(direct access)" in u]
        assert webhook == [
            "http://10.0.2.3:8123/api/webhook/mcp_id",
            "http://10.0.1.3:8123/api/webhook/mcp_id",
        ]
        assert direct == [
            "http://10.0.2.3:9584/private_x (direct access)",
            "http://10.0.1.3:9584/private_x (direct access)",
        ]

    def test_extra_hosts_deduped_against_get_url_host(self):
        # The get_url host repeated in the adapter list must not double-list.
        _install_network_cloud(cloud_url=None, local_url="http://10.0.2.3:8123")
        hass = _make_hass()
        entry = _make_entry(
            data={DATA_WEBHOOK_ID: "mcp_id", DATA_SECRET_PATH: "/private_x"},
            options={esetup.OPT_BIND_HOST: esetup.BIND_HOST_ALL},
        )
        urls = esetup.build_connect_urls(hass, entry, extra_hosts=["10.0.2.3"])
        assert sum("(direct access)" in u for u in urls) == 1
        assert sum("/api/webhook/" in u for u in urls) == 1

    def test_extra_hosts_surface_direct_line_without_get_url(self):
        # get_url unavailable but adapters known: the direct-access line still
        # lists the real adapter hosts rather than only a placeholder.
        _install_network_cloud(cloud_url=None, local_url=None)
        hass = _make_hass()
        entry = _make_entry(
            data={DATA_WEBHOOK_ID: "mcp_id", DATA_SECRET_PATH: "/private_x"},
            options={esetup.OPT_BIND_HOST: esetup.BIND_HOST_ALL},
        )
        urls = esetup.build_connect_urls(hass, entry, extra_hosts=["10.0.1.3"])
        direct = [u for u in urls if "(direct access)" in u]
        assert direct == ["http://10.0.1.3:9584/private_x (direct access)"]

    def test_portless_internal_url_swaps_host_without_a_port(self):
        # A reverse-proxied internal URL has no port; _swap_url_host must keep it
        # port-less for the extra adapter host rather than inventing one.
        _install_network_cloud(cloud_url=None, local_url="https://ha.internal")
        hass = _make_hass()
        entry = _make_entry(data={DATA_WEBHOOK_ID: "mcp_id", DATA_SECRET_PATH: "/priv"})
        urls = esetup.build_connect_urls(hass, entry, extra_hosts=["10.0.1.3"])
        webhook = [u for u in urls if "/api/webhook/" in u]
        assert webhook == [
            "https://ha.internal/api/webhook/mcp_id",
            "https://10.0.1.3/api/webhook/mcp_id",
        ]

    def test_direct_line_uses_placeholder_when_no_host_resolves(self):
        # bind-all + secret present but no get_url host and no adapters: the
        # direct line falls back to the <home-assistant-ip> placeholder rather
        # than dropping the line or rendering a host-less URL.
        _install_network_cloud(cloud_url=None, local_url=None)
        hass = _make_hass()
        entry = _make_entry(
            data={DATA_WEBHOOK_ID: "mcp_id", DATA_SECRET_PATH: "/priv"},
            options={esetup.OPT_BIND_HOST: esetup.BIND_HOST_ALL},
        )
        urls = esetup.build_connect_urls(hass, entry)
        direct = [u for u in urls if "(direct access)" in u]
        assert direct == ["http://<home-assistant-ip>:9584/priv (direct access)"]


class TestAsyncGetLanHosts:
    """Coverage of ``async_get_lan_hosts`` — the per-interface IPv4 enumeration
    that feeds ``build_connect_urls`` ``extra_hosts`` (#1862)."""

    async def test_lists_enabled_adapter_ipv4_in_order(self):
        _install_adapters(
            [
                {"enabled": True, "ipv4": [{"address": "10.0.2.3"}]},
                {
                    "enabled": True,
                    "ipv4": [{"address": "10.0.1.3"}, {"address": "10.0.1.4"}],
                },
                {"enabled": False, "ipv4": [{"address": "10.9.9.9"}]},
            ]
        )
        hosts = await esetup.async_get_lan_hosts(_make_hass())
        # Disabled adapter dropped; enabled addresses kept in adapter order.
        assert hosts == ["10.0.2.3", "10.0.1.3", "10.0.1.4"]

    async def test_degrades_to_empty_on_error(self):
        # A lookup failure must yield [] so URL surfacing (and bring-up) never
        # breaks for a display-only enumeration.
        _install_adapters(error=RuntimeError("no network component"))
        hosts = await esetup.async_get_lan_hosts(_make_hass())
        assert hosts == []

    async def test_degrades_to_empty_on_malformed_adapter(self):
        # A malformed adapter entry (missing keys) must also degrade to [] rather
        # than escaping the loop into async_bring_up_server's handler, which would
        # tear the running server down for a display-only lookup.
        _install_adapters([{"enabled": True}])  # no "ipv4" key
        hosts = await esetup.async_get_lan_hosts(_make_hass())
        assert hosts == []


# ---------------------------------------------------------------------------
# Component / server version-compatibility repair issue
# ---------------------------------------------------------------------------


def _make_async_hass() -> MagicMock:
    """A hass with an inline executor (from ``_make_hass``) and awaitable reload."""
    hass = _make_hass()
    hass.config_entries.async_reload = AsyncMock()
    return hass


class TestComponentCompat:
    async def test_outdated_component_files_issue(self, monkeypatch):
        hass = _make_async_hass()
        entry = _make_entry()
        monkeypatch.setattr(esetup, "_read_min_component_version", lambda: "0.15.0")
        monkeypatch.setattr(
            esetup,
            "async_get_integration",
            AsyncMock(return_value=SimpleNamespace(version="0.14.0")),
        )

        await esetup._async_check_component_compat(hass, entry)

        esetup.ir.async_create_issue.assert_called_once()
        kwargs = esetup.ir.async_create_issue.call_args.kwargs
        args = esetup.ir.async_create_issue.call_args.args
        assert ISSUE_COMPONENT_OUTDATED in args
        # The learn-more link is the server-updates docs section.
        assert kwargs["learn_more_url"] == SERVER_UPDATES_DOCS_URL
        assert kwargs["translation_placeholders"] == {
            "required": "0.15.0",
            "installed": "0.14.0",
        }
        assert kwargs["severity"] == esetup.ir.IssueSeverity.WARNING
        assert kwargs["is_fixable"] is False
        esetup.ir.async_delete_issue.assert_not_called()

    async def test_satisfied_component_clears_issue(self, monkeypatch):
        hass = _make_async_hass()
        entry = _make_entry()
        monkeypatch.setattr(esetup, "_read_min_component_version", lambda: "0.11.0")
        monkeypatch.setattr(
            esetup,
            "async_get_integration",
            AsyncMock(return_value=SimpleNamespace(version="0.14.0")),
        )

        await esetup._async_check_component_compat(hass, entry)

        esetup.ir.async_create_issue.assert_not_called()
        esetup.ir.async_delete_issue.assert_called_once_with(
            hass, DOMAIN, ISSUE_COMPONENT_OUTDATED
        )

    async def test_missing_min_version_skips(self, monkeypatch):
        # An older/newer server without MIN_COMPONENT_VERSION ⇒ nothing to
        # enforce: neither file nor clear the issue.
        hass = _make_async_hass()
        entry = _make_entry()
        monkeypatch.setattr(esetup, "_read_min_component_version", lambda: None)
        get_integration = AsyncMock()
        monkeypatch.setattr(esetup, "async_get_integration", get_integration)

        await esetup._async_check_component_compat(hass, entry)

        get_integration.assert_not_awaited()
        esetup.ir.async_create_issue.assert_not_called()
        esetup.ir.async_delete_issue.assert_not_called()

    async def test_integration_read_error_is_swallowed(self, monkeypatch):
        # A failure reading the component version must not raise (advisory only).
        hass = _make_async_hass()
        entry = _make_entry()
        monkeypatch.setattr(esetup, "_read_min_component_version", lambda: "0.15.0")
        monkeypatch.setattr(
            esetup,
            "async_get_integration",
            AsyncMock(side_effect=RuntimeError("loader boom")),
        )

        await esetup._async_check_component_compat(hass, entry)  # must not raise

        esetup.ir.async_create_issue.assert_not_called()

    async def test_version_less_manifest_takes_the_unreadable_path(
        self, monkeypatch, caplog
    ):
        # A manifest without a version reads as None rather than raising, so
        # only the explicit guard routes it here. The warning is what
        # discriminates: without the guard the literal "None" reaches
        # AwesomeVersion and the comparison decides the outcome quietly — the
        # silent misreport the guard exists to prevent.
        import logging

        hass = _make_async_hass()
        entry = _make_entry()
        monkeypatch.setattr(esetup, "_read_min_component_version", lambda: "0.15.0")
        monkeypatch.setattr(
            esetup,
            "async_get_integration",
            AsyncMock(return_value=SimpleNamespace(version=None)),
        )

        with caplog.at_level(logging.WARNING):
            await esetup._async_check_component_compat(hass, entry)

        assert "Could not read the HA-MCP component version" in caplog.text
        esetup.ir.async_create_issue.assert_not_called()

    def test_read_min_component_version_skips_when_server_absent(self, monkeypatch):
        # Simulate the server package being uninstalled regardless of the test
        # environment (CI installs the real ha_mcp; the local stub tier does
        # not). The None entry MUST be the full dotted module name: Python
        # resolves ``from a.b.c import x`` through the immediate parent
        # ``a.b``, so a ``sys.modules["a"] = None`` is short-circuited whenever
        # the submodule chain is already imported — and accidentally importing
        # the real ha_mcp here poisons its in-process settings caches for
        # unrelated tests on the same xdist worker.
        monkeypatch.setitem(sys.modules, "ha_mcp.tools.tools_filesystem", None)
        assert esetup._read_min_component_version() is None
