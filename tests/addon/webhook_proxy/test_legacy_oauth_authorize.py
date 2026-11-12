"""The legacy ``/authorize`` consent page and its form, driven over HTTP."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant.typing import ClientSessionGenerator

from ._legacy_oauth import (
    CLIENT_ID,
    PUBLIC_BASE_URL,
    REDIRECT_URI,
    STATE,
    VERIFIER,
    RenderedPage,
    get_json,
    redirect_query,
    s256,
    set_up_legacy,
    submit_consent,
)
from ._variants import Variant


def _authorize_query(**overrides: str) -> dict[str, str]:
    query = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "code_challenge": s256(VERIFIER),
        "code_challenge_method": "S256",
        "state": STATE,
    }
    query.update(overrides)
    return query


@pytest.mark.parametrize(
    ("overrides", "names"),
    [
        ({"response_type": "token"}, "unsupported_response_type"),
        # RFC 7636: `plain` would downgrade PKCE to a bare shared value.
        ({"code_challenge_method": "plain"}, "S256"),
        ({"code_challenge": "too-short"}, "code_challenge"),
        ({"client_id": "not-the-configured-id"}, "client_id"),
    ],
    ids=["response-type", "plain-pkce", "short-challenge", "unknown-client"],
)
async def test_authorize_refuses_a_malformed_request(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
    overrides: dict[str, str],
    names: str,
) -> None:
    """Each refusal names what is wrong. Only the unknown client id, which is
    what a client holding credentials from before a regeneration sends, tells
    the user to restart Home Assistant (#1694): that is the one a restart fixes."""
    await set_up_legacy(hass, variant, fs_root)
    client = await hass_client_no_auth()

    response = await client.get("/authorize", params=_authorize_query(**overrides))

    text = await response.text()
    assert response.status == 400
    assert names in text
    assert ("restart Home Assistant" in text) is ("client_id" in overrides)


@pytest.mark.parametrize(
    ("redirect_uri", "accepted"),
    [
        ("https://claude.ai/callback", True),
        ("https://example.com:8443/cb", True),
        ("http://claude.ai/cb", False),
        ("ftp://claude.ai/cb", False),
        ("javascript:alert(1)", False),
        ("https:///nohost", False),
        ("", False),
        ("https://claude.ai/cb#fragment", False),
    ],
    ids=[
        "https",
        "https-port",
        "http",
        "ftp",
        "javascript",
        "no-host",
        "empty",
        "fragment",
    ],
)
async def test_authorize_accepts_only_safe_redirect_uris(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
    redirect_uri: str,
    accepted: bool,
) -> None:
    """The redirect carries the authorization code, so a plaintext, non-web or
    fragment-bearing callback (RFC 6749 §3.1.2) is refused before consent."""
    await set_up_legacy(hass, variant, fs_root)
    client = await hass_client_no_auth()

    response = await client.get(
        "/authorize", params=_authorize_query(redirect_uri=redirect_uri)
    )

    if accepted:
        assert response.status == 200
    else:
        assert response.status == 400
        assert "redirect_uri" in await response.text()


async def test_consent_page_shows_the_destination_and_posts_the_request_back(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
) -> None:
    """The user sees where the code will go, and the form carries the request
    back to the same route so the approval can be checked again."""
    await set_up_legacy(hass, variant, fs_root)
    client = await hass_client_no_auth()

    response = await client.get("/authorize", params=_authorize_query())

    assert response.status == 200
    assert response.content_type == "text/html"
    html = await response.text()
    assert f"<code>{REDIRECT_URI}</code>" in html
    page = RenderedPage(html)
    assert ("form", {"method": "POST", "action": "/authorize"}) in page.tags
    form = {
        name: str(attrs.get("value") or "") for name, attrs in page.inputs().items()
    }
    assert set(form) == {"client_id", "redirect_uri", "state", "code_challenge"}

    approval = await client.post(
        "/authorize", data={**form, "action": "approve"}, allow_redirects=False
    )

    assert approval.status == 302
    assert redirect_query(approval)["code"]


async def test_consent_page_escapes_a_hostile_redirect_uri(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
) -> None:
    """A redirect_uri is attacker-chosen and rendered into the page; markup in
    it must arrive as text, not run. (Home Assistant's own request filter
    refuses a literal ``<script>`` in the query, so the payload is an
    attribute breakout instead.)"""
    await set_up_legacy(hass, variant, fs_root)
    client = await hass_client_no_auth()
    hostile = 'https://evil.example/?"><img src=x onerror=alert(1)>'

    response = await client.get(
        "/authorize", params=_authorize_query(redirect_uri=hostile)
    )

    assert response.status == 200
    page = RenderedPage(await response.text())
    redirect_input = page.inputs()["redirect_uri"]
    assert redirect_input["value"] == hostile
    assert set(redirect_input) == {"type", "name", "value"}
    assert "img" not in {tag for tag, _attrs in page.tags}


@pytest.mark.parametrize(
    ("action", "outcome"),
    [("approve", "code"), ("deny", "error")],
)
async def test_consent_answer_redirects_with_state_and_the_advertised_issuer(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
    action: str,
    outcome: str,
) -> None:
    """Approval returns a code and denial ``access_denied``; both echo the
    client's state and carry the RFC 9207 ``iss`` equal to the issuer the
    metadata document advertises, so a mix-up attack is detectable."""
    await set_up_legacy(hass, variant, fs_root, public_base_url=PUBLIC_BASE_URL)
    client = await hass_client_no_auth()
    metadata = await get_json(client, f"{variant.oauth_base}/authorization-server")

    response = await submit_consent(client, action)

    assert response.status == 302
    assert response.headers["Location"].startswith(REDIRECT_URI)
    params = redirect_query(response)
    assert outcome in params
    if action == "deny":
        assert params["error"] == ["access_denied"]
    assert params["state"] == [STATE]
    assert params["iss"] == [metadata["issuer"]]


@pytest.mark.parametrize(
    ("fields", "names"),
    [
        ({"client_id": "attacker-substituted-id"}, "client_id"),
        ({"redirect_uri": "http://evil.example/"}, "redirect_uri"),
    ],
    ids=["client-id", "redirect-uri"],
)
async def test_consent_submission_rechecks_its_hidden_fields(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
    fields: dict[str, str],
    names: str,
) -> None:
    """The POST can be forged without ever loading the page, so the hidden
    fields are validated again rather than trusted."""
    await set_up_legacy(hass, variant, fs_root)
    client = await hass_client_no_auth()

    response = await submit_consent(client, **fields)

    assert response.status == 400
    assert names in await response.text()


async def test_unknown_consent_action_issues_nothing(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
) -> None:
    await set_up_legacy(hass, variant, fs_root)
    client = await hass_client_no_auth()

    response = await submit_consent(client, "hijack")

    assert response.status == 400
    assert "Location" not in response.headers


async def test_full_pending_code_store_answers_temporarily_unavailable(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
) -> None:
    """Approvals nobody redeems must not grow memory without bound: at the cap
    the client is told to retry later (RFC 6749 §4.1.2.1)."""
    await set_up_legacy(hass, variant, fs_root)
    provider = hass.data[variant.domain]["oauth"]
    for _ in range(component.oauth.MAX_PENDING_CODES):
        assert provider.issue_code(REDIRECT_URI, s256(VERIFIER)) is not None
    client = await hass_client_no_auth()

    response = await submit_consent(client)

    assert response.status == 302
    params = redirect_query(response)
    assert params["error"] == ["temporarily_unavailable"]
    assert "code" not in params
