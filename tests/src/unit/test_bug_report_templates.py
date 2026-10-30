"""Unit tests for the ha_report_issue rendering helpers (bug_report_templates)."""

from ha_mcp.tools.bug_report_templates import (
    _format_client_host_for_template,
    _format_client_info_for_template,
    _format_config_toggles_for_template,
    _generate_search_keywords,
)


class TestFormatConfigTogglesForTemplate:
    """Tests for _format_config_toggles_for_template."""

    def test_empty_returns_placeholder(self):
        assert (
            _format_config_toggles_for_template({}) == "_(config toggles unavailable)_"
        )

    def test_renders_bullets_with_value(self):
        rendered = _format_config_toggles_for_template(
            {"enable_tool_search": False, "tool_search_max_results": 5}
        )
        assert "- **enable_tool_search:** `False`" in rendered
        assert "- **tool_search_max_results:** `5`" in rendered


class TestFormatClientInfoForTemplate:
    """Tests for _format_client_info_for_template."""

    def test_empty_renders_unknown_placeholder(self):
        rendered = _format_client_info_for_template({})
        assert "unknown" in rendered
        # Phrasing describes the observable, not the underlying API field name,
        # so the message stays accurate if MCP renames the attribute.
        assert "did not advertise" in rendered

    def test_name_and_version_render_as_single_line(self):
        rendered = _format_client_info_for_template(
            {"name": "Claude Desktop", "version": "0.7.42", "title": ""}
        )
        assert rendered == "Claude Desktop 0.7.42"

    def test_distinct_title_appears_as_aside(self):
        rendered = _format_client_info_for_template(
            {
                "name": "ClaudeDesktop",
                "version": "0.7.42",
                "title": "Claude Desktop (claude.ai integration)",
            }
        )
        assert "ClaudeDesktop 0.7.42" in rendered
        assert "Claude Desktop (claude.ai integration)" in rendered

    def test_title_matching_name_is_not_repeated(self):
        # Many clients send title == name; don't print the same string twice.
        rendered = _format_client_info_for_template(
            {"name": "Cursor", "version": "0.42", "title": "Cursor"}
        )
        assert rendered == "Cursor 0.42"


class TestFormatClientHostForTemplate:
    """The `MCP Client Host:` row — what the server learned beyond clientInfo."""

    def test_stdio_host_detected_renders_name_and_version(self):
        line = _format_client_host_for_template(
            {
                "mcp_transport": "stdio",
                "mcp_client_info": {"name": "local-agent-mode-Home Assistant"},
                "mcp_client_host": {"name": "Claude Desktop", "version": "2.110.0"},
            }
        )
        assert line == (
            "Claude Desktop (local agent mode); "
            "Claude Desktop 2.110.0 _(from the parent process)_"
        )

    def test_stdio_without_host_says_not_detected(self):
        line = _format_client_host_for_template(
            {"mcp_transport": "stdio", "mcp_client_info": {"name": "cursor-vscode"}}
        )
        assert line == "not detected"

    def test_local_agent_mode_prefix_is_labelled_even_when_host_unknown(self):
        line = _format_client_host_for_template(
            {
                "mcp_transport": "stdio",
                "mcp_client_info": {"name": "local-agent-mode-home-assistant"},
            }
        )
        assert line == "Claude Desktop (local agent mode); not detected"

    def test_http_renders_user_agent_alongside_client_info(self):
        line = _format_client_host_for_template(
            {
                "mcp_transport": "http",
                "mcp_client_info": {"name": "claude-ai", "title": ""},
                "http_user_agent": "Claude-User (+https://docs.anthropic.com/claude-code)",
            }
        )
        assert (
            line == "User-Agent `Claude-User (+https://docs.anthropic.com/claude-code)`"
        )

    def test_http_user_agent_not_repeated_when_it_already_is_the_client_info(self):
        # The clientInfo fallback already parsed User-Agent into name/version.
        line = _format_client_host_for_template(
            {
                "mcp_transport": "http",
                "mcp_client_info": {
                    "name": "python-httpx",
                    "version": "0.28.1",
                    "title": "from HTTP User-Agent",
                },
                "http_user_agent": "python-httpx/0.28.1",
            }
        )
        assert line == "not detected"

    def test_empty_diagnostics_say_not_detected(self):
        assert _format_client_host_for_template({}) == "not detected"

    def test_python_sdk_default_identity_is_flagged_as_a_bridge(self):
        # Observed live: Claude Desktop -> fastmcp-remote -> component shows
        # up as the Python MCP SDK's default clientInfo, hiding Desktop.
        line = _format_client_host_for_template(
            {
                "mcp_transport": "http",
                "mcp_client_info": {"name": "mcp", "version": "0.1.0", "title": ""},
                "http_user_agent": "python-httpx/0.28.1",
            }
        )
        assert line.startswith("stdio bridge (Python MCP SDK default identity")
        assert "ask the user which app and version launched the bridge" in line
        assert "User-Agent `python-httpx/0.28.1`" in line

    def test_mcp_name_with_a_real_version_is_not_a_bridge(self):
        # Only the SDK's literal default (mcp 0.1.0) marks a bridge; a client
        # that calls itself "mcp" with its own version is reported as-is.
        line = _format_client_host_for_template(
            {
                "mcp_transport": "http",
                "mcp_client_info": {"name": "mcp", "version": "2.3.0", "title": ""},
            }
        )
        assert "stdio bridge" not in line
        assert line == "not detected"

    def test_named_bridges_are_flagged(self):
        line = _format_client_host_for_template(
            {"mcp_transport": "http", "mcp_client_info": {"name": "mcp-remote"}}
        )
        assert "stdio bridge (mcp-remote bridge)" in line


def test_feature_request_duplicate_search_uses_its_title() -> None:
    """A feature request has no error to search by; falling back to "bug"
    or to the session's last failed call would never find an earlier
    request for the same feature."""
    failed = [{"tool_name": "ha_call_service", "error_message": "Service not found"}]
    assert _generate_search_keywords({}, failed, "Manage Thread datasets") == [
        "Manage Thread datasets"
    ]
    assert "ha_call_service" in _generate_search_keywords({}, failed)
    # A blank title falls back to the usual keywords instead of an empty term.
    assert sorted(_generate_search_keywords({}, failed, "   ")) == sorted(
        _generate_search_keywords({}, failed)
    )
