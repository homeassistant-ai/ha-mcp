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
    DATA_BRINGUP_TASK,
    DATA_MANAGER,
    DATA_PENDING_UPDATE_NOTIFY,
    DATA_SECRET_PATH,
    DATA_UPDATE_COORDINATOR,
    DATA_WEBHOOK_ID,
    DEFAULT_PIP_SPEC,
    DIST_NAME_DEV,
    DIST_NAME_STABLE,
    DOMAIN,
    ISSUE_COMPONENT_OUTDATED,
    ISSUE_PACKAGE_FAILED,
    ISSUE_START_FAILED,
    ISSUE_UPDATE_HELD,
    OPT_AUTO_UPDATE,
    OPT_PIP_SPEC,
    OPT_WEBHOOK_AUTH,
    WEBHOOK_AUTH_HA,
)
from custom_components.ha_mcp_tools.coordinator import ServerVersionInfo  # noqa: E402


def _make_hass() -> MagicMock:
    hass = MagicMock(name="hass")
    hass.data = {}

    def _update_entry(entry, *, data=None, **_kw):
        if data is not None:
            entry.data = data

    hass.config_entries.async_update_entry = MagicMock(side_effect=_update_entry)

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
    monkeypatch.setattr(esetup, "async_register_webhook", AsyncMock())
    monkeypatch.setattr(esetup, "async_unregister_webhook", AsyncMock())
    monkeypatch.setattr(esetup, "async_register_llm_api", AsyncMock())
    monkeypatch.setattr(esetup, "async_unregister_llm_api", MagicMock())
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
        # Conversation-agent LLM API (#1745): registered with the running
        # server's port + secret path.
        kwargs = esetup.async_register_llm_api.await_args.kwargs
        assert kwargs["port"] == 9584
        assert kwargs["secret_path"] == "/private_x"

    async def test_success_clears_stale_repair_issues(self, fake_manager):
        # Review gap: a successful bring-up must clear EVERY repair-issue id
        # left by a previous failed attempt, or a fixed install keeps showing
        # a stale repair forever. The update-held issue clears here too: a
        # reload that reached bring-up either bypassed the hold deliberately
        # (Install button) or made it moot, and the post-setup coordinator
        # refresh re-files it if it still applies.
        hass = _make_hass()
        entry = _make_entry()

        await esetup.async_bring_up_server(hass, entry)

        cleared = {c.args[2] for c in esetup.ir.async_delete_issue.call_args_list}
        assert cleared == {
            esetup.ISSUE_PACKAGE_FAILED,
            esetup.ISSUE_START_FAILED,
            esetup.ISSUE_UPDATE_HELD,
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

    async def test_package_failure_drops_pending_update_marker(self, fake_manager):
        # The install did not land: the deferred "updated" notification must
        # never fire for it - the repair issue is the user-facing signal.
        hass = _make_hass()
        hass.data[DOMAIN] = {DATA_PENDING_UPDATE_NOTIFY: {"old": "7.9.0"}}
        entry = _make_entry()
        fake_manager.async_start.side_effect = esetup.EmbeddedServerError(
            "pip failed", kind="package"
        )

        await esetup.async_bring_up_server(hass, entry)

        assert DATA_PENDING_UPDATE_NOTIFY not in hass.data[DOMAIN]

    async def test_cancelled_bringup_keeps_pending_update_marker(self, fake_manager):
        # Deliberately NOT dropped on cancellation: this bring-up never ran (the
        # entry was unloaded before it started), so the marker belongs to
        # whichever bring-up runs next, not to this cancelled attempt.
        hass = _make_hass()
        hass.data[DOMAIN] = {DATA_PENDING_UPDATE_NOTIFY: {"old": "7.9.0"}}
        entry = _make_entry()
        fake_manager.async_start.side_effect = asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await esetup.async_bring_up_server(hass, entry)

        assert hass.data[DOMAIN][DATA_PENDING_UPDATE_NOTIFY] == {"old": "7.9.0"}


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


# ---------------------------------------------------------------------------
# Automatic-update decision (given a ServerVersionInfo from the coordinator)
# ---------------------------------------------------------------------------


def _make_async_hass() -> MagicMock:
    """A hass with an inline executor (from ``_make_hass``) and awaitable reload."""
    hass = _make_hass()
    hass.config_entries.async_reload = AsyncMock()
    return hass


class _FakeTask:
    """Stand-in for the bring-up ``asyncio.Task`` — only ``.done()`` is read."""

    def __init__(self, *, done: bool) -> None:
        self._done = done

    def done(self) -> bool:
        return self._done


class TestMaybeAutoUpdate:
    _NEWER = ServerVersionInfo(
        installed="7.9.0", latest="7.10.0", dist=DIST_NAME_STABLE
    )

    @pytest.fixture(autouse=True)
    def _no_component_gate(self, monkeypatch):
        """Neutralize the component-compatibility gate (fetch "fails" → the
        gate fails open) so these tests keep exercising only the original
        auto-update decision. The gate itself is covered by
        :class:`TestAutoUpdateComponentGate`."""
        monkeypatch.setattr(
            esetup,
            "_async_fetch_shipped_component_version",
            AsyncMock(return_value=None),
        )

    async def test_newer_version_reloads_and_sets_pending_marker(self, monkeypatch):
        # The notification no longer fires here - it's deferred to
        # _async_finish_update_cycle, which only runs after the reloaded
        # entry's bring-up actually confirms the install landed.
        hass = _make_async_hass()
        entry = _make_entry()
        notif = MagicMock()
        monkeypatch.setattr(esetup.persistent_notification, "async_create", notif)

        await esetup.async_maybe_auto_update(hass, entry, self._NEWER)

        hass.config_entries.async_reload.assert_awaited_once_with(entry.entry_id)
        assert hass.data[DOMAIN][DATA_PENDING_UPDATE_NOTIFY] == {"old": "7.9.0"}
        notif.assert_not_called()

    async def test_info_none_does_not_reload_or_set_marker(self, monkeypatch):
        hass = _make_async_hass()
        entry = _make_entry()

        await esetup.async_maybe_auto_update(hass, entry, None)

        hass.config_entries.async_reload.assert_not_awaited()
        assert DATA_PENDING_UPDATE_NOTIFY not in hass.data.get(DOMAIN, {})

    async def test_reload_failure_drops_marker_and_logs(self, monkeypatch, caplog):
        import logging

        hass = _make_async_hass()
        hass.config_entries.async_reload = AsyncMock(side_effect=RuntimeError("boom"))
        entry = _make_entry()

        with caplog.at_level(logging.ERROR):
            await esetup.async_maybe_auto_update(hass, entry, self._NEWER)  # no raise

        assert DATA_PENDING_UPDATE_NOTIFY not in hass.data.get(DOMAIN, {})
        assert "reload failed" in caplog.text

    async def test_equal_version_does_not_reload(self, monkeypatch):
        hass = _make_async_hass()
        entry = _make_entry()
        info = ServerVersionInfo(
            installed="7.9.0", latest="7.9.0", dist=DIST_NAME_STABLE
        )

        await esetup.async_maybe_auto_update(hass, entry, info)

        hass.config_entries.async_reload.assert_not_awaited()

    async def test_auto_update_off_does_not_reload(self, monkeypatch):
        hass = _make_async_hass()
        entry = _make_entry(options={OPT_AUTO_UPDATE: False})

        await esetup.async_maybe_auto_update(hass, entry, self._NEWER)

        hass.config_entries.async_reload.assert_not_awaited()

    async def test_override_does_not_reload(self, monkeypatch):
        hass = _make_async_hass()
        entry = _make_entry(options={OPT_PIP_SPEC: "ha-mcp==7.8.0"})

        await esetup.async_maybe_auto_update(hass, entry, self._NEWER)

        hass.config_entries.async_reload.assert_not_awaited()

    async def test_default_pip_spec_value_is_not_an_override(self, monkeypatch):
        # The default pip-spec ("ha-mcp") stored verbatim still means "no
        # override" - the reload must run, not skip.
        hass = _make_async_hass()
        entry = _make_entry(options={OPT_PIP_SPEC: DEFAULT_PIP_SPEC})

        await esetup.async_maybe_auto_update(hass, entry, self._NEWER)

        hass.config_entries.async_reload.assert_awaited_once_with(entry.entry_id)

    async def test_unknown_installed_version_does_not_reload(self, monkeypatch):
        hass = _make_async_hass()
        entry = _make_entry()
        info = ServerVersionInfo(installed=None, latest="7.10.0", dist=DIST_NAME_STABLE)

        await esetup.async_maybe_auto_update(hass, entry, info)

        hass.config_entries.async_reload.assert_not_awaited()

    async def test_unknown_latest_version_does_not_reload(self, monkeypatch):
        hass = _make_async_hass()
        entry = _make_entry()
        info = ServerVersionInfo(installed="7.9.0", latest=None, dist=DIST_NAME_STABLE)

        await esetup.async_maybe_auto_update(hass, entry, info)

        hass.config_entries.async_reload.assert_not_awaited()

    async def test_version_compare_failure_does_not_reload(self, monkeypatch):
        # Incomparable version strategies (AwesomeVersionException) must not
        # raise or reload; the next refresh retries.
        hass = _make_async_hass()
        entry = _make_entry()
        info = ServerVersionInfo(
            installed="not-a-version",
            latest="also-not-a-version",
            dist=DIST_NAME_STABLE,
        )
        monkeypatch.setattr(
            esetup,
            "AwesomeVersion",
            MagicMock(side_effect=esetup.AwesomeVersionException("bad version")),
        )

        await esetup.async_maybe_auto_update(hass, entry, info)

        hass.config_entries.async_reload.assert_not_awaited()

    async def test_bringup_in_flight_does_not_reload(self, monkeypatch):
        # The coordinator's first refresh can land while the background
        # bring-up (first pip install) is still running — reloading here would
        # cancel it mid-install.
        hass = _make_async_hass()
        hass.data[DOMAIN] = {DATA_BRINGUP_TASK: _FakeTask(done=False)}
        entry = _make_entry()

        await esetup.async_maybe_auto_update(hass, entry, self._NEWER)

        hass.config_entries.async_reload.assert_not_awaited()

    async def test_bringup_done_allows_reload(self, monkeypatch):
        hass = _make_async_hass()
        hass.data[DOMAIN] = {DATA_BRINGUP_TASK: _FakeTask(done=True)}
        entry = _make_entry()

        await esetup.async_maybe_auto_update(hass, entry, self._NEWER)

        hass.config_entries.async_reload.assert_awaited_once_with(entry.entry_id)


# ---------------------------------------------------------------------------
# Component-compatibility gate on the automatic server update (#1783/#1785)
# ---------------------------------------------------------------------------


class TestAutoUpdateComponentGate:
    """The pre-install gate: a server release that also shipped a newer custom
    component must not auto-install under the older running component (that is
    the #1783/#1785 breakage). Held is loud (repair issue + warning log) and
    escapable (HACS component update unblocks it automatically; the update
    entity's Install button bypasses this path entirely). Every failure inside
    the gate fails OPEN — the pre-gate behavior — so a GitHub hiccup can never
    wedge auto-updates.
    """

    _NEWER = ServerVersionInfo(
        installed="7.12.0", latest="7.12.1", dist=DIST_NAME_STABLE
    )

    def _stub_gate(self, monkeypatch, *, shipped, running="1.0.2"):
        monkeypatch.setattr(
            esetup,
            "_async_fetch_shipped_component_version",
            AsyncMock(return_value=shipped),
        )
        monkeypatch.setattr(
            esetup,
            "async_get_integration",
            AsyncMock(return_value=SimpleNamespace(version=running)),
        )

    async def test_newer_shipped_component_holds_update(self, monkeypatch, caplog):
        import logging

        hass = _make_async_hass()
        entry = _make_entry()
        self._stub_gate(monkeypatch, shipped="1.0.9", running="1.0.2")

        with caplog.at_level(logging.WARNING):
            await esetup.async_maybe_auto_update(hass, entry, self._NEWER)

        hass.config_entries.async_reload.assert_not_awaited()
        assert DATA_PENDING_UPDATE_NOTIFY not in hass.data.get(DOMAIN, {})
        esetup.ir.async_create_issue.assert_called_once()
        args = esetup.ir.async_create_issue.call_args.args
        kwargs = esetup.ir.async_create_issue.call_args.kwargs
        assert ISSUE_UPDATE_HELD in args
        assert kwargs["translation_placeholders"] == {
            "latest": "7.12.1",
            "shipped": "1.0.9",
            "running": "1.0.2",
        }
        assert kwargs["severity"] == esetup.ir.IssueSeverity.WARNING
        assert "HACS" in caplog.text

    async def test_same_shipped_component_proceeds_and_clears_issue(self, monkeypatch):
        hass = _make_async_hass()
        entry = _make_entry()
        self._stub_gate(monkeypatch, shipped="1.0.2", running="1.0.2")

        await esetup.async_maybe_auto_update(hass, entry, self._NEWER)

        hass.config_entries.async_reload.assert_awaited_once_with(entry.entry_id)
        assert (hass, DOMAIN, ISSUE_UPDATE_HELD) in [
            c.args for c in esetup.ir.async_delete_issue.call_args_list
        ]

    async def test_manifest_fetch_failure_fails_open(self, monkeypatch):
        # GitHub unreachable / tag layout changed → behave exactly as before
        # the gate existed: install the update. Held-forever is the failure
        # mode this trades away deliberately.
        hass = _make_async_hass()
        entry = _make_entry()
        self._stub_gate(monkeypatch, shipped=None)

        await esetup.async_maybe_auto_update(hass, entry, self._NEWER)

        hass.config_entries.async_reload.assert_awaited_once_with(entry.entry_id)
        esetup.ir.async_create_issue.assert_not_called()

    async def test_component_version_read_failure_fails_open(self, monkeypatch):
        hass = _make_async_hass()
        entry = _make_entry()
        monkeypatch.setattr(
            esetup,
            "_async_fetch_shipped_component_version",
            AsyncMock(return_value="1.0.9"),
        )
        monkeypatch.setattr(
            esetup,
            "async_get_integration",
            AsyncMock(side_effect=RuntimeError("loader boom")),
        )

        await esetup.async_maybe_auto_update(hass, entry, self._NEWER)

        hass.config_entries.async_reload.assert_awaited_once_with(entry.entry_id)
        esetup.ir.async_create_issue.assert_not_called()

    async def test_incomparable_component_versions_fail_open(self, monkeypatch):
        from awesomeversion import AwesomeVersion as RealAwesomeVersion

        hass = _make_async_hass()
        entry = _make_entry()
        self._stub_gate(monkeypatch, shipped="weird", running="strange")

        def picky(value):
            if value in ("weird", "strange"):
                raise esetup.AwesomeVersionException("bad version")
            return RealAwesomeVersion(value)

        monkeypatch.setattr(esetup, "AwesomeVersion", picky)

        await esetup.async_maybe_auto_update(hass, entry, self._NEWER)

        hass.config_entries.async_reload.assert_awaited_once_with(entry.entry_id)
        esetup.ir.async_create_issue.assert_not_called()

    async def test_up_to_date_clears_held_issue(self, monkeypatch):
        # The hold self-resolves: once the component update lands (installed
        # catches up via the then-unblocked reload) the stale issue must not
        # linger.
        hass = _make_async_hass()
        entry = _make_entry()
        self._stub_gate(monkeypatch, shipped="1.0.2", running="1.0.2")
        info = ServerVersionInfo(
            installed="7.12.1", latest="7.12.1", dist=DIST_NAME_STABLE
        )

        await esetup.async_maybe_auto_update(hass, entry, info)

        hass.config_entries.async_reload.assert_not_awaited()
        assert (hass, DOMAIN, ISSUE_UPDATE_HELD) in [
            c.args for c in esetup.ir.async_delete_issue.call_args_list
        ]

    async def test_gate_not_consulted_for_pip_spec_override(self, monkeypatch):
        # PR-tarball / pinned-version testing must stay unaffected: the
        # override path returns before the gate ever runs.
        hass = _make_async_hass()
        entry = _make_entry(options={OPT_PIP_SPEC: "ha-mcp==7.8.0"})
        fetch = AsyncMock(return_value="1.0.9")
        monkeypatch.setattr(esetup, "_async_fetch_shipped_component_version", fetch)

        await esetup.async_maybe_auto_update(hass, entry, self._NEWER)

        fetch.assert_not_awaited()
        hass.config_entries.async_reload.assert_not_awaited()


class TestFetchShippedComponentVersion:
    """The raw-manifest fetch behind the gate: resolves the component version
    that shipped at the candidate server release's git tag."""

    class _FakeResp:
        """Mimics aiohttp's content-type guard for a raw.githubusercontent.com
        response: the body is served as text/plain, so ``json()`` raises
        ContentTypeError unless the caller passes ``content_type=None`` —
        pinning the exact call production must make (review finding: without
        this, dropping ``content_type=None`` in production would silently turn
        every fetch into a fail-open and disarm the gate with all tests green).
        """

        def __init__(self, payload, *, raise_err=None):
            self._payload = payload
            self._raise_err = raise_err

        def raise_for_status(self):
            if self._raise_err is not None:
                raise self._raise_err

        async def json(self, content_type="application/json"):
            if content_type is not None:
                from aiohttp import ContentTypeError

                raise ContentTypeError(
                    None,
                    (),
                    message="Attempt to decode JSON with unexpected mimetype: "
                    "text/plain; charset=utf-8",
                )
            if isinstance(self._payload, Exception):
                raise self._payload
            return self._payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _FakeSession:
        def __init__(self, resp):
            self._resp = resp
            self.requested_urls = []

        def get(self, url):
            self.requested_urls.append(url)
            return self._resp

    def _install_session(self, monkeypatch, resp):
        session = self._FakeSession(resp)
        monkeypatch.setattr(
            esetup, "async_get_clientsession", MagicMock(return_value=session)
        )
        return session

    async def test_returns_version_from_release_tag_manifest(self, monkeypatch):
        hass = _make_hass()
        session = self._install_session(
            monkeypatch, self._FakeResp({"version": "1.0.2"})
        )

        result = await esetup._async_fetch_shipped_component_version(hass, "7.12.1")

        assert result == "1.0.2"
        assert len(session.requested_urls) == 1
        assert "/v7.12.1/" in session.requested_urls[0]
        assert session.requested_urls[0].endswith("manifest.json")

    async def test_http_error_returns_none(self, monkeypatch):
        from aiohttp import ClientError

        hass = _make_hass()
        self._install_session(
            monkeypatch, self._FakeResp({}, raise_err=ClientError("404"))
        )

        result = await esetup._async_fetch_shipped_component_version(hass, "7.12.1")

        assert result is None

    async def test_missing_version_key_returns_none(self, monkeypatch):
        hass = _make_hass()
        self._install_session(monkeypatch, self._FakeResp({"domain": "ha_mcp_tools"}))

        result = await esetup._async_fetch_shipped_component_version(hass, "7.12.1")

        assert result is None

    async def test_invalid_json_returns_none(self, monkeypatch):
        hass = _make_hass()
        self._install_session(monkeypatch, self._FakeResp(ValueError("not json")))

        result = await esetup._async_fetch_shipped_component_version(hass, "7.12.1")

        assert result is None

    async def test_timeout_returns_none(self, monkeypatch):
        # GitHub latency is the most common failure in the field; the
        # TimeoutError limb of the except tuple must fail open like the rest.
        hass = _make_hass()
        self._install_session(monkeypatch, self._FakeResp({}, raise_err=TimeoutError()))

        result = await esetup._async_fetch_shipped_component_version(hass, "7.12.1")

        assert result is None

    async def test_non_mapping_payload_returns_none(self, monkeypatch):
        # A JSON body that parses but is not an object ("null", a list) makes
        # payload["version"] raise TypeError — the tuple's TypeError limb.
        hass = _make_hass()
        self._install_session(monkeypatch, self._FakeResp(None))

        result = await esetup._async_fetch_shipped_component_version(hass, "7.12.1")

        assert result is None


# ---------------------------------------------------------------------------
# _async_finish_update_cycle (post-bring-up refresh + deferred notification)
# ---------------------------------------------------------------------------


class TestFinishUpdateCycle:
    def _fake_coordinator(self, *, data, refresh_side_effect=None):
        coordinator = MagicMock(name="coordinator")
        coordinator.data = data

        async def _refresh():
            if refresh_side_effect is not None:
                raise refresh_side_effect
            return None

        coordinator.async_refresh = AsyncMock(side_effect=_refresh)
        return coordinator

    async def test_marker_and_newer_install_fires_notification(self, monkeypatch):
        hass = _make_async_hass()
        hass.data[DOMAIN] = {
            DATA_PENDING_UPDATE_NOTIFY: {"old": "7.9.0"},
            DATA_UPDATE_COORDINATOR: self._fake_coordinator(
                data=ServerVersionInfo(
                    installed="7.10.0", latest="7.10.0", dist=DIST_NAME_STABLE
                )
            ),
        }
        notif = MagicMock()
        monkeypatch.setattr(esetup.persistent_notification, "async_create", notif)

        await esetup._async_finish_update_cycle(hass)

        hass.data[DOMAIN][DATA_UPDATE_COORDINATOR].async_refresh.assert_awaited_once()
        assert DATA_PENDING_UPDATE_NOTIFY not in hass.data[DOMAIN]
        notif.assert_called_once()
        message = notif.call_args.kwargs.get("message") or notif.call_args.args[1]
        assert "7.9.0" in message
        assert "7.10.0" in message
        assert "releases/tag/v7.10.0" in message
        assert notif.call_args.kwargs.get("notification_id") == (
            esetup._UPDATE_NOTIFICATION_ID
        )

    async def test_dev_channel_notification_links_commit_history(self, monkeypatch):
        hass = _make_async_hass()
        hass.data[DOMAIN] = {
            DATA_PENDING_UPDATE_NOTIFY: {"old": "7.9.0.dev1"},
            DATA_UPDATE_COORDINATOR: self._fake_coordinator(
                data=ServerVersionInfo(
                    installed="7.10.0.dev1", latest="7.10.0.dev1", dist=DIST_NAME_DEV
                )
            ),
        }
        notif = MagicMock()
        monkeypatch.setattr(esetup.persistent_notification, "async_create", notif)

        await esetup._async_finish_update_cycle(hass)

        message = notif.call_args.kwargs.get("message") or notif.call_args.args[1]
        assert "github.com/homeassistant-ai/ha-mcp/commits/master" in message
        assert "releases/tag" not in message

    async def test_no_marker_refreshes_but_does_not_notify(self, monkeypatch):
        hass = _make_async_hass()
        coordinator = self._fake_coordinator(
            data=ServerVersionInfo(
                installed="7.10.0", latest="7.10.0", dist=DIST_NAME_STABLE
            )
        )
        hass.data[DOMAIN] = {DATA_UPDATE_COORDINATOR: coordinator}
        notif = MagicMock()
        monkeypatch.setattr(esetup.persistent_notification, "async_create", notif)

        await esetup._async_finish_update_cycle(hass)

        coordinator.async_refresh.assert_awaited_once()
        notif.assert_not_called()

    async def test_installed_unchanged_consumes_marker_no_notify(self, monkeypatch):
        # A reload can legitimately resolve to the same build (e.g. already
        # the newest) - an "updated to" notification would be false.
        hass = _make_async_hass()
        hass.data[DOMAIN] = {
            DATA_PENDING_UPDATE_NOTIFY: {"old": "7.9.0"},
            DATA_UPDATE_COORDINATOR: self._fake_coordinator(
                data=ServerVersionInfo(
                    installed="7.9.0", latest="7.9.0", dist=DIST_NAME_STABLE
                )
            ),
        }
        notif = MagicMock()
        monkeypatch.setattr(esetup.persistent_notification, "async_create", notif)

        await esetup._async_finish_update_cycle(hass)

        assert DATA_PENDING_UPDATE_NOTIFY not in hass.data[DOMAIN]
        notif.assert_not_called()

    async def test_missing_coordinator_consumes_marker_no_crash(self, monkeypatch):
        hass = _make_async_hass()
        hass.data[DOMAIN] = {DATA_PENDING_UPDATE_NOTIFY: {"old": "7.9.0"}}
        notif = MagicMock()
        monkeypatch.setattr(esetup.persistent_notification, "async_create", notif)

        await esetup._async_finish_update_cycle(hass)  # must not raise

        assert DATA_PENDING_UPDATE_NOTIFY not in hass.data[DOMAIN]
        notif.assert_not_called()

    async def test_refresh_failure_is_swallowed_and_logged(self, monkeypatch, caplog):
        import logging

        hass = _make_async_hass()
        coordinator = self._fake_coordinator(
            data=ServerVersionInfo(
                installed="7.9.0", latest="7.9.0", dist=DIST_NAME_STABLE
            ),
            refresh_side_effect=RuntimeError("boom"),
        )
        hass.data[DOMAIN] = {DATA_UPDATE_COORDINATOR: coordinator}

        with caplog.at_level(logging.WARNING):
            await esetup._async_finish_update_cycle(hass)  # must not raise

        assert "version refresh failed" in caplog.text


# ---------------------------------------------------------------------------
# Component / server version-compatibility repair issue
# ---------------------------------------------------------------------------


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
