"""Unit tests for ha_report_issue's identifier and install collectors
(bug_report_sources) and the report they feed."""

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from ha_mcp.client.rest_client import ErrorLogPage
from ha_mcp.tools import bug_report_sources, tools_bug_report
from ha_mcp.tools.bug_report_redaction import KnownIdentifiers
from ha_mcp.tools.bug_report_sources import collect_known_identifiers, fetch_ha_install
from ha_mcp.tools.bug_report_templates import _format_ha_install_value

NETWORK_URL = {"internal": "http://172.17.0.3:8123", "external": None, "cloud": None}
NETWORK = {
    "adapters": [
        {
            "name": "lo",
            "enabled": False,
            "ipv4": [{"address": "127.0.0.1", "network_prefix": 8}],
            "ipv6": [{"address": "::1", "network_prefix": 128}],
        },
        {
            "name": "eth0",
            "enabled": True,
            "ipv4": [{"address": "192.168.1.150", "network_prefix": 24}],
            "ipv6": [{"address": "fe80::364d:ad1c:8b5b:e0f5", "network_prefix": 64}],
        },
    ],
    "configured_adapters": [],
}
HA_INFO = {
    "version": "core-2026.10.4",
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
    "config_dir": "/config",
}


def _ws_client(replies: dict[str, Any], base_url: str) -> SimpleNamespace:
    async def send(message: dict[str, Any]) -> dict[str, Any]:
        # Every identifier read carries its own short deadline (#2916 review).
        # Well under the client's 30 s default command wait.
        assert 0 < message["_wait_timeout"] <= 10
        return replies[message["type"]]

    return SimpleNamespace(send_websocket_message=send, base_url=base_url)


def _ok(result: Any) -> dict[str, Any]:
    return {"success": True, "result": result}


