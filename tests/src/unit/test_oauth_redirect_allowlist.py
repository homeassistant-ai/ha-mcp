"""None-mode callback allowlist matching (#2427).

An unlisted callback must never receive an auto-approved code, or the Home
Assistant origin is an open redirector.
"""

from __future__ import annotations

import pytest

from ._embedded_stubs import install

install()

from custom_components.ha_mcp_tools.const import (  # noqa: E402
    DEFAULT_OAUTH_REDIRECT_ALLOWLIST,
    OPT_OAUTH_REDIRECT_ALLOWLIST,
)
from custom_components.ha_mcp_tools.oauth_redirect_allowlist import (  # noqa: E402
    effective_allowlist,
    is_redirect_allowed,
    normalize_allowlist,
)

CLAUDE = "https://claude.ai/api/mcp/auth_callback"
LOOPBACK = "http://127.0.0.1/callback"


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
        # RFC 8252 §7.3: native clients choose a free port per sign-in.
        assert is_redirect_allowed("http://127.0.0.1:61264/callback", [LOOPBACK])

    def test_loopback_port_exception_keeps_the_path_exact(self) -> None:
        assert not is_redirect_allowed("http://127.0.0.1:61264/other", [LOOPBACK])

    def test_loopback_port_exception_keeps_the_host_exact(self) -> None:
        assert not is_redirect_allowed("http://localhost:61264/callback", [LOOPBACK])

    def test_https_callbacks_get_no_port_exception(self) -> None:
        assert not is_redirect_allowed(
            "https://claude.ai:8443/api/mcp/auth_callback", [CLAUDE]
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


class TestNormalizeAllowlist:
    def test_trims_and_drops_blank_and_repeated_entries(self) -> None:
        entries, invalid = normalize_allowlist([f"  {CLAUDE} ", "", CLAUDE, LOOPBACK])
        assert (entries, invalid) == ([CLAUDE, LOOPBACK], [])

    @pytest.mark.parametrize(
        "bad",
        ["http://claude.ai/cb", "claude.ai/cb", f"{CLAUDE}#x", "javascript:alert(1)"],
    )
    def test_reports_entries_no_request_could_use(self, bad: str) -> None:
        assert normalize_allowlist([CLAUDE, bad]) == ([CLAUDE], [bad])
