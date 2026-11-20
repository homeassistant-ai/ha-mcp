"""None-mode callback allowlist matching and storage (#2427).

An unlisted callback must never receive an auto-approved code, or the Home
Assistant origin is an open redirector. The sign-in views that enforce it are
in ``test_oauth_redirect_allowlist_authorize.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from custom_components.ha_mcp_tools.oauth_redirect_allowlist import (
    DEFAULT_OAUTH_REDIRECT_ALLOWLIST,
    MAX_OAUTH_CALLBACK_LENGTH,
    OPT_OAUTH_REDIRECT_ALLOWLIST,
    effective_allowlist,
    is_redirect_allowed,
    normalize_allowlist,
    stored_allowlist,
)

CLAUDE = "https://claude.ai/api/mcp/auth_callback"
LOOPBACK = "http://127.0.0.1/callback"

_ROOT = Path(__file__).resolve().parents[3]
_MODULE = "oauth_redirect_allowlist.py"
_COMPONENT_COPY = _ROOT / "custom_components/ha_mcp_tools" / _MODULE
_DEV_PROXY_COPY = (
    _ROOT / "homeassistant-addon-webhook-proxy-dev/mcp_proxy_dev" / _MODULE
)
# Reaches the stable app through the promote workflow, so it may be absent.
_STABLE_PROXY_COPY = _ROOT / "homeassistant-addon-webhook-proxy/mcp_proxy" / _MODULE


def test_webhook_proxy_copies_honour_the_same_callbacks_as_the_component() -> None:
    """A drifted proxy copy would redirect sign-ins the component refuses."""
    expected = _COMPONENT_COPY.read_bytes()
    copies = [_DEV_PROXY_COPY]
    if _STABLE_PROXY_COPY.is_file():
        copies.append(_STABLE_PROXY_COPY)
    assert [p for p in copies if p.read_bytes() != expected] == []


class TestIsRedirectAllowed:
    def test_listed_callback_is_allowed(self) -> None:
        assert is_redirect_allowed(CLAUDE, [CLAUDE])

    def test_unlisted_callback_is_refused(self) -> None:
        assert not is_redirect_allowed("https://evil.example/cb", [CLAUDE])

    @pytest.mark.parametrize(
        "near_miss",
        [
            f"{CLAUDE}/extra",
            f"{CLAUDE}?next=https://evil.example",
            "https://claude.ai.evil.example/api/mcp/auth_callback",
        ],
    )
    def test_a_listed_callback_is_not_a_prefix(self, near_miss: str) -> None:
        assert not is_redirect_allowed(near_miss, [CLAUDE])

    def test_listed_loopback_callback_matches_any_port(self) -> None:
        # RFC 8252 section 7.3: native clients choose a free port per sign-in.
        assert is_redirect_allowed("http://127.0.0.1:61264/callback", [LOOPBACK])

    def test_loopback_port_exception_keeps_the_path_exact(self) -> None:
        assert not is_redirect_allowed("http://127.0.0.1:61264/other", [LOOPBACK])

    def test_loopback_port_exception_keeps_the_host_exact(self) -> None:
        assert not is_redirect_allowed("http://localhost:61264/callback", [LOOPBACK])

    def test_loopback_port_exception_keeps_the_userinfo_exact(self) -> None:
        assert not is_redirect_allowed(
            "http://user@127.0.0.1:61264/callback", [LOOPBACK]
        )

    def test_loopback_port_exception_keeps_the_query_exact(self) -> None:
        assert not is_redirect_allowed(
            "http://127.0.0.1:61264/callback?x=1", [LOOPBACK]
        )

    def test_listed_ipv6_loopback_callback_matches_any_port(self) -> None:
        assert is_redirect_allowed(
            "http://[::1]:61264/callback", ["http://[::1]/callback"]
        )

    def test_https_callbacks_get_no_port_exception(self) -> None:
        assert not is_redirect_allowed(
            "https://claude.ai:8443/api/mcp/auth_callback", [CLAUDE]
        )

    def test_a_listed_callback_with_userinfo_is_refused(self) -> None:
        listed = "https://user@claude.ai/api/mcp/auth_callback"
        assert not is_redirect_allowed(listed, [listed])

    def test_the_default_list_is_all_listable(self) -> None:
        default = list(DEFAULT_OAUTH_REDIRECT_ALLOWLIST)
        assert normalize_allowlist(default) == (default, [])

    def test_claude_code_on_any_port_matches_the_default(self) -> None:
        for host in ("localhost", "127.0.0.1"):
            assert is_redirect_allowed(
                f"http://{host}:54321/callback", DEFAULT_OAUTH_REDIRECT_ALLOWLIST
            )

    def test_a_malformed_callback_is_refused_even_when_listed(self) -> None:
        listed = f"{CLAUDE}#frag"
        assert not is_redirect_allowed(listed, [listed])


class TestEffectiveAllowlist:
    def test_an_entry_never_edited_uses_the_default(self) -> None:
        assert effective_allowlist({}) == list(DEFAULT_OAUTH_REDIRECT_ALLOWLIST)

    def test_an_emptied_list_stays_empty(self) -> None:
        # Removing every entry must not bring the default back.
        assert effective_allowlist({OPT_OAUTH_REDIRECT_ALLOWLIST: []}) == []

    def test_a_saved_list_is_in_force_as_written(self) -> None:
        saved = {OPT_OAUTH_REDIRECT_ALLOWLIST: [CLAUDE, LOOPBACK]}
        assert effective_allowlist(saved) == [CLAUDE, LOOPBACK]

    def test_a_malformed_stored_list_refuses_every_callback(self) -> None:
        # Falling back to the default would widen a security setting silently.
        assert effective_allowlist({OPT_OAUTH_REDIRECT_ALLOWLIST: CLAUDE}) == []

    def test_a_list_with_an_unusable_entry_refuses_every_callback(self) -> None:
        # The writers never store this; a port-varied loopback request would
        # otherwise match the fragment-bearing entry's loopback key.
        saved = {OPT_OAUTH_REDIRECT_ALLOWLIST: [CLAUDE, "http://localhost/cb#frag"]}
        assert effective_allowlist(saved) == []
        assert not is_redirect_allowed(
            "http://localhost:4321/cb", effective_allowlist(saved)
        )

    def test_a_list_with_a_non_string_entry_refuses_every_callback(self) -> None:
        # Its string entries are not trusted either: only validated writers
        # store this option, so any corruption means the list is not theirs.
        assert effective_allowlist({OPT_OAUTH_REDIRECT_ALLOWLIST: [CLAUDE, 1]}) == []


class TestStoredAllowlist:
    def test_submitting_the_default_unchanged_keeps_following_it(self) -> None:
        assert stored_allowlist({}, list(DEFAULT_OAUTH_REDIRECT_ALLOWLIST)) is None

    def test_a_saved_list_stays_saved_even_when_it_equals_the_default(self) -> None:
        options = {OPT_OAUTH_REDIRECT_ALLOWLIST: [CLAUDE, LOOPBACK]}
        default = list(DEFAULT_OAUTH_REDIRECT_ALLOWLIST)
        assert stored_allowlist(options, default) == default

    @pytest.mark.parametrize("entries", [[], [LOOPBACK], [CLAUDE, LOOPBACK]])
    def test_any_other_list_is_saved(self, entries: list[str]) -> None:
        assert stored_allowlist({}, entries) == entries


class TestNormalizeAllowlist:
    def test_trims_and_drops_blank_and_repeated_entries(self) -> None:
        entries, invalid = normalize_allowlist([f"  {CLAUDE} ", "", CLAUDE, LOOPBACK])
        assert (entries, invalid) == ([CLAUDE, LOOPBACK], [])

    @pytest.mark.parametrize(
        "bad",
        [
            "http://claude.ai/cb",
            "claude.ai/cb",
            f"{CLAUDE}#x",
            "javascript:alert(1)",
            "https://user:secret@claude.ai/api/mcp/auth_callback",
        ],
    )
    def test_reports_entries_no_request_could_use(self, bad: str) -> None:
        assert normalize_allowlist([CLAUDE, bad]) == ([CLAUDE], [bad])

    def test_reports_an_over_long_callback(self) -> None:
        long = "https://example.com/" + "a" * MAX_OAUTH_CALLBACK_LENGTH
        assert normalize_allowlist([long]) == ([], [long])
