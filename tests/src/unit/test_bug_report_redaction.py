"""Unit tests for ha_report_issue's log scrubber (bug_report_redaction)."""

import pytest

from ha_mcp.tools.bug_report_redaction import (
    _URL_USERINFO_RE,
    KnownIdentifiers,
    _sanitize_log_text,
    known_identifiers_scope,
)


class TestSanitizeLogText:
    """Tests for _sanitize_log_text."""

    def test_redacts_jwt_tokens(self) -> None:
        text = "auth: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        result = _sanitize_log_text(text)
        assert "eyJ" not in result
        assert "[REDACTED_JWT]" in result

    def test_redacts_bearer_tokens(self) -> None:
        text = "Authorization: Bearer abc123secrettoken"
        result = _sanitize_log_text(text)
        assert "abc123" not in result
        assert "Bearer [REDACTED]" in result

    def test_redacts_long_hex_strings(self) -> None:
        # Bare hex (no leading "token:" — that path is exercised by the
        # key=value rule below) gets the dedicated hex marker.
        text = f"raw: {'a1b2c3d4' * 5}"  # 40-char hex string
        result = _sanitize_log_text(text)
        assert "a1b2c3d4" not in result
        assert "[REDACTED_HEX]" in result

    def test_redacts_ipv4_with_port(self) -> None:
        text = "Connected to 192.168.1.100:8123"
        result = _sanitize_log_text(text)
        assert "192.168.1.100" not in result
        assert "[IP]" in result

    def test_redacts_ipv4_in_url(self) -> None:
        text = "fetched https://192.168.1.1/api/states"
        result = _sanitize_log_text(text)
        assert "192.168.1.1" not in result
        assert "[IP]" in result

    def test_redacts_ipv4_after_keyword(self) -> None:
        text = "host=10.0.0.5 connecting"
        result = _sanitize_log_text(text)
        assert "10.0.0.5" not in result
        assert "[IP]" in result

    def test_redacts_quoted_ipv4_after_keyword(self) -> None:
        assert _sanitize_log_text("host='203.0.113.7' refused") == "host='[IP]' refused"
        assert _sanitize_log_text('host="203.0.113.7" refused') == 'host="[IP]" refused'
        result = _sanitize_log_text(
            "HTTPConnectionPool(host='203.0.113.7', port=32400): Max retries exceeded"
        )
        assert "203.0.113.7" not in result
        assert "host='[IP]'" in result
        assert "port=32400" in result

    def test_redacts_dashed_ipv4_in_plex_direct_hostname(self) -> None:
        # Plex's wildcard DNS encodes the server address in the leftmost label.
        hex_label = "0123456789abcdef0123456789abcdef"
        cases = [
            f"https://203-0-113-7.{hex_label}.plex.direct:32400/library",
            f"HTTPSConnectionPool(host='203-0-113-7.{hex_label}.plex.direct', port=32400)",
        ]
        for text in cases:
            result = _sanitize_log_text(text)
            assert "203-0-113-7" not in result, result
            assert "[IP].[REDACTED_HEX].plex.direct" in result, result

    def test_preserves_dashed_numbers_outside_plex_direct(self) -> None:
        for text in ("build 1-2-3-4 starting", "resolved 1-2-3-4.example.com"):
            assert _sanitize_log_text(text) == text

    def test_redacts_lan_hostnames(self) -> None:
        result = _sanitize_log_text("GET http://nas.home.lan:5000/api failed")
        assert result == "GET http://[HOST]:5000/api failed"
        result = _sanitize_log_text("HTTPConnectionPool(host='nas.home.lan', port=80)")
        assert "nas.home.lan" not in result
        assert "host='[HOST]'" in result

    def test_preserves_words_containing_lan(self) -> None:
        for text in ("plan the route", "iface wlan0 up", "foo.language set"):
            assert _sanitize_log_text(text) == text

    def test_lan_and_tailnet_suffixes_must_end_the_hostname(self) -> None:
        # ``lan`` or ``ts.net`` as an inner label is some other domain.
        for text in ("GET nas.home.lan.example.com/x", "GET machine.ts.net.au/x"):
            assert _sanitize_log_text(text) == text

    def test_redacts_tailnet_hostnames(self) -> None:
        for text in (
            "https://machine.tailnet-name.ts.net/api failed",
            "resolving machine.tailnet-name.ts.net failed",
        ):
            result = _sanitize_log_text(text)
            assert "tailnet-name" not in result, result
            assert "[TAILNET_HOST]" in result, result
        assert _sanitize_log_text("visit sports.net") == "visit sports.net"

    def test_redacts_graph_calendar_id(self) -> None:
        text = (
            "GET https://graph.microsoft.com/v1.0/me/calendars/"
            "AAMkAGI2TGuLAAA-x_y=/calendarView?startDateTime=2026-01-01 failed"
        )
        result = _sanitize_log_text(text)
        assert "AAMkAGI2TGuLAAA" not in result
        assert "/me/calendars/[CALENDAR_ID]/calendarView?" in result

    def test_preserves_four_segment_version_strings(self) -> None:
        # Regression: bare four-segment values without network context (e.g.
        # version banners) were being mangled by the IPv4 rule.
        text = "ha-mcp version 1.2.3.4 starting"
        result = _sanitize_log_text(text)
        assert result == text

    def test_redacts_key_value_credentials(self) -> None:
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

    def test_redacts_url_userinfo(self) -> None:
        cases = [
            ("https://admin:supersecret@homeassistant.local/api", "supersecret"),
            ("mqtt://user:pass@broker.local:1883", "user:pass"),
        ]
        for text, secret in cases:
            result = _sanitize_log_text(text)
            assert secret not in result, f"{secret!r} leaked in {result!r}"
            assert "[REDACTED]" in result

    def test_the_userinfo_rule_cannot_start_matching_inside_plain_text(self) -> None:
        # A base64 blob or minified payload in a log message once took the
        # userinfo rule time quadratic in its length, so ha_report_issue
        # appeared to hang: the rule began matching at the scheme, so every
        # character of the word was a candidate start. Anchored on "://" it
        # can only start at the separator, which plain text never contains.
        url = "https://admin:supersecret@ha.local/api"
        assert _URL_USERINFO_RE.match(url) is None
        assert _URL_USERINFO_RE.search(url).start() == len("https")
        assert _URL_USERINFO_RE.search("a" * 200_000) is None

    def test_redacts_userinfo_after_a_scheme_glued_to_other_text(self) -> None:
        result = _sanitize_log_text("url=1https://admin:supersecret@ha.local/api")
        assert "supersecret" not in result

    def test_redacts_legacy_oauth_client_secret_log_line(self) -> None:
        # Older component versions logged the legacy OAuth Client Secret at
        # INFO, and those lines can still sit in home-assistant.log. A shared
        # bug report must scrub them; the generic "secret:" key=value rule
        # covers the logged line.
        result = _sanitize_log_text("  OAuth Client Secret: s3cr3tV4lue_Zz-99aa")
        assert "s3cr3tV4lue" not in result
        assert "[REDACTED]" in result

    def test_bearer_preserves_casing(self) -> None:
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

    def test_handles_multiple_secrets_in_one_line(self) -> None:
        text = "token=abc123 connected to 192.168.1.5 with bearer xyz789"
        result = _sanitize_log_text(text)
        assert "abc123" not in result
        assert "xyz789" not in result
        assert "192.168.1.5" not in result
        assert "[REDACTED]" in result
        assert "[IP]" in result

    def test_preserves_normal_text(self) -> None:
        text = "2024-12-01 10:00:00 ERROR ha_mcp.server: Entity not found"
        result = _sanitize_log_text(text)
        assert result == text