class TestCollectKnownIdentifiers:
    @pytest.mark.asyncio
    async def test_reads_urls_adapters_and_the_configured_url(self) -> None:
        replies = {
            "network/url": _ok(
                {
                    "internal": "http://172.17.0.3:8123",
                    "external": "https://Home.Example.org",
                    "cloud": None,
                }
            ),
            "network": _ok(NETWORK),
        }
        client = _ws_client(replies, "http://ha.example.net:8123")
        identifiers, warning = await collect_known_identifiers(client)
        assert warning is None
        assert identifiers == KnownIdentifiers(
            hosts=frozenset({"home.example.org", "ha.example.net"}),
            addresses=frozenset(
                {"172.17.0.3", "192.168.1.150", "fe80::364d:ad1c:8b5b:e0f5"}
            ),
        )

    @pytest.mark.asyncio
    async def test_loopback_and_single_label_hosts_stay_readable(self) -> None:
        """``supervisor`` is the app's own proxy URL host: redacting it would
        blank every mention of the Supervisor in the logs."""
        replies = {
            "network/url": _ok(
                {"internal": "http://homeassistant:8123", "external": None}
            ),
            "network": _ok({"adapters": [NETWORK["adapters"][0]]}),
        }
        client = _ws_client(replies, "http://supervisor/core")
        identifiers, warning = await collect_known_identifiers(client)
        assert warning is None
        assert identifiers == KnownIdentifiers()

    @pytest.mark.asyncio
    async def test_client_without_a_configured_url_is_tolerated(self) -> None:
        """The collector never raises; a client with no ``base_url`` (test
        doubles, or a future client type) just contributes no host."""
        replies = {"network/url": _ok({}), "network": _ok({"adapters": []})}
        client = SimpleNamespace(
            send_websocket_message=_ws_client(replies, "").send_websocket_message
        )
        identifiers, warning = await collect_known_identifiers(client)
        assert identifiers == KnownIdentifiers()
        assert warning is None

    @pytest.mark.asyncio
    async def test_absolute_fqdn_is_stored_without_its_trailing_dot(self) -> None:
        replies = {"network/url": _ok({}), "network": _ok({"adapters": []})}
        client = _ws_client(replies, "http://ha.example.net.:8123")
        identifiers, _ = await collect_known_identifiers(client)
        assert identifiers.hosts == frozenset({"ha.example.net"})

    @pytest.mark.asyncio
    async def test_failed_url_read_still_reads_the_adapters(self) -> None:
        """The two reads are independent: a transient failure of one must not
        drop what the other would have contributed."""
        replies = {
            "network/url": {
                "success": False,
                "error": "Command failed: timeout",
                "error_code": "timeout",
            },
            "network": _ok(NETWORK),
        }
        client = _ws_client(replies, "http://ha.example.net:8123")
        identifiers, warning = await collect_known_identifiers(client)
        assert identifiers == KnownIdentifiers(
            hosts=frozenset({"ha.example.net"}),
            addresses=frozenset({"192.168.1.150", "fe80::364d:ad1c:8b5b:e0f5"}),
        )
        assert warning is not None
        assert "network/url: timeout" in warning
        assert "network:" not in warning.replace("network/url:", "")

    @pytest.mark.asyncio
    async def test_malformed_configured_url_is_skipped(self) -> None:
        replies = {"network/url": _ok({}), "network": _ok({"adapters": []})}
        client = _ws_client(replies, "http://[bad:8123")
        identifiers, warning = await collect_known_identifiers(client)
        assert identifiers == KnownIdentifiers()
        assert warning is None

    @pytest.mark.asyncio
    async def test_a_read_that_never_answers_is_abandoned_at_the_deadline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The deadline covers connecting too, not only the reply wait, so a
        socket that hangs before the command is sent cannot stall the report."""
        monkeypatch.setattr(bug_report_sources, "_IDENTIFIER_READ_DEADLINE_S", 0.05)

        async def send(message: dict[str, Any]) -> dict[str, Any]:
            if message["type"] == "network/url":
                await asyncio.sleep(5)
            return _ok(NETWORK)

        client = SimpleNamespace(
            send_websocket_message=send, base_url="http://ha.example.net:8123"
        )
        identifiers, warning = await collect_known_identifiers(client)
        assert identifiers.hosts == frozenset({"ha.example.net"})
        assert "192.168.1.150" in identifiers.addresses
        assert warning is not None
        assert "network/url: TimeoutError" in warning

    @pytest.mark.asyncio
    async def test_failed_adapter_read_keeps_what_was_read_and_warns(self) -> None:
        """``network`` is admin-only in Core while ``network/url`` is not, so a
        non-admin token must still get the URL hosts redacted."""
        replies = {
            "network/url": _ok(NETWORK_URL),
            # The client's failure envelope: a string error plus error_code.
            "network": {
                "success": False,
                "error": "Command failed: Unauthorized",
                "error_code": "unauthorized",
            },
        }
        client = _ws_client(replies, "http://ha.example.net:8123")
        identifiers, warning = await collect_known_identifiers(client)
        assert identifiers == KnownIdentifiers(
            hosts=frozenset({"ha.example.net"}), addresses=frozenset({"172.17.0.3"})
        )
        assert warning is not None
        assert "network: unauthorized" in warning

    @pytest.mark.asyncio
    async def test_transport_failure_keeps_the_configured_host(self) -> None:
        """The server's own Home Assistant host needs no read, so it is redacted
        even when the WebSocket is down; the exception text may name that host,
        so the warning never echoes it."""
        client = SimpleNamespace(
            send_websocket_message=AsyncMock(
                side_effect=ConnectionError("cannot reach ha.example.net")
            ),
            base_url="http://ha.example.net:8123",
        )
        identifiers, warning = await collect_known_identifiers(client)
        assert identifiers == KnownIdentifiers(hosts=frozenset({"ha.example.net"}))
        assert warning is not None
        assert "network/url: ConnectionError; network: ConnectionError" in warning
        assert "ha.example.net" not in warning


def _ws_with_event(
    event_response: Any = None, error: Exception | None = None
) -> MagicMock:
    ws = MagicMock()
    ws.send_command_with_event = AsyncMock(
        return_value=({"success": True}, event_response), side_effect=error
    )
    ws.disconnect = AsyncMock()
    return ws


class TestFetchHaInstall:
    @pytest.fixture
    def client(self) -> SimpleNamespace:
        return SimpleNamespace(base_url="http://ha:8123", token="t", verify_ssl=True)

    @pytest.mark.asyncio
    async def test_keeps_install_facts_and_drops_personal_ones(
        self, client, monkeypatch
    ):
        event = {
            "event": {"type": "initial", "data": {"homeassistant": {"info": HA_INFO}}}
        }
        ws = _ws_with_event(event)
        monkeypatch.setattr(
            bug_report_sources,
            "get_connected_ws_client",
            AsyncMock(return_value=(ws, None)),
        )
        install = await fetch_ha_install(client)
        assert install is not None
        assert install["installation_type"] == "Home Assistant Container"
        assert install["os_version"] == "7.0.0-1017-azure"
        for personal in ("user", "config_dir", "timezone"):
            assert personal not in install
        ws.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("error", [TimeoutError(), RuntimeError("boom")])
    async def test_failed_read_is_none_and_disconnects(
        self, client, monkeypatch, error
    ):
        ws = _ws_with_event(error=error)
        monkeypatch.setattr(
            bug_report_sources,
            "get_connected_ws_client",
            AsyncMock(return_value=(ws, None)),
        )
        assert await fetch_ha_install(client) is None
        ws.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unreachable_websocket_is_none(self, client, monkeypatch):
        monkeypatch.setattr(
            bug_report_sources,
            "get_connected_ws_client",
            AsyncMock(return_value=(None, {"error": "refused"})),
        )
        assert await fetch_ha_install(client) is None


class TestFormatHaInstallValue:
    def test_container_install(self) -> None:
        install = {k: HA_INFO[k] for k in bug_report_sources._HA_INSTALL_KEYS}
        assert _format_ha_install_value({"ha_install": install}) == (
            "Home Assistant Container; Linux 7.0.0-1017-azure "
            "(x86_64, container amd64); Python 3.14.9"
        )

    def test_install_without_container_arch(self) -> None:
        install = {
            "installation_type": "Home Assistant Core",
            "os_name": "Linux",
            "os_version": "6.8.0",
            "arch": "aarch64",
            "python_version": "3.13.7",
        }
        assert _format_ha_install_value({"ha_install": install}) == (
            "Home Assistant Core; Linux 6.8.0 (aarch64); Python 3.13.7"
        )

    def test_unavailable(self) -> None:
        assert _format_ha_install_value({"ha_install": None}) == (
            "unknown (system_health unavailable)"
        )


class TestReportRedactsKnownIdentifiers:
    """A bare address of this instance has no network context the regex rules
    can see, so before the known-identifier pass it reached the report."""

    def _report(
        self,
        monkeypatch: pytest.MonkeyPatch,
        replies: dict[str, Any],
        install: dict[str, Any] | None,
    ) -> tools_bug_report.BugReportTools:
        client = _ws_client(replies, "http://127.0.0.1:8123")
        client.get_config = AsyncMock(return_value={"version": "2026.10.4"})
        client.get_states = AsyncMock(return_value=[])
        client.get_error_log = AsyncMock(
            return_value=ErrorLogPage(
                text="2026-10-30 ERROR (MainThread) [custom] Lost 172.17.0.3 link\n",
                has_more=False,
            )
        )
        recent = [
            {
                "timestamp": "2026-10-30T12:00:00",
                "tool_name": "ha_call_service",
                "success": False,
                "execution_time_ms": 5,
                "error_message": "Setup failed for 172.17.0.3",
            }
        ]
        monkeypatch.setattr(tools_bug_report, "get_recent_logs", lambda **_: recent)
        monkeypatch.setattr(tools_bug_report, "get_startup_logs", list)
        monkeypatch.setattr(tools_bug_report, "read_info", AsyncMock(return_value=None))
        monkeypatch.setattr(
            tools_bug_report, "fetch_ha_install", AsyncMock(return_value=install)
        )
        report = tools_bug_report.BugReportTools(client)
        for name in (
            "_detect_component_version",
            "_detect_tools_entry_status",
            "_detect_server_entry_status",
        ):
            monkeypatch.setattr(report, name, AsyncMock(return_value=None))
        return report

    @pytest.mark.asyncio
    async def test_bare_known_address_is_redacted_in_every_log(
        self, monkeypatch
    ) -> None:
        install = {k: HA_INFO[k] for k in bug_report_sources._HA_INSTALL_KEYS}
        replies = {"network/url": _ok(NETWORK_URL), "network": _ok(NETWORK)}
        report = self._report(monkeypatch, replies, install)
        result = await report.ha_report_issue(report_type="runtime_bug")
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

    @pytest.mark.asyncio
    async def test_bare_known_address_is_redacted_in_the_addon_log(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The app log is scrubbed at fetch time, inside the identifier scope,
        so the real Supervisor read is mocked rather than the fetcher."""
        install = {k: HA_INFO[k] for k in bug_report_sources._HA_INSTALL_KEYS}
        replies = {"network/url": _ok(NETWORK_URL), "network": _ok(NETWORK)}
        report = self._report(monkeypatch, replies, install)
        monkeypatch.setattr(
            tools_bug_report, "_detect_installation_method", lambda: "addon"
        )
        monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")
        addon_log = httpx.Response(200, text="INFO ha_mcp: upstream is 172.17.0.3\n")
        with patch("httpx.AsyncClient.get", return_value=addon_log):
            result = await report.ha_report_issue(report_type="runtime_bug")
        assert "172.17.0.3" not in result["addon_logs"]
        assert "upstream is [IP]" in result["addon_logs"]
        assert "upstream is [IP]" in result["issue_body"]

    @pytest.mark.asyncio
    async def test_failed_identifier_read_is_reported_not_fatal(
        self, monkeypatch
    ) -> None:
        replies = {
            "network/url": {
                "success": False,
                "error": "Command failed: Unauthorized",
                "error_code": "unauthorized",
            }
        }
        report = self._report(monkeypatch, replies, None)
        result = await report.ha_report_issue(report_type="runtime_bug")
        assert result["success"] is True
        assert len(result["warnings"]) == 1
        assert "network/url" in result["warnings"][0]
        assert "unknown (system_health unavailable)" in result["issue_body"]
