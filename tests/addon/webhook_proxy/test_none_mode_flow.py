"""The none-mode auto-approve OAuth flow, as an MCP client runs it.

Discovery, the invisible authorize redirect, its callback allowlist and the
PKCE token exchange are all made over Home Assistant's real HTTP server
against a proxy Core set up with OAuth off.
"""

from __future__ import annotations

import html
import logging
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from aiohttp import ClientResponse
from aiohttp.test_utils import TestClient
from homeassistant.core import HomeAssistant
from pytest_homeassistant.typing import ClientSessionGenerator

from ._auth_modes import (
    CLAUDE_REDIRECT,
    as_document_urls,
    fetch_json,
    pkce_pair,
    proxy_config,
    submodule,
)
from ._proxy import start_proxy
from ._variants import Variant, callback_allowlist_supported

OTHER_CALLBACK = "https://other.example/cb"
REFUSED_LINE = "Allowed OAuth callback URLs option"


@pytest.fixture
async def proxy_client(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
) -> TestClient:
    """A client of a proxy running with OAuth off."""
    await start_proxy(hass, variant, fs_root, proxy_config())
    return await hass_client_no_auth()


async def _authorize(
    proxy_client: TestClient,
    variant: Variant,
    challenge: str,
    **overrides: str,
) -> ClientResponse:
    params = {
        "response_type": "code",
        "client_id": "https://claude.ai/oauth/mcp-oauth-client-metadata",
        "redirect_uri": CLAUDE_REDIRECT,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "st-1",
        **overrides,
    }
    return await proxy_client.get(
        f"{variant.oauth_base}/authorize",
        params=params,
        headers={"Host": "ha.example.com"},
        allow_redirects=False,
    )


def _redirect_query(response: ClientResponse) -> dict[str, list[str]]:
    assert response.status == 302
    return parse_qs(urlparse(response.headers["Location"]).query)


def _require_allowlist(variant: Variant) -> None:
    if not callback_allowlist_supported(variant):
        pytest.skip("the callback allowlist rides the dev flavor until promote")


async def _client_with(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    variant: Variant,
    fs_root: Path,
    **extra: Any,
) -> TestClient:
    """A client of a proxy with OAuth off and ``extra`` in its config file."""
    await start_proxy(hass, variant, fs_root, proxy_config(**extra))
    return await hass_client_no_auth()


async def _status_for(
    client: TestClient, variant: Variant, redirect_uri: str
) -> tuple[int, str | None]:
    """Authorize's status and ``Location`` for ``redirect_uri``."""
    _verifier, challenge = pkce_pair()
    response = await _authorize(client, variant, challenge, redirect_uri=redirect_uri)
    return response.status, response.headers.get("Location")


def _refusal_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if REFUSED_LINE in r.getMessage()]


async def _token(
    proxy_client: TestClient, variant: Variant, code: str, verifier: str
) -> ClientResponse:
    return await proxy_client.post(
        f"{variant.oauth_base}/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CLAUDE_REDIRECT,
            "code_verifier": verifier,
        },
    )


async def test_none_mode_document_advertises_the_proxys_public_pkce_server(
    proxy_client: TestClient, variant: Variant
) -> None:
    """The document HA core's own root discovery lacks (#1969): the proxy's
    endpoints, a public PKCE client (``none``), CIMD, DCR, and RFC 9207
    ``iss`` on authorization responses."""
    status, doc = await fetch_json(
        proxy_client, as_document_urls(variant)[0], "x.example"
    )

    base = f"http://x.example{variant.oauth_base}"
    assert status == 200
    assert doc == {
        "issuer": base,
        "authorization_response_iss_parameter_supported": True,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "client_id_metadata_document_supported": True,
    }


async def test_authorize_redirects_straight_back_and_the_code_buys_a_token(
    proxy_client: TestClient, variant: Variant
) -> None:
    """Authorize renders nothing and 302s back with a code and the state; the
    code plus its PKCE verifier, with no client secret, buys a bearer that
    must not be cached and comes with no refresh token."""
    verifier, challenge = pkce_pair()

    query = _redirect_query(await _authorize(proxy_client, variant, challenge))
    assert query["state"] == ["st-1"]
    response = await _token(proxy_client, variant, query["code"][0], verifier)

    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Pragma"] == "no-cache"
    body: dict[str, Any] = await response.json()
    assert body["token_type"] == "Bearer"
    assert body["access_token"]
    assert isinstance(body["expires_in"], int)
    assert "refresh_token" not in body


