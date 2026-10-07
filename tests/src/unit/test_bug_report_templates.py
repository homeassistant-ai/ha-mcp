"""Unit tests for the ha_report_issue rendering helpers (bug_report_templates)."""

from ha_mcp.tools.bug_report_templates import (
    _format_client_host_for_template,
    _format_client_info_for_template,
    _format_config_toggles_for_template,
    _generate_search_keywords,
    _sanitize_log_text,
)


class TestSanitizeLogText:
    """Tests for _sanitize_log_text."""

    def test_redacts_jwt_tokens(self):
        text = "auth: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        result = _sanitize_log_text(text)
        assert "eyJ" not in result
        assert "[REDACTED_JWT]" in result

    def test_redacts_bearer_tokens(self):
        text = "Authorization: Bearer abc123secrettoken"
        result = _sanitize_log_text(text)
        assert "abc123" not in result
        assert "Bearer [REDACTED]" in result

    def test_redacts_long_hex_strings(self):
        # Bare hex (no leading "token:" — that path is exercised by the
        # key=value rule below) gets the dedicated hex marker.
        text = f"raw: {'a1b2c3d4' * 5}"  # 40-char hex string
        result = _sanitize_log_text(text)
        assert "a1b2c3d4" not in result
        assert "[REDACTED_HEX]" in result

    def test_redacts_ipv4_with_port(self):
        text = "Connected to 192.168.1.100:8123"
        result = _sanitize_log_text(text)
        assert "192.168.1.100" not in result
        assert "[IP]" in result

    def test_redacts_ipv4_in_url(self):
        text = "fetched https://192.168.1.1/api/states"
        result = _sanitize_log_text(text)
        assert "192.168.1.1" not in result
        assert "[IP]" in result

    def test_redacts_ipv4_after_keyword(self):
        text = "host=10.0.0.5 connecting"
        result = _sanitize_log_text(text)
        assert "10.0.0.5" not in result
        assert "[IP]" in result

    def test_preserves_four_segment_version_strings(self):
        # Regression: bare four-segment values without network context (e.g.
        # version banners) were being mangled by the IPv4 rule.
        text = "ha-mcp version 1.2.3.4 starting"
        result = _sanitize_log_text(text)
        assert result == text

    def test_redacts_key_value_credentials(self):
        cases = [
            ("OPENAI_API_KEY=sk-proj-AbCdEf1234567890qwerty", "sk-proj-AbCdEf"),
            ("token=ghp_AbCdEf1234567890qwerty1234567890qwer", "ghp_AbCdEf"),
            ("password=hunter2-S3cret!", "hunter2"),
            ("api_key: somekey1234", "somekey1234"),
            # JSON and Python dict reprs of an error payload (Codex #2588)
            ('{"token": "abc 123 secret"}', "secret"),
            ("{'password': 'hunter2'}", "hunter2"),
            # Authorization schemes other than Bearer (Codex #2588)
            ("Authorization: Basic dXNlcjpzZWNyZXQ=", "dXNlcjpzZWNyZXQ="),
            ('{"authorization": "Digest abc123"}', "abc123"),
        ]
        for text, secret in cases:
            result = _sanitize_log_text(text)
            assert secret not in result, f"{secret!r} leaked in {result!r}"
            assert "[REDACTED]" in result

    def test_redacts_url_userinfo(self):
        cases = [
            ("https://admin:supersecret@homeassistant.local/api", "supersecret"),
            ("mqtt://user:pass@broker.local:1883", "user:pass"),
        ]
        for text, secret in cases:
            result = _sanitize_log_text(text)
            assert secret not in result, f"{secret!r} leaked in {result!r}"
            assert "[REDACTED]" in result

    def test_a_long_unbroken_log_line_does_not_stall_the_report(self):
        # A base64 blob or minified payload in a log message once took the
        # userinfo rule time quadratic in its length: about a minute at this
        # size, so ha_report_issue appeared to hang. Linear work takes
        # milliseconds; the bound leaves a wide margin for a slow runner.
        import time

        started = time.monotonic()
        _sanitize_log_text("a" * 200_000)
        assert time.monotonic() - started < 5

    def test_redacts_userinfo_after_a_scheme_glued_to_other_text(self):
        result = _sanitize_log_text("url=1https://admin:supersecret@ha.local/api")
        assert "supersecret" not in result

    def test_redacts_legacy_oauth_client_secret_log_line(self):
        # The in-process component logs the legacy OAuth Client Secret at INFO,
        # so it reaches home-assistant.log (readable by a trusted MCP client via
        # ha_get_logs — by design). A shared bug report must still scrub it; the
        # generic "secret:" key=value rule covers the logged line.
        result = _sanitize_log_text("  OAuth Client Secret: s3cr3tV4lue_Zz-99aa")
        assert "s3cr3tV4lue" not in result
        assert "[REDACTED]" in result

    def test_bearer_preserves_casing(self):
        # All casings get redacted via re.IGNORECASE; the lambda echoes
        # m.group(1) so the original casing is preserved in the output.
        cases = [
            ("Authorization: Bearer abc123token", "Bearer [REDACTED]"),
            ("auth: bearer abc123token", "bearer [REDACTED]"),
            ("HDR: BEARER abc123token", "BEARER [REDACTED]"),
            ("hdr: BeArEr abc123token", "BeArEr [REDACTED]"),
        ]
        for text, expected in cases:
            result = _sanitize_log_text(text)
            assert "abc123" not in result, f"abc123 leaked in {result!r}"
            assert expected in result, f"missing {expected!r} in {result!r}"

    def test_handles_multiple_secrets_in_one_line(self):
        text = "token=abc123 connected to 192.168.1.5 with bearer xyz789"
        result = _sanitize_log_text(text)
        assert "abc123" not in result
        assert "xyz789" not in result
        assert "192.168.1.5" not in result
        assert "[REDACTED]" in result
        assert "[IP]" in result

    def test_preserves_normal_text(self):
        text = "2024-12-01 10:00:00 ERROR ha_mcp.server: Entity not found"
        result = _sanitize_log_text(text)
        assert result == text


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
