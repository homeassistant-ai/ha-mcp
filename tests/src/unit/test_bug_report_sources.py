"""ha_report_issue's identifier and install collectors, on a real Core.

The report redacts this instance's own hostnames and addresses, read from
Core's ``network/url`` and ``network`` commands, and states the install from
``system_health/info``. Core's network adapters come from ``ifaddr`` in a unit
test, so a test that needs particular adapters gives Core its own.
"""

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import ifaddr
import pytest
from aiohttp import web
from homeassistant.components.network import async_reload_adapters
from homeassistant.const import KEY_DATA_LOGGING
from homeassistant.core import HomeAssistant
from homeassistant.helpers.system_info import async_get_system_info
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.client.websocket_client import (
    DEFAULT_COMMAND_WAIT_TIMEOUT,
    HomeAssistantWebSocketClient,
)
from ha_mcp.tools import bug_report_sources
from ha_mcp.tools.bug_report_redaction import KnownIdentifiers
from ha_mcp.tools.bug_report_sources import collect_known_identifiers, fetch_ha_install
from ha_mcp.tools.tools_bug_report import BugReportTools
from ha_mcp.utils.usage_logger import log_tool_call

from ._core_client import refused_url
from ._gateway import Gateway
from ._gateway import gateway as gateway
from ._gateway import gateway_client as gateway_client

ETH0 = ifaddr.Adapter(
    "eth0",
    "eth0",
    [
        ifaddr.IP("192.168.1.150", 24, "eth0"),
        ifaddr.IP(("fe80::364d:ad1c:8b5b:e0f5", 0, 2), 64, "eth0"),
    ],
    index=2,
)
LOOPBACK = ifaddr.Adapter(
    "lo",
    "lo",
    [ifaddr.IP("127.0.0.1", 8, "lo"), ifaddr.IP(("::1", 0, 0), 128, "lo")],
    index=1,
)
# What Core reports for a Home Assistant Container install, which a unit test
# cannot be.
CONTAINER_INFO = {
    "version": "2026.10.4",
    "installation_type": "Home Assistant Container",
    "dev": False,
    "hassio": False,
    "docker": True,
    "container_arch": "amd64",
    "user": "root",
    "virtualenv": False,
    "python_version": "3.14.9",
    "os_name": "Linux",
    "os_version": "7.0.0-1017-azure",
    "arch": "x86_64",
    "timezone": "America/Toronto",
}


async def _set_up_network(hass: HomeAssistant, *adapters: ifaddr.Adapter) -> None:
    """Set up ``network`` with these interfaces.

    ``http`` has already loaded Core's adapters from the plugin's own
    ``ifaddr`` answer, so Core reloads them, as when the host's interfaces
    change.
    """
    assert await async_setup_component(hass, "network", {})
    with patch(
        "homeassistant.components.network.util.ifaddr.get_adapters",
        return_value=list(adapters),
    ):
        await async_reload_adapters(hass)


@pytest.fixture
def disconnect_spy() -> Iterator[Any]:
    """Count WebSocket disconnects; each still runs."""
    with patch.object(
        HomeAssistantWebSocketClient,
        "disconnect",
        autospec=True,
        side_effect=HomeAssistantWebSocketClient.disconnect,
    ) as spy:
        yield spy