class TestKnownIdentifierRedaction:
    """This instance's own hostnames and addresses, read from Home Assistant,
    are redacted wherever they appear, not only where a regex sees network
    context."""

    IDS = KnownIdentifiers(
        hosts=frozenset({"homeassistant.local", "nas.example"}),
        addresses=frozenset({"172.17.0.3", "fd6a:ebdb:5b2c:4791::6c69"}),
    )

    def _scrub(self, text: str) -> str:
        with known_identifiers_scope(self.IDS):
            return _sanitize_log_text(text)

    def test_bare_known_address_is_redacted(self) -> None:
        """The version-string guard spares a bare address without context."""
        assert self._scrub("Setup failed for 172.17.0.3 now") == (
            "Setup failed for [IP] now"
        )

    def test_known_address_inside_a_longer_number_is_kept(self) -> None:
        for text in ("seen 172.17.0.30 ok", "build 1172.17.0.3 ok"):
            assert self._scrub(text) == text

    def test_known_address_inside_a_longer_hostname_is_kept(self) -> None:
        for text in ("host2 172.17.0.3.example up", "via router-172.17.0.3 up"):
            assert self._scrub(text) == text
        assert self._scrub("ended 172.17.0.3. next") == "ended [IP]. next"

    def test_known_ipv6_after_a_label_colon_is_redacted(self) -> None:
        assert self._scrub("address:fd6a:ebdb:5b2c:4791::6c69 up") == (
            "address:[IP] up"
        )
        # The known address as a prefix of a longer one is a different address.
        text = "peer fd6a:ebdb:5b2c:4791::6c69:1234 up"
        assert self._scrub(text) == text

    def test_known_ipv4_mapped_ipv6_address_is_redacted(self) -> None:
        ids = KnownIdentifiers(addresses=frozenset({"::ffff:192.0.2.1"}))
        with known_identifiers_scope(ids):
            assert _sanitize_log_text("peer2 ::ffff:192.0.2.1 up") == "peer2 [IP] up"
            assert _sanitize_log_text("peer2 ::ffff:192.0.2.10 up") == (
                "peer2 ::ffff:192.0.2.10 up"
            )

    def test_known_ipv6_address_is_redacted_in_any_spelling(self) -> None:
        # Home Assistant reports the compressed form; a log may spell the same
        # address with every zero group written out.
        assert self._scrub("bound to fd6a:ebdb:5b2c:4791::6c69 failed") == (
            "bound to [IP] failed"
        )
        assert (
            self._scrub("bound to fd6a:ebdb:5b2c:4791:0000:0000:0000:6c69 failed")
            == "bound to [IP] failed"
        )
        assert self._scrub("at [FD6A:EBDB:5B2C:4791::6C69]:8123") == "at [[IP]]:8123"

    def test_other_colon_tokens_and_addresses_are_kept(self) -> None:
        text = "2026-10-30 17:07:50 bound to fd6a:ebdb:5b2c:4791::6c6a and ::1"
        assert self._scrub(text) == text

    def test_known_host_is_redacted_before_port_and_quote(self) -> None:
        assert self._scrub("homeassistant.local:8123 refused") == (
            "[HA_HOST]:8123 refused"
        )
        assert self._scrub("Pool(host='nas.example', port=80)") == (
            "Pool(host='[HA_HOST]', port=80)"
        )
        assert self._scrub("could not reach nas.example.") == (
            "could not reach [HA_HOST]."
        )

    def test_known_host_inside_a_longer_name_is_kept(self) -> None:
        for text in ("resolve nas.example.com", "resolve mynas.example"):
            assert self._scrub(text) == text

    def test_known_host_matches_any_case(self) -> None:
        assert self._scrub("GET http://NAS.Example/api") == "GET http://[HA_HOST]/api"

    def test_regex_rules_still_run_after_the_known_identifiers(self) -> None:
        result = self._scrub("172.17.0.3 token=abc123 from 10.0.0.9")
        assert result == "[IP] token=[REDACTED] from [IP]"

    def test_nothing_is_redacted_outside_the_scope(self) -> None:
        with known_identifiers_scope(self.IDS):
            pass
        text = "Setup failed for 172.17.0.3 on nas.example"
        assert _sanitize_log_text(text) == text

    def test_scope_resets_when_its_body_raises(self) -> None:
        def assemble_report() -> str:
            raise RuntimeError("report assembly failed")

        with pytest.raises(RuntimeError), known_identifiers_scope(self.IDS):
            assemble_report()
        assert _sanitize_log_text("on nas.example") == "on nas.example"