@pytest.mark.parametrize("store_full", [False, True], ids=["code", "error"])
async def test_authorization_responses_carry_the_advertised_rfc9207_iss(
    proxy_client: TestClient,
    component: ModuleType,
    variant: Variant,
    monkeypatch: pytest.MonkeyPatch,
    store_full: bool,
) -> None:
    """RFC 9207 §2: success and error redirects both name the issuer that
    produced them, exactly as the metadata document advertises it."""
    if store_full:
        # The pending-code store at its abuse cap answers temporarily_unavailable.
        monkeypatch.setattr(submodule(component, "oauth"), "MAX_PENDING_CODES", 0)
    _verifier, challenge = pkce_pair()

    query = _redirect_query(await _authorize(proxy_client, variant, challenge))

    _status, doc = await fetch_json(
        proxy_client, as_document_urls(variant)[0], "ha.example.com"
    )
    assert ("error" in query) is store_full
    assert ("code" in query) is not store_full
    assert query["iss"] == [doc["issuer"]]


async def test_unlisted_callback_gets_an_explanation_page_not_a_redirect(
    proxy_client: TestClient,
    component: ModuleType,
    variant: Variant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An anonymous authorize that redirected anywhere would make the Home
    Assistant origin an open redirector: an unlisted callback gets a 400 page
    naming it, escaped, uncached and unscriptable, and no code. Stable predates
    the list and still redirects."""
    # One pending code at most: a refusal that issued one would starve the
    # listed request below.
    monkeypatch.setattr(submodule(component, "oauth"), "MAX_PENDING_CODES", 1)
    callback = f"{OTHER_CALLBACK}?next=<img src=x>"
    _verifier, challenge = pkce_pair()

    response = await _authorize(proxy_client, variant, challenge, redirect_uri=callback)

    if not callback_allowlist_supported(variant):
        assert response.status == 302
        assert response.headers["Location"].startswith(f"{OTHER_CALLBACK}?")
        return
    assert response.status == 400
    assert "Location" not in response.headers
    page = await response.text()
    assert html.escape(callback) in page
    assert "<img src=x>" not in page
    assert "no-store" in response.headers["Cache-Control"]
    csp = response.headers["Content-Security-Policy"]
    assert "default-src 'none'" in csp
    assert "script-src" not in csp
    assert "code" in _redirect_query(await _authorize(proxy_client, variant, challenge))


async def test_a_configured_list_replaces_the_shipped_defaults(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
) -> None:
    """The app's option is the whole list, not an addition: its entry signs
    in, and claude.ai's default callback is then refused."""
    _require_allowlist(variant)
    client = await _client_with(
        hass,
        hass_client_no_auth,
        variant,
        fs_root,
        oauth_redirect_allowlist=[OTHER_CALLBACK],
    )

    listed = await _status_for(client, variant, OTHER_CALLBACK)
    default = await _status_for(client, variant, CLAUDE_REDIRECT)

    assert listed[0] == 302
    assert default == (400, None)


@pytest.mark.parametrize(
    ("redirect_uri", "accepted"),
    [
        ("http://127.0.0.1:61264/cli-callback", True),
        ("http://127.0.0.1:61264/elsewhere", False),
    ],
    ids=["other-port", "other-path"],
)
async def test_a_listed_loopback_callback_matches_any_port_but_only_its_path(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
    redirect_uri: str,
    accepted: bool,
) -> None:
    """Native and CLI clients (RFC 8252) pick a fresh port per sign-in, so a
    listed loopback callback must work on any port, never on another path."""
    _require_allowlist(variant)
    client = await _client_with(
        hass,
        hass_client_no_auth,
        variant,
        fs_root,
        oauth_redirect_allowlist=["http://127.0.0.1/cli-callback"],
    )

    status, _location = await _status_for(client, variant, redirect_uri)

    assert status == (302 if accepted else 400)


@pytest.mark.parametrize("allow_any", [False, True], ids=["listed", "allow-any"])
async def test_a_callback_carrying_credentials_is_refused(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
    allow_any: bool,
) -> None:
    """A callback with userinfo is never legitimate: it is refused even when
    its host and path are listed, and even with any callback allowed."""
    _require_allowlist(variant)
    client = await _client_with(
        hass,
        hass_client_no_auth,
        variant,
        fs_root,
        oauth_redirect_allowlist=["http://127.0.0.1/cli-callback"],
        oauth_allow_any_callback=allow_any,
    )

    result = await _status_for(client, variant, "http://u@127.0.0.1:5000/cli-callback")

    assert result == (400, None)


@pytest.mark.parametrize(
    ("callback", "listable"),
    [(OTHER_CALLBACK, True), ("https://u:pw@other.example/cb", False)],
    ids=["unlisted", "carries-credentials"],
)
async def test_a_refusal_blames_the_list_only_for_a_callback_the_list_could_hold(
    proxy_client: TestClient,
    variant: Variant,
    caplog: pytest.LogCaptureFixture,
    callback: str,
    listable: bool,
) -> None:
    """The app's option refuses a callback carrying credentials, so telling the
    administrator to list one sends them to a setting that cannot help."""
    _require_allowlist(variant)
    _verifier, challenge = pkce_pair()

    with caplog.at_level(logging.WARNING):
        response = await _authorize(
            proxy_client, variant, challenge, redirect_uri=callback
        )

    page = await response.text()
    logged = [r.getMessage() for r in caplog.records if callback in r.getMessage()]
    assert response.status == 400
    assert ("add this exact address" in page) is listable
    assert len(logged) == 1
    assert (REFUSED_LINE in logged[0]) is listable


async def test_allow_any_callback_accepts_an_unlisted_callback(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
) -> None:
    """With the app's allow-any option on, an unlisted https callback signs in."""
    _require_allowlist(variant)
    client = await _client_with(
        hass, hass_client_no_auth, variant, fs_root, oauth_allow_any_callback=True
    )

    status, location = await _status_for(client, variant, OTHER_CALLBACK)

    assert status == 302
    assert location is not None and location.startswith(f"{OTHER_CALLBACK}?")


@pytest.mark.parametrize(
    "stored",
    [CLAUDE_REDIRECT, [CLAUDE_REDIRECT, "http://localhost/callback#frag"]],
    ids=["not-a-list", "unusable-entry"],
)
async def test_a_malformed_stored_list_refuses_every_callback(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
    stored: object,
) -> None:
    """A list no validated writer could have stored is not the administrator's:
    honouring its other entries or falling back to the defaults would widen
    the setting silently, so even a callback it holds is refused."""
    _require_allowlist(variant)
    client = await _client_with(
        hass, hass_client_no_auth, variant, fs_root, oauth_redirect_allowlist=stored
    )

    result = await _status_for(client, variant, CLAUDE_REDIRECT)

    assert result == (400, None)


async def test_a_refused_callback_is_logged_without_forging_lines(
    proxy_client: TestClient,
    variant: Variant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Whoever reads the log can tell why a client keeps failing to sign in,
    and a terminal escape in the anonymous caller's callback stays inert."""
    _require_allowlist(variant)

    with caplog.at_level(logging.WARNING):
        await _status_for(proxy_client, variant, f"{OTHER_CALLBACK}\x1b[31mRED")

    lines = _refusal_lines(caplog)
    assert len(lines) == 1
    assert OTHER_CALLBACK in lines[0]
    assert "\x1b" not in lines[0]


async def test_a_burst_of_refusals_writes_one_line_then_a_count(
    proxy_client: TestClient,
    component: ModuleType,
    variant: Variant,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authorize is anonymous, so refusals must not flood the log: a burst
    writes one line, and the next line counts what it held back."""
    _require_allowlist(variant)
    # The clock the refusal log's rate limit reads, moved only by the test.
    now = [1_000.0]
    clock = SimpleNamespace(monotonic=lambda: now[0])
    monkeypatch.setattr(submodule(component, "oauth_autoapprove"), "time", clock)

    with caplog.at_level(logging.WARNING):
        for n in range(5):
            await _status_for(proxy_client, variant, f"{OTHER_CALLBACK}{n}")
        assert len(_refusal_lines(caplog)) == 1
        now[0] += 3600
        await _status_for(proxy_client, variant, f"{OTHER_CALLBACK}-last")

    lines = _refusal_lines(caplog)
    assert len(lines) == 2
    assert "4 more refused since the last such line" in lines[1]
    assert f"{OTHER_CALLBACK}-last" in lines[1]


async def test_authorize_refuses_a_non_s256_challenge(
    proxy_client: TestClient, variant: Variant
) -> None:
    """Only PKCE S256 is advertised; a ``plain`` challenge is a 400, never a
    code."""
    _verifier, challenge = pkce_pair()

    response = await _authorize(
        proxy_client, variant, challenge, code_challenge_method="plain"
    )

    assert response.status == 400


async def test_wrong_pkce_verifier_is_an_invalid_grant(
    proxy_client: TestClient, variant: Variant
) -> None:
    """A code redeemed with another verifier gets ``invalid_grant``."""
    _verifier, challenge = pkce_pair()
    other_verifier, _challenge = pkce_pair()
    query = _redirect_query(await _authorize(proxy_client, variant, challenge))

    response = await _token(proxy_client, variant, query["code"][0], other_verifier)

    assert response.status == 400
    assert (await response.json())["error"] == "invalid_grant"


async def test_authorization_code_redeems_only_once(
    proxy_client: TestClient, variant: Variant
) -> None:
    """A replayed code is refused after its first successful exchange."""
    verifier, challenge = pkce_pair()
    code = _redirect_query(await _authorize(proxy_client, variant, challenge))["code"][
        0
    ]

    first = await _token(proxy_client, variant, code, verifier)
    replay = await _token(proxy_client, variant, code, verifier)

    assert first.status == 200
    assert replay.status == 400
    assert (await replay.json())["error"] == "invalid_grant"
