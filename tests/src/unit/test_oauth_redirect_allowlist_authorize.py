"""The none-mode ``/authorize`` view enforcing the callback allowlist (#2427).

An anonymous visitor must never be bounced to a callback the administrator did
not list, and a refusal must be readable in the log without being forgeable.
"""

from __future__ import annotations

import itertools
import logging
import time
import uuid

import pytest
from aiohttp import ClientResponse
from homeassistant.core import HomeAssistant

from custom_components.ha_mcp_tools import oauth_autoapprove, oauth_dcr
from custom_components.ha_mcp_tools.const import (
    OPT_OAUTH_ALLOW_ANY_CALLBACK,
    OPT_OAUTH_REDIRECT_ALLOWLIST,
    WEBHOOK_AUTH_HA,
    WEBHOOK_AUTH_NONE,
)

from ._oauth_autoapprove_core import (
    AUTHORIZE_URL,
    CLAUDE_REDIRECT,
    DCR_KEY,
    OAuthSurface,
    OpenSurface,
    authorize_query,
)
from ._oauth_autoapprove_core import open_oauth_surface as open_oauth_surface

CHATGPT = "https://chatgpt.example/connector/cb"
REFUSED_LINE = "not on the OAuth callback list"
_runs = itertools.count(1)


class RefusalClock:
    """The clock the refusal log's rate limit reads, moved only by the test."""

    def __init__(self, now: float) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def refusal_clock(monkeypatch: pytest.MonkeyPatch) -> RefusalClock:
    """Each test starts a full interval after the previous refusal was logged."""
    clock = RefusalClock(time.monotonic() + next(_runs) * 100_000.0)
    monkeypatch.setattr(oauth_autoapprove, "time", clock)
    return clock


async def authorize(surface: OAuthSurface, **overrides: str) -> ClientResponse:
    """GET ``/authorize`` for the Claude callback unless overridden, unfollowed."""
    return await surface.client.get(
        AUTHORIZE_URL, params=authorize_query(**overrides), allow_redirects=False
    )


def refusal_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if REFUSED_LINE in r.getMessage()]


async def test_an_unlisted_callback_is_refused_without_redirecting(
    open_oauth_surface: OpenSurface,
) -> None:
    """An anonymous /authorize must not bounce a visitor to any URL (#2427)."""
    surface = await open_oauth_surface(WEBHOOK_AUTH_NONE)

    resp = await authorize(surface, redirect_uri=CHATGPT)

    assert resp.status == 400
    assert "Location" not in resp.headers


async def test_a_listed_callback_is_redirected_to(
    open_oauth_surface: OpenSurface,
) -> None:
    """The same request goes through once the administrator lists the callback."""
    surface = await open_oauth_surface(
        WEBHOOK_AUTH_NONE, options={OPT_OAUTH_REDIRECT_ALLOWLIST: [CHATGPT]}
    )

    resp = await authorize(surface, redirect_uri=CHATGPT)

    assert resp.status == 302
    assert resp.headers["Location"].startswith(CHATGPT)


async def test_a_listed_loopback_callback_is_redirected_to_on_any_port(
    open_oauth_surface: OpenSurface,
) -> None:
    """Native/CLI loopback callbacks (RFC 8252) pick a fresh port per sign-in."""
    surface = await open_oauth_surface(
        WEBHOOK_AUTH_NONE,
        options={OPT_OAUTH_REDIRECT_ALLOWLIST: ["http://localhost/callback"]},
    )

    resp = await authorize(surface, redirect_uri="http://localhost:61264/callback")

    assert resp.status == 302
    assert "code=" in resp.headers["Location"]


async def test_an_allowlist_edit_applies_to_the_next_sign_in(
    open_oauth_surface: OpenSurface, hass: HomeAssistant
) -> None:
    """An administrator's options edit applies with no reload or re-registration."""
    surface = await open_oauth_surface(
        WEBHOOK_AUTH_NONE, options={OPT_OAUTH_REDIRECT_ALLOWLIST: []}
    )
    assert (await authorize(surface, redirect_uri=CHATGPT)).status == 400

    hass.config_entries.async_update_entry(
        surface.entry, options={OPT_OAUTH_REDIRECT_ALLOWLIST: [CHATGPT]}
    )

    assert (await authorize(surface, redirect_uri=CHATGPT)).status == 302


