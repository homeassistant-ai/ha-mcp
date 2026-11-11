"""What ha_report_issue renders about the client, its host and the config.

Each report comes from ``ha_report_issue`` on the server's own client. The
MCP client identifies itself in its handshake; the transport is the one the
operator sets in the environment; the launching host app and the request's
User-Agent come from the process tree and the HTTP request, which a unit test
does not have, so those two readers answer what the test gives them.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from ha_mcp._vendor.mcp.types import Implementation
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.config import reset_global_settings
from ha_mcp.tools import tools_bug_report

from ._report_issue import fail_a_tool_call, report_issue, report_line


@pytest.fixture
def connection(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Set the transport, the launching host app and the request's User-Agent."""

    def _set(
        transport: str, host: dict[str, str] | None = None, user_agent: str = ""
    ) -> None:
        monkeypatch.setenv("FASTMCP_TRANSPORT", transport)
        monkeypatch.setattr(tools_bug_report, "detect_client_host", lambda: host or {})
        monkeypatch.setattr(
            tools_bug_report,
            "get_http_headers",
            lambda include=None: {"user-agent": user_agent} if user_agent else {},
        )

    return _set


class TestConfigToggles:
    """The report's config section lists the settings the server runs with."""

    async def test_renders_bullets_with_value(
        self, core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ENABLE_TOOL_SEARCH", "false")
        monkeypatch.setenv("TOOL_SEARCH_MAX_RESULTS", "7")
        reset_global_settings()

        body = (await report_issue(core_client, fields="issue_body"))["issue_body"]

        assert "- **enable_tool_search:** `False`" in body
        assert "- **tool_search_max_results:** `7`" in body

    async def test_unreadable_settings_render_a_placeholder(
        self, core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TOOL_SEARCH_MAX_RESULTS", "not a number")
        reset_global_settings()

        body = (await report_issue(core_client, fields="issue_body"))["issue_body"]

        assert "_(config toggles unavailable)_" in body


class TestClientInfo:
    """The `MCP Client:` line — the client's self-identification."""

    async def test_no_client_info_renders_unknown_placeholder(
        self, core_client: HomeAssistantClient
    ) -> None:
        rendered = report_line(await report_issue(core_client), "MCP Client")
        assert "unknown" in rendered
        # Phrasing describes the observable, not the underlying API field name,
        # so the message stays accurate if MCP renames the attribute.
        assert "did not advertise" in rendered

    @pytest.mark.parametrize(
        ("client_info", "expected"),
        [
            pytest.param(
                Implementation(name="Claude Desktop", version="0.7.42"),
                "Claude Desktop 0.7.42",
                id="name-and-version",
            ),
            # Many clients send title == name; don't print the same string twice.
            pytest.param(
                Implementation(name="Cursor", version="0.42", title="Cursor"),
                "Cursor 0.42",
                id="title-matching-name",
            ),
        ],
    )
    async def test_client_renders_as_a_single_line(
        self,
        core_client: HomeAssistantClient,
        client_info: Implementation,
        expected: str,
    ) -> None:
        report = await report_issue(core_client, client_info=client_info)
        assert report_line(report, "MCP Client") == expected

    async def test_distinct_title_appears_as_aside(
        self, core_client: HomeAssistantClient
    ) -> None:
        client_info = Implementation(
            name="ClaudeDesktop",
            version="0.7.42",
            title="Claude Desktop (claude.ai integration)",
        )
        rendered = report_line(
            await report_issue(core_client, client_info=client_info), "MCP Client"
        )
        assert "ClaudeDesktop 0.7.42" in rendered
        assert "Claude Desktop (claude.ai integration)" in rendered


class TestClientHost:
    """The `MCP Client Host:` row — what the server learned beyond clientInfo."""

    @staticmethod
    async def _host(client: HomeAssistantClient, client_info: Implementation) -> str:
        report = await report_issue(client, client_info=client_info)
        return report_line(report, "MCP Client Host")

    async def test_stdio_host_detected_renders_name_and_version(
        self, core_client: HomeAssistantClient, connection: Any
    ) -> None:
        connection("stdio", {"name": "Claude Desktop", "version": "2.110.0"})
        line = await self._host(
            core_client,
            Implementation(name="local-agent-mode-Home Assistant", version="1.0"),
        )
        assert line == (
            "Claude Desktop (local agent mode); "
            "Claude Desktop 2.110.0 _(from the parent process)_"
        )

    async def test_stdio_without_host_says_not_detected(
        self, core_client: HomeAssistantClient, connection: Any
    ) -> None:
        connection("stdio")
        line = await self._host(
            core_client, Implementation(name="cursor-vscode", version="1.0")
        )
        assert line == "not detected"

    async def test_local_agent_mode_prefix_is_labelled_even_when_host_unknown(
        self, core_client: HomeAssistantClient, connection: Any
    ) -> None:
        connection("stdio")
        line = await self._host(
            core_client,
            Implementation(name="local-agent-mode-home-assistant", version="1.0"),
        )
        assert line == "Claude Desktop (local agent mode); not detected"

    async def test_http_renders_user_agent_alongside_client_info(
        self, core_client: HomeAssistantClient, connection: Any
    ) -> None:
        connection(
            "http",
            user_agent="Claude-User (+https://docs.anthropic.com/claude-code)",
        )
        line = await self._host(
            core_client, Implementation(name="claude-ai", version="1.0")
        )
        assert (
            line == "User-Agent `Claude-User (+https://docs.anthropic.com/claude-code)`"
        )

    async def test_http_user_agent_not_repeated_when_it_already_is_the_client_info(
        self, core_client: HomeAssistantClient, connection: Any
    ) -> None:
        # The clientInfo fallback already parsed User-Agent into name/version.
        connection("http", user_agent="python-httpx/0.28.1")
        line = await self._host(
            core_client,
            Implementation(
                name="python-httpx", version="0.28.1", title="from HTTP User-Agent"
            ),
        )
        assert line == "not detected"

    async def test_python_sdk_default_identity_is_flagged_as_a_bridge(
        self, core_client: HomeAssistantClient, connection: Any
    ) -> None:
        # Observed live: Claude Desktop -> fastmcp-remote -> component shows
        # up as the Python MCP SDK's default clientInfo, hiding Desktop.
        connection("http", user_agent="python-httpx/0.28.1")
        line = await self._host(
            core_client, Implementation(name="mcp", version="0.1.0")
        )
        assert line.startswith("stdio bridge (Python MCP SDK default identity")
        assert "ask the user which app and version launched the bridge" in line
        assert "User-Agent `python-httpx/0.28.1`" in line

    async def test_mcp_name_with_a_real_version_is_not_a_bridge(
        self, core_client: HomeAssistantClient, connection: Any
    ) -> None:
        # Only the SDK's literal default (mcp 0.1.0) marks a bridge; a client
        # that calls itself "mcp" with its own version is reported as-is.
        connection("http")
        line = await self._host(
            core_client, Implementation(name="mcp", version="2.3.0")
        )
        assert "stdio bridge" not in line
        assert line == "not detected"

    async def test_named_bridges_are_flagged(
        self, core_client: HomeAssistantClient, connection: Any
    ) -> None:
        connection("http")
        line = await self._host(
            core_client, Implementation(name="mcp-remote", version="1.0")
        )
        assert "stdio bridge (mcp-remote bridge)" in line


def _searched_terms(report: dict[str, Any]) -> list[str]:
    """What each duplicate-check link searches for, besides ``is:issue``."""
    return [
        parse_qs(urlparse(url).query)["q"][0].removeprefix("is:issue ")
        for url in report["duplicate_check_urls"]
    ]


async def test_feature_request_duplicate_search_uses_its_title(
    core_client: HomeAssistantClient,
) -> None:
    """A feature request has no error to search by; falling back to "bug"
    or to the session's last failed call would never find an earlier
    request for the same feature."""
    await fail_a_tool_call(RuntimeError("Service not found"), "ha_call_service")

    feature = await report_issue(
        core_client, report_type="feature_request", title="Manage Thread datasets"
    )
    bug = await report_issue(core_client)
    # A blank title falls back to the usual keywords instead of an empty term.
    blank = await report_issue(core_client, report_type="feature_request", title="   ")

    assert _searched_terms(feature) == ["Manage Thread datasets"]
    assert "ha_call_service" in _searched_terms(bug)
    assert sorted(_searched_terms(blank)) == sorted(_searched_terms(bug))
