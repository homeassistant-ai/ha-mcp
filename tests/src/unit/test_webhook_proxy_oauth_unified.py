"""Proxy ports of the unified OAuth, DCR, and CIMD regression tests."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ._embedded_stubs import install

install()

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPONENT_DIR = (
    REPO_ROOT / "homeassistant-addon-webhook-proxy-dev" / "mcp_proxy_dev"
)
KEY = b"k" * 32
GOOGLE_REDIRECT_URIS = [
    "https://oauth-redirect.googleusercontent.com/r/ha-mcp",
    "https://oauth-redirect-sandbox.googleusercontent.com/r/ha-mcp",
]


def _load_submodule(monkeypatch, package_name: str, name: str):
    module_name = f"{package_name}.{name}"
    spec = importlib.util.spec_from_file_location(
        module_name, COMPONENT_DIR / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def oauth_stack(monkeypatch):
    """Load the dev proxy OAuth modules as one package with a live backend."""
    package_name = "webhook_proxy_dev_oauth_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(COMPONENT_DIR)]
    monkeypatch.setitem(sys.modules, package_name, package)

    oauth = _load_submodule(monkeypatch, package_name, "oauth")

    async def _backend_alive(_hass):
        return True

    monkeypatch.setattr(oauth, "_backend_alive", _backend_alive)
    dcr = _load_submodule(monkeypatch, package_name, "oauth_dcr")
    indirect = _load_submodule(monkeypatch, package_name, "oauth_indirect")
    return SimpleNamespace(
        oauth=oauth,
        dcr=dcr,
        indirect=indirect,
        package_name=package_name,
    )


def _hass(oauth, mode: str, *, dcr_key: bytes | None = KEY):
    data = {
        "oauth_mode": mode,
        "target_url": "http://127.0.0.1:9583/private_x",
    }
    if dcr_key is not None:
        data["dcr_signing_key"] = dcr_key
    return SimpleNamespace(data={oauth.DOMAIN: data}, http=SimpleNamespace())


def _json_request(body):
    return SimpleNamespace(json=AsyncMock(return_value=body))


def test_dcr_client_id_round_trips_multiple_redirects(oauth_stack):
    """A signed registration preserves every presented redirect URI."""
    dcr = oauth_stack.dcr
    client_id = dcr.mint_client_id(KEY, GOOGLE_REDIRECT_URIS)

    assert client_id.startswith("hamcp-dcr-")
    assert dcr.client_redirect_uris(KEY, client_id) == GOOGLE_REDIRECT_URIS
    assert dcr.client_redirect_uris(b"x" * 32, client_id) is None


async def test_dcr_none_mode_advertises_authorization_code_only(oauth_stack):
    """None-mode registrations do not promise an unsupported refresh grant."""
    oauth, dcr = oauth_stack.oauth, oauth_stack.dcr
    response = await dcr.DcrRegisterView(
        _hass(oauth, oauth.MODE_NONE_AUTOAPPROVE)
    ).post(_json_request({"redirect_uris": ["https://a.example/cb"]}))

    assert response.status == 201
    assert response.json_body["grant_types"] == ["authorization_code"]


async def test_dcr_ha_auth_narrows_multi_origin_registration(oauth_stack):
    """Spark's two web origins register, without an unreproducible refresh."""
    oauth, dcr = oauth_stack.oauth, oauth_stack.dcr
    response = await dcr.DcrRegisterView(_hass(oauth, oauth.MODE_HA_AUTH)).post(
        _json_request({"redirect_uris": GOOGLE_REDIRECT_URIS})
    )

    assert response.status == 201
    assert response.json_body["grant_types"] == ["authorization_code"]
    assert dcr.client_redirect_uris(KEY, response.json_body["client_id"]) == (
        GOOGLE_REDIRECT_URIS
    )


async def test_dcr_ha_auth_keeps_reproducible_refresh(oauth_stack):
    """A single stable web origin can be reconstructed on refresh."""
    oauth, dcr = oauth_stack.oauth, oauth_stack.dcr
    response = await dcr.DcrRegisterView(_hass(oauth, oauth.MODE_HA_AUTH)).post(
        _json_request({"redirect_uris": ["https://a.example/cb"]})
    )

    assert response.status == 201
    assert response.json_body["grant_types"] == [
        "authorization_code",
        "refresh_token",
    ]


async def test_dcr_preserves_explicit_zero_port_origin(oauth_stack):
    """An explicit port zero remains distinct from the HTTPS default port."""
    oauth, dcr = oauth_stack.oauth, oauth_stack.dcr
    response = await dcr.DcrRegisterView(_hass(oauth, oauth.MODE_HA_AUTH)).post(
        _json_request(
            {
                "redirect_uris": [
                    "https://a.example/cb",
                    "https://a.example:0/cb",
                ]
            }
        )
    )

    assert response.status == 201
    assert response.json_body["grant_types"] == ["authorization_code"]


async def test_dcr_rejects_non_loopback_http_redirect(oauth_stack):
    """Only HTTPS and RFC 8252 loopback HTTP callbacks are accepted."""
    oauth, dcr = oauth_stack.oauth, oauth_stack.dcr
    response = await dcr.DcrRegisterView(
        _hass(oauth, oauth.MODE_NONE_AUTOAPPROVE)
    ).post(_json_request({"redirect_uris": ["http://evil.example/cb"]}))

    assert response.status == 400
    assert response.json_body["error"] == "invalid_redirect_uri"


