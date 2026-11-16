"""The logs ha_report_issue puts into a report, and what it scrubs from them.

Home Assistant's own log is served by a real Core; the add-on's log comes from
a Supervisor the test runs at ``SUPERVISOR_BASE_URL``, as an add-on reaches
its own. Every log line is redacted before it is cut, so a secret straddling
the cut cannot survive as an unrecognizable fragment.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from aiohttp import web

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.bug_report_redaction import sanitize_log_text
from ha_mcp.utils.usage_logger import log_tool_call

from ._bug_report_core import JWT, record_startup_log, serve_supervisor
from ._bug_report_core import write_error_log as write_error_log
from ._core_client import refused_url
from ._report_issue import report_issue, report_line

# A secret path in the shape the server generates for its connect URL.
SECRET_PATH = "/private_T6tSJxqYuTESTtoken"

# Many tokens: far longer than any log cap as written, far shorter once each
# is redacted. Cut first, a fragment of one would survive unredacted.
MANY_TOKENS = "".join(f"token {JWT}\n" for _ in range(100))


def _logs(text: str) -> Any:
    async def own_logs(request: web.Request) -> web.Response:
        return web.Response(text=text)

    return own_logs


class TestHomeAssistantLog:
    async def test_log_reaches_the_report_with_its_secrets_scrubbed(
        self, write_error_log: Callable[[str], None], core_client: HomeAssistantClient
    ) -> None:
        """home-assistant.log carries the auth and integration errors that
        took several rounds to diagnose in #1694, and the secrets next to
        them."""
        write_error_log(
            "invalid authentication from localhost\n"
            "InsecureKeyLengthWarning: The HMAC key is 0 bytes long\n"
            f"Bearer {JWT}\n"
            "token=deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\n"
            "client_secret: kX9pQ4mZ2vL8nR3sT6uW1yA5cB7dF0gH\n"
            "http://user:hunter2@ha:8123/private_axAloe89imJfMt8RgkwuVA\n"
        )

        report = await report_issue(core_client)

        log = report["core_error_log"]
        assert "invalid authentication" in log
        assert "InsecureKeyLengthWarning" in log
        for surface in ("formatted_report", "issue_body"):
            assert "Home Assistant Error Log" in report[surface]
            assert "InsecureKeyLengthWarning" in report[surface]
        for secret in (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
            "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
            "kX9pQ4mZ2vL8nR3sT6uW1yA5cB7dF0gH",
            "hunter2",
            "axAloe89imJfMt8RgkwuVA",
        ):
            assert secret not in log
            assert secret not in report["issue_body"]

    async def test_long_log_keeps_its_most_recent_lines(
        self, write_error_log: Callable[[str], None], core_client: HomeAssistantClient
    ) -> None:
        write_error_log("OLDEST_LINE\n" + ("x" * 99 + "\n") * 200 + "TAIL_MARKER\n")

        log = (await report_issue(core_client))["core_error_log"]

        assert "TAIL_MARKER" in log
        assert "OLDEST_LINE" not in log
        assert "truncated" in log

    async def test_unserved_log_leaves_no_empty_section(
        self, core_client: HomeAssistantClient
    ) -> None:
        """Core serves no log without a log file, and a role too low gets a
        403: the report still comes, without a dangling header."""
        report = await report_issue(core_client)

        assert report["core_error_log"] == ""
        assert "Home Assistant Error Log" not in report["formatted_report"]
        assert "Home Assistant Error Log" not in report["issue_body"]


class TestAddOnLog:
    async def test_add_on_reads_its_own_log_from_the_supervisor(
        self,
        core_client: HomeAssistantClient,
        aiohttp_client: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The add-on's container log is the one place its own startup
        errors appear; colour codes and addresses come out of it."""
        seen: list[tuple[str, str]] = []

        async def own_logs(request: web.Request) -> web.Response:
            seen.append((request.path, request.headers["Authorization"]))
            return web.Response(
                text="INFO ha_mcp: connected to 127.0.0.1:8123\n"
                "\x1b[31mERROR\x1b[0m: fail\n"
            )

        await serve_supervisor(aiohttp_client, monkeypatch, own_logs)

        report = await report_issue(core_client)

        assert seen == [("/addons/self/logs", "Bearer supervisor-token")]
        log = report["addon_logs"]
        assert "ha_mcp: connected to [IP]" in log
        assert "127.0.0.1" not in log
        assert "\x1b[" not in log
        assert "truncated" not in log
        for surface in ("formatted_report", "issue_body"):
            assert "Add-on Container Logs" in report[surface]
        assert report_line(report, "Installation Method") == "addon"

    async def test_long_log_keeps_its_most_recent_lines(
        self,
        core_client: HomeAssistantClient,
        aiohttp_client: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await serve_supervisor(
            aiohttp_client,
            monkeypatch,
            _logs("OLDEST_LINE\n" + "x" * 5_000 + "\nTAIL_MARKER"),
        )

        log = (await report_issue(core_client))["addon_logs"]

        assert log.startswith("[...truncated")
        assert log.endswith("TAIL_MARKER")
        assert "OLDEST_LINE" not in log

    async def test_log_is_redacted_before_it_is_cut(
        self,
        core_client: HomeAssistantClient,
        aiohttp_client: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await serve_supervisor(aiohttp_client, monkeypatch, _logs(MANY_TOKENS))

        log = (await report_issue(core_client))["addon_logs"]

        assert "truncated" not in log
        assert log.count("[REDACTED_JWT]") == 100
        assert "eyJ" not in log

    async def test_refused_log_read_is_logged_with_its_status(
        self,
        core_client: HomeAssistantClient,
        aiohttp_client: Any,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The status tells a 4xx/5xx apart from "Supervisor unreachable" or
        "not an add-on"."""

        async def forbidden(request: web.Request) -> web.Response:
            return web.Response(status=403, text="Forbidden")

        await serve_supervisor(aiohttp_client, monkeypatch, forbidden)

        with caplog.at_level("INFO", logger="ha_mcp.tools.bug_report_sources"):
            report = await report_issue(core_client)

        assert report["addon_logs"] == ""
        assert any("403" in record.getMessage() for record in caplog.records)

    async def test_unreachable_supervisor_leaves_the_log_empty(
        self, core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SUPERVISOR_TOKEN", "supervisor-token")
        monkeypatch.setenv("SUPERVISOR_BASE_URL", refused_url())

        report = await report_issue(core_client)

        assert report["success"] is True
        assert report["addon_logs"] == ""

    async def test_empty_supervisor_token_is_not_an_add_on(
        self, core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SUPERVISOR_TOKEN", "")

        report = await report_issue(core_client)

        assert report_line(report, "Installation Method") != "addon"
        assert report["addon_logs"] == ""
        assert "Add-on Container Logs" not in report["formatted_report"]


class TestServerLogs:
    async def test_startup_log_loses_the_connect_url_secret(
        self, core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GHSA-mc92-ww4q-6fg4: the startup log names the connect URL, whose
        secret path must not reach a pasted public report. Only the report's
        copy is scrubbed; the collected record stays as logged."""
        collector = record_startup_log(
            monkeypatch,
            f"Starting MCP server 'ha-mcp' on http://127.0.0.1:9583{SECRET_PATH}",
        )

        report = await report_issue(core_client)

        for text in (report["startup_logs"][0]["message"], report["formatted_report"]):
            assert SECRET_PATH not in text
            assert "[REDACTED_SECRET_PATH]" in text
        assert SECRET_PATH in collector.get_logs()[0]["message"]

    async def test_recent_tool_calls_are_listed_newest_first_within_the_bound(
        self, core_client: HomeAssistantClient
    ) -> None:
        """``tool_call_count`` sizes the log to the calls since the issue
        started, so older calls stay out and the failing one is in."""
        for _ in range(20):
            log_tool_call("ha_search", {}, execution_time_ms=5, success=True)
        log_tool_call("ha_get_state", {}, execution_time_ms=5, success=True)
        log_tool_call(
            "ha_call_service",
            {},
            execution_time_ms=5,
            success=False,
            error_message="Entity not found",
        )

        report = await report_issue(core_client, tool_call_count=1)

        count = report["log_count"]
        assert 2 <= count < 22
        assert [log["tool_name"] for log in report["recent_logs"][:2]] == [
            "ha_call_service",
            "ha_get_state",
        ]
        formatted = report["formatted_report"]
        assert f"=== Recent Tool Calls ({count} entries) ===" in formatted
        assert "| ha_get_state | OK |" in formatted
        assert "| ha_call_service | FAIL |" in formatted
        assert "## 📊 Recent Tool Calls" in report["issue_body"]

    async def test_call_and_startup_lines_are_redacted_before_they_are_cut(
        self, core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each line of the summaries is cut short; three tokens are longer
        than either cut as written and shorter once redacted."""
        tokens = f"{JWT} {JWT} {JWT}"
        log_tool_call(
            "ha_call_service",
            {},
            execution_time_ms=5,
            success=False,
            error_message=tokens,
        )
        record_startup_log(monkeypatch, tokens)

        formatted = (await report_issue(core_client))["formatted_report"]

        redacted = "[REDACTED_JWT] [REDACTED_JWT] [REDACTED_JWT]"
        assert f"Error: {redacted}" in formatted
        assert f"FastMCP: {redacted}" in formatted
        assert "eyJ" not in formatted


class TestSecretPathRedaction:
    """The connect and settings URLs carry a secret path the report must not
    hand to an MCP client or a public issue."""

    def test_generated_secret_path_is_redacted(self) -> None:
        out = sanitize_log_text(f"on http://127.0.0.1:9583{SECRET_PATH}/mcp")
        assert SECRET_PATH not in out
        assert "[REDACTED_SECRET_PATH]" in out

    def test_configured_secret_path_is_redacted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A custom path has no ``/private_`` shape to fall back on."""
        monkeypatch.setenv("MCP_SECRET_PATH", "/customhook")
        out = sanitize_log_text("connect at http://ha.local:9583/customhook/mcp")
        assert "/customhook" not in out
        assert "[REDACTED_SECRET_PATH]" in out

    @pytest.mark.parametrize("configured", ["/admin_ui", "/admin_ui "])
    def test_configured_settings_path_is_redacted(
        self, monkeypatch: pytest.MonkeyPatch, configured: str
    ) -> None:
        """GHSA-mx64-982r-65vg. The settings UI mounts at the stripped value,
        so a value with stray whitespace must be redacted stripped too."""
        monkeypatch.delenv("MCP_SECRET_PATH", raising=False)
        monkeypatch.setenv("MCP_SETTINGS_SECRET_PATH", configured)
        out = sanitize_log_text(
            "Settings UI available at: http://ha.local:8086/admin_ui/settings"
        )
        assert "/admin_ui" not in out
        assert "[REDACTED_SECRET_PATH]" in out

    def test_default_mcp_path_is_kept(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The low-entropy ``/mcp`` default is no secret; redacting it is noise."""
        monkeypatch.delenv("MCP_SECRET_PATH", raising=False)
        assert "/mcp" in sanitize_log_text("on http://127.0.0.1:9583/mcp")

    def test_short_configured_path_redacts_only_its_own_segment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MCP_SECRET_PATH", "/ha")
        out = sanitize_log_text("Connecting to /happy/path via /ha")
        assert "/happy/path" in out
        assert out.count("[REDACTED_SECRET_PATH]") == 1
