"""ha_report_issue E2E tests against a live Home Assistant."""

import logging

import pytest

from ...utilities.assertions import (
    extract_error_message,
    parse_mcp_result,
)

logger = logging.getLogger(__name__)


@pytest.mark.system
class TestReportIssueE2E:
    """E2E coverage for ha_report_issue guidance against a live HA instance.

    Proves the tool actually hands back the missing-tool refresh hint and the
    beta-features flag when invoked end-to-end (issue #1804). It cannot prove an
    LLM will read or act on the hint — only that the server returns it.
    """

    @pytest.mark.asyncio
    async def test_report_issue_surfaces_hint_and_beta_flag(self, mcp_client):
        """ha_report_issue returns the missing-tool hint (field + pre-check) and
        reports the beta master flag, verified against a real server."""
        logger.info("Calling ha_report_issue against live HA...")

        result = await mcp_client.call_tool("ha_report_issue", {})
        data = parse_mcp_result(result)

        # The tool always succeeds (diagnostics are best-effort); a failure here
        # is a real regression, not a degraded-environment skip.
        assert data.get("success") is True, (
            f"ha_report_issue failed: {extract_error_message(data)}"
        )

        # 1) The dedicated hint field is present and carries the actual
        #    refresh-your-tool-list guidance — the #1804 fix.
        hint = data.get("missing_tool_hint", "")
        assert hint, "missing_tool_hint field should be present and non-empty"
        hint_lower = hint.lower()
        assert "refresh" in hint_lower and "reconnect" in hint_lower, (
            f"hint should tell the user to refresh/reconnect; got: {hint}"
        )
        assert "tool list" in hint_lower, (
            "hint should name the stale client tool list as the cause"
        )

        # 2) The agent-facing workflow front-loads the pre-check (before the
        #    duplicate-check step), so a missing tool is triaged before filing.
        instructions = data.get("instructions", "")
        assert "PRE-CHECK" in instructions, (
            "instructions should include the missing-tool pre-check step"
        )
        assert instructions.index("PRE-CHECK") < instructions.index(
            "Check for duplicates FIRST"
        ), "pre-check must come before the duplicate-check step"

        # 3) The beta master flag is read from live Settings into the structured
        #    toggle snapshot AND rendered into the report a triager reads.
        toggles = data.get("diagnostic_info", {}).get("config_toggles", {})
        assert "enable_beta_features" in toggles, (
            "config_toggles should report the beta master flag"
        )
        assert isinstance(toggles["enable_beta_features"], bool)
        report = data.get("formatted_report", "")
        assert "enable_beta_features" in report, (
            "formatted_report should render the beta flag in its config toggles"
        )

        # 4) The host-app row is always rendered so a triager can see whether
        #    the launching app (and, for Claude Desktop, its release) was
        #    identified or still has to be asked for. Over stdio the parent
        #    chain is walked; over HTTP it is the User-Agent, if any.
        diag = data.get("diagnostic_info", {})
        assert isinstance(diag.get("mcp_client_host"), dict)
        assert "MCP Client Host:" in report
        assert "**MCP Client Host:**" in data.get("issue_body", "")

        logger.info(
            "ha_report_issue hint + beta flag verified end-to-end "
            f"(enable_beta_features={toggles['enable_beta_features']})"
        )

    @pytest.mark.parametrize(
        ("report_type", "prefix"),
        [("agent_behavior", "[AGENT]"), ("feature_request", "[FEATURE]")],
    )
    @pytest.mark.asyncio
    async def test_report_text_reaches_the_finished_issue(
        self, mcp_client, report_type, prefix
    ):
        """The agent's text passes the tool schema and lands in the finished
        issue."""
        description = "E2E: script created but never registered."
        result = await mcp_client.call_tool(
            "ha_report_issue",
            {
                "report_type": report_type,
                "title": "E2E report title",
                "description": description,
                "fields": "issue_title,issue_body,issue_url",
            },
        )
        data = parse_mcp_result(result)
        assert data.get("success") is True, extract_error_message(data)

        assert data["issue_title"] == f"{prefix} E2E report title"
        assert description in data["issue_body"]
