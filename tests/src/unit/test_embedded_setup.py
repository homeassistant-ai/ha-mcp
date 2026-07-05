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

    def test_includes_cloud_and_local_urls(self):
        _install_network_cloud(
            cloud_url="https://abc.ui.nabu.casa", local_url="http://192.168.1.5:8123"
        )
        hass = _make_hass()
        entry = _make_entry(data={DATA_WEBHOOK_ID: "mcp_id", DATA_SECRET_PATH: "/p"})
        esetup._surface_connect_urls(hass, entry, "none")
        self.notif.assert_called_once()
        message = self._message()
        assert "https://abc.ui.nabu.casa/api/webhook/mcp_id" in message
        assert "http://192.168.1.5:8123/api/webhook/mcp_id" in message
        # The rename commit's discoverability contract: the running
        # notification links the sidebar settings panel and carries the
        # HA-MCP Server title (the only path from "it is running" to the UI).
        assert "[HA-MCP settings panel](/ha-mcp)" in message
        assert self.notif.call_args.kwargs.get("title") == "HA-MCP Server"

    def test_falls_back_to_relative_url_when_none_available(self):
        _install_network_cloud(cloud_url=None, local_url=None)
        hass = _make_hass()
        entry = _make_entry(data={DATA_WEBHOOK_ID: "mcp_id", DATA_SECRET_PATH: "/p"})
        esetup._surface_connect_urls(hass, entry, "ha_auth")
        self.notif.assert_called_once()
        assert "/api/webhook/mcp_id" in self._message()

    def test_lan_bind_surfaces_direct_access_line(self):
        # bind_host=0.0.0.0 is the user-visible signal that the server is
        # exposed beyond loopback — the notification must say so, with the
        # direct URL (review finding: branch previously uncovered).
        _install_network_cloud(cloud_url=None, local_url="http://192.168.1.5:8123")
        hass = _make_hass()
        entry = _make_entry(
            data={DATA_WEBHOOK_ID: "mcp_id", DATA_SECRET_PATH: "/priv"},
            options={esetup.OPT_BIND_HOST: "0.0.0.0", esetup.OPT_SERVER_PORT: 9999},
        )
        esetup._surface_connect_urls(hass, entry, "none")
        message = self._message()
        assert "Direct LAN access" in message
        assert ":9999/priv" in message

    def test_default_bind_includes_direct_access_line(self):
        # LAN default (add-on parity): no explicit bind option -> the direct
        # URL is part of the standard connect notification.
        _install_network_cloud(cloud_url=None, local_url="http://192.168.1.5:8123")
        hass = _make_hass()
        entry = _make_entry(data={DATA_WEBHOOK_ID: "mcp_id", DATA_SECRET_PATH: "/priv"})
        esetup._surface_connect_urls(hass, entry, "none")
        assert "Direct LAN access" in self._message()

    def test_loopback_bind_omits_direct_access_line(self):
        _install_network_cloud(cloud_url=None, local_url="http://192.168.1.5:8123")
        hass = _make_hass()
        entry = _make_entry(
            data={DATA_WEBHOOK_ID: "mcp_id", DATA_SECRET_PATH: "/priv"},
            options={esetup.OPT_BIND_HOST: "127.0.0.1"},
        )
        esetup._surface_connect_urls(hass, entry, "none")
        assert "Direct LAN access" not in self._message()