def test_cimd_timing_and_negative_cache_contract(oauth_stack):
    """Pin the anonymous lookup's resolver, total deadline, and cache bounds."""
    indirect = oauth_stack.indirect

    assert indirect.CIMD_RESOLVE_TIMEOUT == 5.0
    assert indirect.CIMD_TOTAL_LOOKUP_TIMEOUT == 12.0
    assert indirect.CIMD_NEGATIVE_TTL == 60.0
    assert indirect.CIMD_NEGATIVE_TTL < indirect.CIMD_CACHE_TTL


def test_cimd_redirect_matching_includes_loopback_port_variance(oauth_stack):
    """Non-loopback matches are exact; loopback runtime ports may vary."""
    indirect = oauth_stack.indirect
    assert indirect.redirect_matches(
        ["https://spark.example/cb"], "https://spark.example/cb"
    )
    assert not indirect.redirect_matches(
        ["https://spark.example/cb"], "https://spark.example/other"
    )
    assert indirect.redirect_matches(
        ["http://localhost/callback"], "http://localhost:61264/callback"
    )
    assert not indirect.redirect_matches(
        ["http://localhost/callback"], "http://localhost:61264/other"
    )


@pytest.mark.parametrize(
    ("redirect_uri", "expected"),
    [
        (GOOGLE_REDIRECT_URIS[0], "https://oauth-redirect.googleusercontent.com"),
        (
            GOOGLE_REDIRECT_URIS[1],
            "https://oauth-redirect-sandbox.googleusercontent.com",
        ),
    ],
)
async def test_multi_origin_dcr_uses_presented_redirect_translation(
    oauth_stack, redirect_uri, expected
):
    """Each authorization leg translates to its matched presented origin."""
    dcr, indirect = oauth_stack.dcr, oauth_stack.indirect
    client_id = dcr.mint_client_id(KEY, GOOGLE_REDIRECT_URIS)

    translated = await indirect.resolve_forward_client_id(
        session=None,
        dcr_key=KEY,
        client_id=client_id,
        redirect_uri=redirect_uri,
    )

    assert translated == expected


async def test_same_origin_comparison_is_canonical(oauth_stack, monkeypatch):
    """An explicit default port remains on the no-fetch same-origin path."""
    indirect = oauth_stack.indirect
    fetch = AsyncMock()
    monkeypatch.setattr(indirect, "fetch_cimd_redirects", fetch)

    translated = await indirect.resolve_forward_client_id(
        session=object(),
        dcr_key=None,
        client_id="https://claude.ai:443/oauth/client.json",
        redirect_uri="https://claude.ai/api/mcp/auth_callback",
    )

    assert translated == "https://claude.ai:443/oauth/client.json"
    fetch.assert_not_awaited()


async def test_refresh_identity_has_three_variants(oauth_stack, monkeypatch):
    """Refresh derives an origin, passes through, or rejects unreproducible IDs."""
    dcr, indirect = oauth_stack.dcr, oauth_stack.indirect
    reproducible = dcr.mint_client_id(KEY, ["https://a.example/cb"])
    multi_origin = dcr.mint_client_id(KEY, GOOGLE_REDIRECT_URIS)

    assert (
        await indirect.translated_client_id_for_refresh(None, KEY, reproducible)
        == "https://a.example"
    )
    assert (
        await indirect.translated_client_id_for_refresh(None, KEY, multi_origin)
        is indirect.RefreshDisposition.UNREPRODUCIBLE
    )

    monkeypatch.setattr(indirect, "fetch_cimd_redirects", AsyncMock(return_value=None))
    assert (
        await indirect.translated_client_id_for_refresh(
            object(), None, "https://unknown.example/client.json"
        )
        is indirect.RefreshDisposition.PASSTHROUGH
    )


async def test_unreproducible_cimd_refresh_is_explicit(oauth_stack, monkeypatch):
    """Verified CIMD multi-origin identities use UNREPRODUCIBLE too."""
    indirect = oauth_stack.indirect
    monkeypatch.setattr(
        indirect,
        "fetch_cimd_redirects",
        AsyncMock(return_value=GOOGLE_REDIRECT_URIS),
    )

    translated = await indirect.translated_client_id_for_refresh(
        object(), None, "https://spark.example/client.json"
    )

    assert translated is indirect.RefreshDisposition.UNREPRODUCIBLE


async def test_cimd_unreachable_result_is_negative_cached(oauth_stack, monkeypatch):
    """Repeated requests for a dead identity do not repeat DNS resolution."""
    indirect = oauth_stack.indirect
    resolve = AsyncMock(return_value=[])
    monkeypatch.setattr(indirect, "_resolve_public_addresses", resolve)
    indirect._cimd_cache.clear()
    client_id = "https://dead.example/client.json"

    assert await indirect.fetch_cimd_redirects(object(), client_id) is None
    assert await indirect.fetch_cimd_redirects(object(), client_id) is None

    resolve.assert_awaited_once_with("dead.example", 443)
    indirect._cimd_cache.clear()