class TestCollectKnownIdentifiers:
    async def test_reads_core_urls_and_adapter_addresses(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        await _set_up_network(hass, LOOPBACK, ETH0)
        await hass.config.async_update(
            internal_url="http://172.17.0.3:8123",
            external_url="https://Home.Example.org",
        )

        with patch.object(
            core_client,
            "send_websocket_message",
            wraps=core_client.send_websocket_message,
        ) as sent:
            identifiers, warning = await collect_known_identifiers(core_client)

        assert warning is None
        # The client's own base URL is loopback, like the lo adapter: neither
        # identifies anybody.
        assert identifiers == KnownIdentifiers(
            hosts=frozenset({"home.example.org"}),
            addresses=frozenset(
                {"172.17.0.3", "192.168.1.150", "fe80::364d:ad1c:8b5b:e0f5"}
            ),
        )
        # Every identifier read carries its own short deadline (#2916 review),
        # well under the client's default command wait.
        waits = [call.args[0]["_wait_timeout"] for call in sent.await_args_list]
        assert len(waits) == 2
        assert all(0 < wait < DEFAULT_COMMAND_WAIT_TIMEOUT for wait in waits)

    async def test_loopback_and_single_label_hosts_stay_readable(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        """A single-label name (``homeassistant``, or ``supervisor`` in the
        app's proxy URL) is generic: redacting it would blank every mention of
        the word in the logs."""
        await _set_up_network(hass, LOOPBACK)
        await hass.config.async_update(internal_url="http://homeassistant:8123")

        identifiers, warning = await collect_known_identifiers(core_client)

        assert warning is None
        assert identifiers == KnownIdentifiers()

    async def test_absolute_fqdn_is_stored_without_its_trailing_dot(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        await _set_up_network(hass)
        await hass.config.async_update(internal_url="http://ha.example.net.:8123")

        identifiers, _ = await collect_known_identifiers(core_client)

        assert identifiers.hosts == frozenset({"ha.example.net"})

    async def test_dropped_url_read_still_reads_the_adapters(
        self,
        hass: HomeAssistant,
        gateway: Gateway,
        gateway_client: HomeAssistantClient,
    ) -> None:
        """The two reads are independent: a transient failure of one must not
        drop what the other would have contributed."""
        await _set_up_network(hass, ETH0)
        gateway.drops.add("network/url")

        identifiers, warning = await collect_known_identifiers(gateway_client)

        assert identifiers == KnownIdentifiers(
            addresses=frozenset({"192.168.1.150", "fe80::364d:ad1c:8b5b:e0f5"})
        )
        assert warning is not None
        assert "network/url: " in warning
        assert "network: " not in warning

    async def test_a_read_that_never_answers_is_abandoned_at_the_deadline(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A read that hangs cannot stall the report. The client's socket is
        opened first, so only the hung read can reach the deadline."""
        await _set_up_network(hass, ETH0)
        await hass.config.async_update(external_url="https://home.example.org")
        await core_client.send_websocket_message({"type": "network/url"})
        monkeypatch.setattr(bug_report_sources, "IDENTIFIER_READ_DEADLINE_S", 1.0)
        never = asyncio.Event()

        async def adapters_never_load(hass: HomeAssistant) -> None:
            await never.wait()

        try:
            with patch(
                "homeassistant.components.network.websocket.async_get_network",
                adapters_never_load,
            ):
                identifiers, warning = await collect_known_identifiers(core_client)
        finally:
            never.set()

        assert identifiers.hosts == frozenset({"home.example.org"})
        assert warning is not None
        assert "network: TimeoutError" in warning

    async def test_refused_adapter_read_keeps_the_urls_and_warns(
        self, hass: HomeAssistant, read_only_client: HomeAssistantClient
    ) -> None:
        """``network`` is admin-only in Core while ``network/url`` is not, so a
        non-admin token must still get the URL hosts redacted."""
        await _set_up_network(hass, ETH0)
        await hass.config.async_update(internal_url="http://172.17.0.3:8123")

        identifiers, warning = await collect_known_identifiers(read_only_client)

        assert identifiers == KnownIdentifiers(addresses=frozenset({"172.17.0.3"}))
        assert warning is not None
        assert "network: unauthorized" in warning

    async def test_no_answer_keeps_the_configured_host(self) -> None:
        """The server's own Home Assistant host needs no read, so it is redacted
        even when the WebSocket is down; the exception text may name that host,
        so the warning never echoes it."""
        client = HomeAssistantClient(
            base_url="http://ha.example.net:8123", token="token", verify_ssl=False
        )
        try:
            with patch.object(
                client,
                "send_websocket_message",
                AsyncMock(
                    side_effect=HomeAssistantConnectionError(
                        "cannot reach ha.example.net"
                    )
                ),
            ):
                identifiers, warning = await collect_known_identifiers(client)
        finally:
            await client.close()

        assert identifiers == KnownIdentifiers(hosts=frozenset({"ha.example.net"}))
        assert warning is not None
        assert (
            "network/url: HomeAssistantConnectionError; "
            "network: HomeAssistantConnectionError" in warning
        )
        assert "ha.example.net" not in warning

    async def test_malformed_configured_url_is_skipped(self) -> None:
        """A malformed server URL gets no answer and has no host to redact;
        the collector still returns instead of raising."""
        client = HomeAssistantClient(
            base_url="http://[bad:8123", token="token", verify_ssl=False
        )
        try:
            identifiers, warning = await collect_known_identifiers(client)
        finally:
            await client.close()

        assert identifiers == KnownIdentifiers()
        assert warning is not None


class TestFetchHaInstall:
    async def test_keeps_install_facts_and_drops_personal_ones(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        disconnect_spy: Any,
    ) -> None:
        assert await async_setup_component(hass, "homeassistant", {})
        assert await async_setup_component(hass, "system_health", {})
        info = await async_get_system_info(hass)

        install = await fetch_ha_install(core_client)

        assert install is not None
        for fact in ("installation_type", "os_name", "os_version", "python_version"):
            assert install[fact] == info[fact]
        for personal in ("user", "config_dir", "timezone"):
            assert personal not in install
        disconnect_spy.assert_awaited_once()

    @pytest.mark.parametrize("failure", ["no_answer", "not_set_up"])
    async def test_failed_read_is_none_and_disconnects(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        disconnect_spy: Any,
        failure: str,
    ) -> None:
        if failure == "no_answer":
            assert await async_setup_component(hass, "system_health", {})
            with patch.object(
                HomeAssistantWebSocketClient,
                "send_command_with_event",
                side_effect=TimeoutError(),
            ):
                assert await fetch_ha_install(core_client) is None
        else:
            # Core answers ``unknown_command`` without ``system_health``.
            assert await fetch_ha_install(core_client) is None
        disconnect_spy.assert_awaited_once()

    async def test_unreachable_websocket_is_none(
        self, hass_access_token: str, socket_enabled: None
    ) -> None:
        client = HomeAssistantClient(
            base_url=refused_url(),
            token=hass_access_token,
            verify_ssl=False,
        )
        try:
            assert await fetch_ha_install(client) is None
        finally:
            await client.close()


@pytest.fixture
def log_file(hass: HomeAssistant, tmp_path: Path) -> Path:
    """The file Core serves as its error log; asked for before ``core_client``,
    whose ``api`` setup only serves the log when Core knows the file."""
    path = tmp_path / "home-assistant.log"
    path.write_text(
        "2026-10-30 ERROR (MainThread) [custom] Lost 172.17.0.3 link\n",
        encoding="utf-8",
    )
    hass.data[KEY_DATA_LOGGING] = str(path)
    return path


async def _set_up_reporting_core(hass: HomeAssistant, info: dict[str, Any]) -> Any:
    """Core with its network, an internal URL and ``system_health``.

    Returns the patch that makes Core report ``info`` as its system info.
    """
    await _set_up_network(hass, ETH0)
    await hass.config.async_update(internal_url="http://172.17.0.3:8123")
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "system_health", {})
    return patch(
        "homeassistant.helpers.system_info.async_get_system_info", return_value=info
    )


class TestReport:
    """A bare address of this instance has no network context the regex rules
    can see, so before the known-identifier pass it reached the report."""

    async def test_bare_known_address_is_redacted_in_every_log(
        self,
        hass: HomeAssistant,
        log_file: Path,
        core_client: HomeAssistantClient,
    ) -> None:
        system_info = await _set_up_reporting_core(hass, CONTAINER_INFO)
        log_tool_call(
            "ha_call_service",
            {},
            execution_time_ms=5,
            success=False,
            error_message="Setup failed for 172.17.0.3",
        )

        with system_info:
            result = await BugReportTools(core_client).ha_report_issue(
                report_type="runtime_bug"
            )

        body = result["issue_body"]
        assert "172.17.0.3" not in body
        assert "172.17.0.3" not in result["formatted_report"]
        assert "172.17.0.3" not in result["core_error_log"]
        assert "Lost [IP] link" in body
        assert "Setup failed for [IP]" in body
        assert (
            "- **Home Assistant Install:** Home Assistant Container; Linux "
            "7.0.0-1017-azure (x86_64, container amd64); Python 3.14.9" in body
        )
        assert "warnings" not in result

    async def test_install_without_container_arch(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        core_install = {
            **CONTAINER_INFO,
            "installation_type": "Home Assistant Core",
            "docker": False,
            "container_arch": None,
            "os_version": "6.8.0",
            "arch": "aarch64",
            "python_version": "3.13.7",
        }
        system_info = await _set_up_reporting_core(hass, core_install)

        with system_info:
            result = await BugReportTools(core_client).ha_report_issue(
                report_type="runtime_bug"
            )

        assert (
            "- **Home Assistant Install:** Home Assistant Core; Linux 6.8.0 "
            "(aarch64); Python 3.13.7" in result["issue_body"]
        )

    async def test_bare_known_address_is_redacted_in_the_app_log(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        aiohttp_client: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The app log is scrubbed at fetch time, inside the identifier scope."""

        async def own_logs(request: web.Request) -> web.Response:
            return web.Response(text="INFO ha_mcp: upstream is 172.17.0.3\n")

        supervisor_app = web.Application()
        supervisor_app.router.add_get("/addons/self/logs", own_logs)
        supervisor = await aiohttp_client(supervisor_app)
        monkeypatch.setenv("SUPERVISOR_TOKEN", "supervisor-token")
        monkeypatch.setenv(
            "SUPERVISOR_BASE_URL", str(supervisor.make_url("")).rstrip("/")
        )
        system_info = await _set_up_reporting_core(hass, CONTAINER_INFO)

        with system_info:
            result = await BugReportTools(core_client).ha_report_issue(
                report_type="runtime_bug"
            )

        assert "172.17.0.3" not in result["addon_logs"]
        assert "upstream is [IP]" in result["addon_logs"]
        assert "upstream is [IP]" in result["issue_body"]

    async def test_failed_identifier_read_is_reported_not_fatal(
        self, core_client: HomeAssistantClient
    ) -> None:
        """Without ``network`` and ``system_health`` Core rejects both reads."""
        result = await BugReportTools(core_client).ha_report_issue(
            report_type="runtime_bug"
        )

        assert result["success"] is True
        assert len(result["warnings"]) == 1
        assert "network/url" in result["warnings"][0]
        assert "unknown (system_health unavailable)" in result["issue_body"]
