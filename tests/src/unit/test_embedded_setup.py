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
from types import ModuleType
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
    ISSUE_PACKAGE_FAILED,
    ISSUE_START_FAILED,
    OPT_WEBHOOK_AUTH,
    WEBHOOK_AUTH_HA,
)


def _make_hass() -> MagicMock:
    hass = MagicMock(name="hass")
    hass.data = {}

    def _update_entry(entry, *, data=None, **_kw):
        if data is not None:
            entry.data = data

    hass.config_entries.async_update_entry = MagicMock(side_effect=_update_entry)
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
    monkeypatch.setattr(esetup, "async_register_webhook", AsyncMock())
    monkeypatch.setattr(esetup, "async_unregister_webhook", AsyncMock())
    monkeypatch.setattr(esetup.ir, "async_create_issue", MagicMock())
    monkeypatch.setattr(esetup.ir, "async_delete_issue", MagicMock())
    monkeypatch.setattr(esetup, "_surface_connect_urls", MagicMock())


class TestBringUp:
    async def test_success_starts_registers_and_surfaces(self, fake_manager):
        hass = _make_hass()
        entry = _make_entry()

        await esetup.async_bring_up_server(hass, entry)

        fake_manager.async_start.assert_awaited_once()
        esetup.async_register_webhook.assert_awaited_once()
        esetup._surface_connect_urls.assert_called_once()
        assert isinstance(hass.data[DOMAIN][DATA_MANAGER], fake_manager)
        esetup.ir.async_create_issue.assert_not_called()

    async def test_success_clears_stale_repair_issues(self, fake_manager):
        # Review gap: a successful bring-up must clear BOTH repair-issue ids
        # left by a previous failed attempt, or a fixed install keeps showing
        # a stale repair forever.
        hass = _make_hass()
        entry = _make_entry()

        await esetup.async_bring_up_server(hass, entry)

        cleared = {c.args[2] for c in esetup.ir.async_delete_issue.call_args_list}
        assert cleared == {esetup.ISSUE_PACKAGE_FAILED, esetup.ISSUE_START_FAILED}

    async def test_local_only_skips_webhook_registration(self, fake_manager, caplog):
        # Owner request: enable_webhook=False must never register the webhook
        # (Nabu Casa path dead) while the server still starts; the log carries
        # the local-only note.
        import logging

        hass = _make_hass()
        entry = _make_entry(options={esetup.OPT_ENABLE_WEBHOOK: False})

        with caplog.at_level(logging.INFO):
            await esetup.async_bring_up_server(hass, entry)

        fake_manager.async_start.assert_awaited_once()
        esetup.async_register_webhook.assert_not_awaited()
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
        # The failure kind selects the package-install repair issue.
        assert esetup.ir.async_create_issue.call_args.args[2] == ISSUE_PACKAGE_FAILED

    async def test_start_failure_files_start_issue(self, fake_manager):
        hass = _make_hass()
        entry = _make_entry()
        fake_manager.async_start.side_effect = esetup.EmbeddedServerError(
            "bind failed", kind="start"
        )

        await esetup.async_bring_up_server(hass, entry)
        assert esetup.ir.async_create_issue.call_args.args[2] == ISSUE_START_FAILED

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

    def test_notification_carries_no_secrets_urls_go_to_log(self, caplog):
        # Review finding (Patch76): persistent notifications are visible to
        # every authenticated user, so the message must carry NO connect URL
        # or secret path - those go to the admin-only log; the notification
        # points at the admin-only surfaces.
        import logging

        _install_network_cloud(
            cloud_url="https://abc.ui.nabu.casa", local_url="http://192.168.1.5:8123"
        )
        hass = _make_hass()
        entry = _make_entry(data={DATA_WEBHOOK_ID: "mcp_id", DATA_SECRET_PATH: "/p"})
        with caplog.at_level(logging.INFO):
            esetup._surface_connect_urls(hass, entry, "none")
        self.notif.assert_called_once()
        message = self._message()
        assert "mcp_id" not in message
        assert "/p " not in message
        assert "[HA-MCP settings panel](/ha-mcp)" in message
        assert "Configure" in message
        assert "https://abc.ui.nabu.casa/api/webhook/mcp_id" in caplog.text
        assert "http://192.168.1.5:8123/api/webhook/mcp_id" in caplog.text

    def test_external_url_option_leads_the_list(self, caplog):
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
        import logging

        with caplog.at_level(logging.INFO):
            esetup._surface_connect_urls(hass, entry, "none")
        first = next(
            line for line in caplog.text.splitlines() if "/api/webhook/" in line
        )
        assert "https://ha.example.com/api/webhook/mcp_id" in first
        assert "https://abc.ui.nabu.casa/api/webhook/mcp_id" in caplog.text
        # The rename commit's discoverability contract: the running
        # notification links the sidebar settings panel and carries the
        # HA-MCP Server title (the only path from "it is running" to the UI).
        assert "[HA-MCP settings panel](/ha-mcp)" in self._message()
        assert self.notif.call_args.kwargs.get("title") == "HA-MCP Server"

    def test_falls_back_to_relative_url_when_none_available(self, caplog):
        import logging

        _install_network_cloud(cloud_url=None, local_url=None)
        hass = _make_hass()
        entry = _make_entry(data={DATA_WEBHOOK_ID: "mcp_id", DATA_SECRET_PATH: "/p"})
        with caplog.at_level(logging.INFO):
            esetup._surface_connect_urls(hass, entry, "ha_auth")
        self.notif.assert_called_once()
        assert "/api/webhook/mcp_id" in caplog.text
        assert "mcp_id" not in self._message()

    def test_lan_bind_logs_direct_access_with_configured_port(self, caplog):
        # Explicit 0.0.0.0 + custom port: the direct URL (with that port)
        # appears in the admin-only log.
        import logging

        _install_network_cloud(cloud_url=None, local_url="http://192.168.1.5:8123")
        hass = _make_hass()
        entry = _make_entry(
            data={DATA_WEBHOOK_ID: "mcp_id", DATA_SECRET_PATH: "/priv"},
            options={esetup.OPT_BIND_HOST: "0.0.0.0", esetup.OPT_SERVER_PORT: 9999},
        )
        with caplog.at_level(logging.INFO):
            esetup._surface_connect_urls(hass, entry, "none")
        # Strengthened: the direct line names the resolved host, not just the port.
        assert "http://192.168.1.5:9999/priv (direct access)" in caplog.text

    def test_default_bind_logs_direct_access_line(self, caplog):
        # LAN default (add-on parity): no explicit bind option -> the direct
        # URL is part of the admin-only LOG output (never the notification).
        import logging

        _install_network_cloud(cloud_url=None, local_url="http://192.168.1.5:8123")
        hass = _make_hass()
        entry = _make_entry(data={DATA_WEBHOOK_ID: "mcp_id", DATA_SECRET_PATH: "/priv"})
        with caplog.at_level(logging.INFO):
            esetup._surface_connect_urls(hass, entry, "none")
        # Strengthened: the resolved host rides the default-port direct line.
        assert "http://192.168.1.5:9584/priv (direct access)" in caplog.text
        assert "/priv" not in self._message()

    def test_loopback_bind_omits_direct_access_line(self, caplog):
        import logging

        _install_network_cloud(cloud_url=None, local_url="http://192.168.1.5:8123")
        hass = _make_hass()
        entry = _make_entry(
            data={DATA_WEBHOOK_ID: "mcp_id", DATA_SECRET_PATH: "/priv"},
            options={esetup.OPT_BIND_HOST: "127.0.0.1"},
        )
        with caplog.at_level(logging.INFO):
            esetup._surface_connect_urls(hass, entry, "none")
        assert "(direct access)" not in caplog.text

    def test_local_only_surface_has_no_webhook_urls(self, caplog):
        import logging

        _install_network_cloud(
            cloud_url="https://abc.ui.nabu.casa", local_url="http://192.168.1.5:8123"
        )
        hass = _make_hass()
        entry = _make_entry(
            data={DATA_WEBHOOK_ID: "mcp_id", DATA_SECRET_PATH: "/priv"},
            options={esetup.OPT_EXTERNAL_URL: "https://ha.example.com"},
        )
        with caplog.at_level(logging.INFO):
            esetup._surface_connect_urls(hass, entry, "none", webhook_enabled=False)
        assert "/api/webhook/" not in caplog.text
        # Strengthened: even in local-only mode the direct line names the host.
        assert "http://192.168.1.5:9584/priv (direct access)" in caplog.text
        assert "disabled" in self._message()

    def test_cloud_import_error_falls_back_to_local_url(self, monkeypatch, caplog):
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
        import logging

        with caplog.at_level(logging.INFO):
            esetup._surface_connect_urls(hass, entry, "none")
        assert "http://192.168.1.5:8123/api/webhook/mcp_id" in caplog.text


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
