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

from . import test_oauth_autoapprove as aa_tests  # noqa: E402
from .test_mcp_webhook import (  # noqa: E402
    DATA_WEBHOOK,
    DOMAIN,
    WEBHOOK_AUTH_NONE,
    FakeSession,
    _entry,
    _register_hass,
    mw,
)
from .test_oauth_autoapprove import AUTH_QS, oauth_dcr  # noqa: E402

# The real-aiohttp /authorize harness, shared with the auto-approve suite.
unified_view_client_factory = aa_tests.unified_view_client_factory

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


# ---------------------------------------------------------------------------
# Through the none-mode /authorize view
# ---------------------------------------------------------------------------


async def test_none_mode_refuses_an_unlisted_callback_without_redirecting(
    unified_view_client_factory,
):
    """An anonymous /authorize must not bounce a visitor to any URL (#2427)."""
    client = await unified_view_client_factory(mode="none")
    resp = await client.get(
        "/api/ha_mcp_tools/oauth/authorize"
        + AUTH_QS
        + "&redirect_uri=https%3A%2F%2Fchatgpt.example%2Fconnector%2Fcb",
        allow_redirects=False,
    )
    assert resp.status == 400
    assert "Location" not in resp.headers


async def test_none_mode_refusal_page_escapes_the_callback(
    unified_view_client_factory,
):
    """The refusal page shows the callback to the user, so it must not run it."""
    client = await unified_view_client_factory(mode="none")
    resp = await client.get(
        "/api/ha_mcp_tools/oauth/authorize"
        + AUTH_QS
        + "&redirect_uri=https%3A%2F%2Fevil.example%2F%3Cscript%3Ex%3C%2Fscript%3E",
        allow_redirects=False,
    )
    body = await resp.text()
    assert "<script>x" not in body
    assert "&lt;script&gt;x" in body


async def test_none_mode_follows_allowlist_edits_without_a_reload(
    unified_view_client_factory,
):
    """An administrator's edit applies to the next sign-in, no restart."""
    allowlist: list[str] = []
    client = await unified_view_client_factory(mode="none", allowlist=allowlist)
    url = (
        "/api/ha_mcp_tools/oauth/authorize"
        + AUTH_QS
        + "&redirect_uri=https%3A%2F%2Fchatgpt.example%2Fconnector%2Fcb"
    )
    assert (await client.get(url, allow_redirects=False)).status == 400

    allowlist.append("https://chatgpt.example/connector/cb")

    assert (await client.get(url, allow_redirects=False)).status == 302


async def test_none_mode_dynamic_registration_cannot_add_a_callback(
    unified_view_client_factory,
):
    """A client registering its own callback must not get past the allowlist."""
    key = b"k" * 32
    callback = "https://evil.example/cb"
    client_id = oauth_dcr.mint_client_id(key, [callback])
    client = await unified_view_client_factory(mode="none", dcr_key=key)
    resp = await client.get(
        "/api/ha_mcp_tools/oauth/authorize"
        "?response_type=code&code_challenge=" + "a" * 43 + "&code_challenge_method=S256"
        f"&client_id={client_id}&redirect_uri=https%3A%2F%2Fevil.example%2Fcb",
        allow_redirects=False,
    )
    assert resp.status == 400


async def test_none_mode_listed_loopback_callback_autoapproves_on_any_port(
    unified_view_client_factory,
):
    """Native/CLI loopback callbacks (RFC 8252) pick a fresh port per sign-in."""
    client = await unified_view_client_factory(
        mode="none", allowlist=["http://localhost/callback"]
    )
    resp = await client.get(
        "/api/ha_mcp_tools/oauth/authorize"
        + AUTH_QS
        + "&redirect_uri=http%3A%2F%2Flocalhost%3A61264%2Fcallback",
        allow_redirects=False,
    )
    assert resp.status == 302
    assert "code=" in resp.headers["Location"]


async def test_the_registered_provider_follows_the_entry_allowlist(monkeypatch):
    """An options edit to the callback list applies with no re-registration."""
    hass = _register_hass()
    monkeypatch.setattr(mw.aiohttp, "ClientSession", lambda **kw: FakeSession())
    entry = _entry()
    entry.options = {OPT_OAUTH_REDIRECT_ALLOWLIST: []}
    await mw.async_register_webhook(
        hass,
        entry,
        port=9584,
        secret_path="/private_x",
        auth_mode=WEBHOOK_AUTH_NONE,
    )
    provider = hass.data[DOMAIN][DATA_WEBHOOK][mw.CFG_AUTOAPPROVE_PROVIDER]
    callback = "https://chatgpt.example/cb"
    assert not provider.allows_redirect(callback)

    entry.options = {OPT_OAUTH_REDIRECT_ALLOWLIST: [callback]}

    assert provider.allows_redirect(callback)