async def test_dynamic_registration_cannot_add_a_callback(
    open_oauth_surface: OpenSurface,
) -> None:
    """A client registering its own callback must not get past the allowlist."""
    callback = "https://evil.example/cb"
    surface = await open_oauth_surface(WEBHOOK_AUTH_NONE)

    resp = await authorize(
        surface,
        client_id=oauth_dcr.mint_client_id(DCR_KEY, [callback]),
        redirect_uri=callback,
    )

    assert resp.status == 400
    assert "Location" not in resp.headers


async def test_the_refusal_page_escapes_the_callback(
    open_oauth_surface: OpenSurface,
) -> None:
    """The refusal page shows the callback to the user, so it must not run it."""
    surface = await open_oauth_surface(WEBHOOK_AUTH_NONE)

    resp = await authorize(surface, redirect_uri="https://evil.example/<img src=x>")

    body = await resp.text()
    assert "<img src=x>" not in body
    assert "&lt;img src=x&gt;" in body
    # Defence in depth: nothing on the page may run, and it is never cached.
    csp = resp.headers["Content-Security-Policy"]
    assert "default-src 'none'" in csp
    assert "script-src" not in csp
    assert "no-store" in resp.headers["Cache-Control"]


async def test_a_refusal_is_logged_with_the_callback(
    open_oauth_surface: OpenSurface, caplog: pytest.LogCaptureFixture
) -> None:
    """Whoever reads the log can tell why a client keeps failing to sign in."""
    surface = await open_oauth_surface(WEBHOOK_AUTH_NONE)

    with caplog.at_level(logging.WARNING):
        await authorize(surface, redirect_uri=CHATGPT)

    assert any(CHATGPT in line for line in refusal_lines(caplog))


async def test_a_burst_of_refusals_writes_one_line_then_a_count(
    open_oauth_surface: OpenSurface,
    caplog: pytest.LogCaptureFixture,
    refusal_clock: RefusalClock,
) -> None:
    """The view is anonymous: a burst of refusals writes one line, then a count."""
    surface = await open_oauth_surface(WEBHOOK_AUTH_NONE)

    with caplog.at_level(logging.WARNING):
        for n in range(5):
            await authorize(surface, redirect_uri=f"https://chatgpt.example/cb{n}")
        assert len(refusal_lines(caplog)) == 1
        refusal_clock.now += 3600
        await authorize(surface, redirect_uri="https://chatgpt.example/last")

    lines = refusal_lines(caplog)
    assert len(lines) == 2
    assert "4 more refused since the last such line" in lines[1]
    assert "chatgpt.example/last" in lines[1]


async def test_a_refusal_log_cannot_be_forged(
    open_oauth_surface: OpenSurface, caplog: pytest.LogCaptureFixture
) -> None:
    """A terminal escape in the callback must not reach the log raw (Core's own
    filter already drops requests carrying a newline)."""
    surface = await open_oauth_surface(WEBHOOK_AUTH_NONE)

    with caplog.at_level(logging.WARNING):
        await authorize(surface, redirect_uri="https://evil.example/cb\x1b[31mRED")

    refusals = refusal_lines(caplog)
    assert len(refusals) == 1
    assert "\x1b" not in refusals[0]
    assert "\\x1b[31mRED" in refusals[0]


async def test_a_malformed_stored_list_refuses_every_callback_and_says_so(
    open_oauth_surface: OpenSurface, caplog: pytest.LogCaptureFixture
) -> None:
    """Falling back to the default would widen a security setting silently."""
    surface = await open_oauth_surface(
        WEBHOOK_AUTH_NONE,
        options={OPT_OAUTH_REDIRECT_ALLOWLIST: f"{CLAUDE_REDIRECT}/{uuid.uuid4().hex}"},
    )

    with caplog.at_level(logging.WARNING):
        resp = await authorize(surface)

    assert resp.status == 400
    assert "malformed OAuth callback allowlist (str)" in caplog.text


