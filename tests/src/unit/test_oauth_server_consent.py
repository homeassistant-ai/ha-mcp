"""The ``ha-mcp-oauth`` consent form: where a user hands over their token.

The page tells the user which site gets their Long-Lived Access Token, and the
form only accepts a submission for a sign-in that is still open.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
from freezegun import freeze_time

from ha_mcp.auth.consent_form import create_consent_html, create_error_html
from ha_mcp.auth.provider import PENDING_AUTHORIZATION_EXPIRY_SECONDS

from ._oauth_server_core import BASE_URL, CALLBACK, connector, issuer, query
from ._oauth_server_core import oauth_client as oauth_client
from ._oauth_server_core import oauth_provider as oauth_provider

START = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)


def test_consent_form_asks_for_the_token_only() -> None:
    """The Home Assistant URL is server-side; a form field for it was an SSRF."""
    html = create_consent_html(
        client_id="test-client",
        redirect_uri=CALLBACK,
        state="test-state",
        txn_id="test-txn-123",
    )

    assert "<form" in html
    assert 'name="ha_token"' in html
    assert "test-txn-123" in html
    assert 'name="ha_url"' not in html


def test_consent_form_names_the_site_the_token_is_shared_with() -> None:
    """The user sees the callback's site, not a name the client chose."""
    html = create_consent_html(
        client_id="test-client",
        redirect_uri="https://chatgpt.com/aip/callback",
        state="state",
        txn_id="txn-789",
    )

    assert "warning-box" in html
    assert "shared with" in html
    assert "Authorization request from <strong>chatgpt.com</strong>" in html
    assert "Long-Lived Access Tokens" in html


def test_consent_form_shows_the_error_it_was_sent_back_with() -> None:
    html = create_consent_html(
        client_id="test-client",
        redirect_uri=CALLBACK,
        state="state",
        txn_id="txn-123",
        error_message="Invalid credentials",
    )

    assert "Invalid credentials" in html
    assert "error-message" in html


def test_consent_form_does_not_run_values_the_client_sent() -> None:
    sent = {
        "client_id": '<script>alert("client")</script>',
        "redirect_uri": 'http://evil.example/"><script>alert("redirect")</script>',
        "state": '"><script>alert("state")</script>',
        "txn_id": '"><script>alert("txn")</script>',
    }

    html = create_consent_html(**sent)

    # The page has a <script> of its own, so each value is looked for as sent.
    for field, value in sent.items():
        assert value not in html, field
        assert value.removeprefix("http://evil.example/") not in html, field
    assert "&lt;script&gt;alert(" in html
    assert "&quot;&gt;&lt;script&gt;" in html


def test_error_page_shows_the_error() -> None:
    html = create_error_html(
        error="invalid_request", error_description="The request was malformed"
    )

    assert "invalid_request" in html
    assert "The request was malformed" in html
    assert "Authentication Error" in html


def test_error_page_does_not_run_its_values() -> None:
    html = create_error_html(
        error='<script>alert("xss")</script>',
        error_description="<img src=x onerror=alert(1)>",
    )

    assert "<script>" not in html
    assert "<img src=x onerror" not in html
    assert "&lt;script&gt;" in html


async def test_authorize_opens_the_consent_form_for_that_sign_in(
    oauth_client: httpx.AsyncClient,
) -> None:
    """The consent redirect is not an authorization response, so it has no iss."""
    client = await connector(oauth_client)

    resp = await client.authorize()

    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith(f"{BASE_URL}/consent?")
    assert "iss" not in query(location)
    form = await oauth_client.get(location)
    assert form.status_code == 200
    assert "claude.ai" in form.text
    assert query(location)["txn_id"][0] in form.text


async def test_an_unregistered_client_gets_no_consent_form(
    oauth_client: httpx.AsyncClient,
) -> None:
    client = await connector(oauth_client)
    client.client_id = "never-registered"

    resp = await client.authorize()

    assert resp.status_code == 400
    assert "location" not in resp.headers


async def test_consent_form_without_a_sign_in_is_refused(
    oauth_client: httpx.AsyncClient,
) -> None:
    resp = await oauth_client.get("/consent")

    assert resp.status_code == 400
    assert "Missing transaction ID" in resp.text


async def test_consent_form_for_an_unknown_sign_in_is_refused(
    oauth_client: httpx.AsyncClient,
) -> None:
    resp = await oauth_client.get("/consent", params={"txn_id": "nonexistent"})

    assert resp.status_code == 400
    assert "expired or not found" in resp.text


async def test_an_expired_consent_form_is_refused_and_forgotten(
    oauth_client: httpx.AsyncClient,
) -> None:
    with freeze_time(START, tick=True) as clock:
        client = await connector(oauth_client)
        txn_id = await client.open_consent()
        clock.move_to(
            START + timedelta(seconds=PENDING_AUTHORIZATION_EXPIRY_SECONDS + 60)
        )

        first = await oauth_client.get("/consent", params={"txn_id": txn_id})
        again = await oauth_client.get("/consent", params={"txn_id": txn_id})

    assert first.status_code == 400
    assert "has expired" in first.text
    assert "expired or not found" in again.text


async def test_a_form_sent_without_a_token_comes_back_with_an_error(
    oauth_client: httpx.AsyncClient,
) -> None:
    client = await connector(oauth_client)
    txn_id = await client.open_consent()

    resp = await client.submit(txn_id)

    assert resp.status_code == 303
    location = resp.headers["location"]
    assert query(location)["txn_id"] == [txn_id]
    form = await oauth_client.get(location)
    assert form.status_code == 200
    assert "Please provide your Long-Lived Access Token." in form.text


async def test_a_sent_form_returns_a_code_from_this_issuer_to_the_client(
    oauth_client: httpx.AsyncClient,
) -> None:
    """RFC 9207 §2: the response names the issuer the metadata advertises."""
    client = await connector(oauth_client)

    resp = await client.submit(await client.open_consent(), ha_token="llat")

    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith(CALLBACK)
    params = query(location)
    assert params["code"]
    assert params["state"] == ["st-1"]
    assert params["iss"] == [await issuer(oauth_client)]


async def test_a_callback_with_its_own_iss_gets_exactly_this_issuers(
    oauth_client: httpx.AsyncClient,
) -> None:
    """Parsers take the first of two ``iss`` values: the client's must go."""
    client = await connector(
        oauth_client, redirect_uris=["https://client.example/cb?iss=evil&keep=1"]
    )

    resp = await client.submit(await client.open_consent(), ha_token="llat")

    params = query(resp.headers["location"])
    assert params["iss"] == [await issuer(oauth_client)]
    assert params["keep"] == ["1"]
    assert params["state"] == ["st-1"]
    assert params["code"]