async def test_a_malformed_list_is_logged_once_without_its_contents(
    open_oauth_surface: OpenSurface, caplog: pytest.LogCaptureFixture
) -> None:
    """/authorize reads the list on every anonymous request."""
    secret = f"https://listed.example/{uuid.uuid4().hex}"
    surface = await open_oauth_surface(
        WEBHOOK_AUTH_NONE,
        options={
            OPT_OAUTH_REDIRECT_ALLOWLIST: [secret, f"http://localhost/{secret[-8:]}#f"]
        },
    )

    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            assert (await authorize(surface)).status == 400

    lines = [
        r.getMessage() for r in caplog.records if "Ignoring a malformed" in r.message
    ]
    assert len(lines) == 1
    assert "2 entries" in lines[0]
    assert secret not in caplog.text


async def test_the_switch_that_turns_the_list_off_accepts_any_listable_callback(
    open_oauth_surface: OpenSurface,
) -> None:
    """Configure's switch lets any well-formed callback through; off, it is refused."""
    callbacks = ["https://any-client.example/cb", "http://127.0.0.1:5000/callback"]
    off = await open_oauth_surface(
        WEBHOOK_AUTH_NONE, options={OPT_OAUTH_REDIRECT_ALLOWLIST: []}
    )
    refused = [(await authorize(off, redirect_uri=c)).status for c in callbacks]
    off.hass.config_entries.async_update_entry(
        off.entry,
        options={OPT_OAUTH_REDIRECT_ALLOWLIST: [], OPT_OAUTH_ALLOW_ANY_CALLBACK: True},
    )

    accepted = [(await authorize(off, redirect_uri=c)).status for c in callbacks]

    assert refused == [400, 400]
    assert accepted == [302, 302]


async def test_the_list_applies_while_the_switch_is_explicitly_off(
    open_oauth_surface: OpenSurface,
) -> None:
    """With the switch saved as off, only a listed callback is accepted."""
    surface = await open_oauth_surface(
        WEBHOOK_AUTH_NONE,
        options={
            OPT_OAUTH_REDIRECT_ALLOWLIST: [CLAUDE_REDIRECT],
            OPT_OAUTH_ALLOW_ANY_CALLBACK: False,
        },
    )

    listed = await authorize(surface)
    other = await authorize(surface, redirect_uri="https://any-client.example/cb")

    assert (listed.status, other.status) == (302, 400)


@pytest.mark.parametrize(
    "callback",
    [
        "http://evil.example/cb",
        "https://user@evil.example/cb",
        "https://evil.example/cb#frag",
        "https://evil.example/cb#",
        "javascript:alert(1)",
    ],
)
async def test_the_switch_still_refuses_a_malformed_callback(
    open_oauth_surface: OpenSurface, callback: str
) -> None:
    """Turning the list off never admits a callback no list could hold."""
    surface = await open_oauth_surface(
        WEBHOOK_AUTH_NONE, options={OPT_OAUTH_ALLOW_ANY_CALLBACK: True}
    )

    resp = await authorize(surface, redirect_uri=callback)

    assert resp.status == 400
    assert "Location" not in resp.headers


async def test_ha_auth_mode_still_accepts_a_callback_with_userinfo(
    open_oauth_surface: OpenSurface,
) -> None:
    """Legacy, ha_auth and DCR keep their shared floor; only none mode's list
    refuses userinfo."""
    surface = await open_oauth_surface(WEBHOOK_AUTH_HA)

    resp = await authorize(
        surface,
        client_id="https://claude.ai",
        redirect_uri="https://user@claude.ai/api/mcp/auth_callback",
    )

    assert resp.status == 302
    assert resp.headers["Location"].startswith("/auth/authorize?")
