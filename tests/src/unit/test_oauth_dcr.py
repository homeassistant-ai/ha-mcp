"""Unit tests for the stateless DCR client_id blobs (oauth_dcr)."""

from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace

import pytest

from ._embedded_stubs import install

install()

from custom_components.ha_mcp_tools import oauth_dcr  # noqa: E402
from custom_components.ha_mcp_tools.const import (  # noqa: E402
    DATA_WEBHOOK,
    DOMAIN,
)
from custom_components.ha_mcp_tools.oauth_dcr import (  # noqa: E402
    CFG_DCR_SIGNING_KEY,
    DcrRegisterView,
    client_redirect_uris,
    mint_client_id,
)

KEY = b"k" * 32
OTHER_KEY = b"x" * 32


def test_mint_and_verify_round_trip():
    uris = ["https://claude.ai/api/mcp/auth_callback"]
    cid = mint_client_id(KEY, uris)
    assert cid.startswith("hamcp-dcr-")
    assert client_redirect_uris(KEY, cid) == uris


def test_wrong_key_rejects():
    cid = mint_client_id(KEY, ["https://example.com/cb"])
    assert client_redirect_uris(OTHER_KEY, cid) is None


def test_tampered_blob_rejects():
    cid = mint_client_id(KEY, ["https://example.com/cb"])
    body, _, sig = cid.rpartition(".")
    assert client_redirect_uris(KEY, body + ".AAAA" + sig[4:]) is None


def test_non_dcr_client_id_rejects():
    assert client_redirect_uris(KEY, "https://claude.ai") is None
    assert client_redirect_uris(KEY, "") is None
    assert client_redirect_uris(KEY, "hamcp-dcr-notablob") is None


def _module_is(name: str, root: str) -> bool:
    """Return whether a module name is ``root`` or one of its children."""
    return name == root or name.startswith(f"{root}.")


@pytest.fixture
async def dcr_view_client_factory():
    """Build real aiohttp clients around a fake HA view registration.

    The shared embedded-component stubs replace ``aiohttp`` in ``sys.modules``.
    Load the real package only for this fixture, then restore the exact module
    table so this HTTP-level harness and the stub-driven component tests can
    coexist in the full unit suite.
    """
    package_roots = ("aiohttp", "yarl")
    saved_modules = {
        name: module
        for name, module in tuple(sys.modules.items())
        if any(_module_is(name, root) for root in package_roots)
    }
    for name in saved_modules:
        sys.modules.pop(name, None)

    clients = []
    stub_web = oauth_dcr.web
    try:
        aiohttp_web = importlib.import_module("aiohttp.web")
        test_utils = importlib.import_module("aiohttp.test_utils")
        oauth_dcr.web = aiohttp_web

        async def factory(*, dcr_key):
            cfg = {}
            if dcr_key is not None:
                cfg[CFG_DCR_SIGNING_KEY] = dcr_key

            app = aiohttp_web.Application()
            hass = SimpleNamespace(
                data={DOMAIN: {DATA_WEBHOOK: cfg}},
                http=SimpleNamespace(),
            )

            def register_view(view):
                app.router.add_post(view.url, view.post)

            hass.http.register_view = register_view
            hass.http.register_view(DcrRegisterView(hass))

            client = test_utils.TestClient(test_utils.TestServer(app))
            await client.start_server()
            clients.append(client)
            return client

        yield factory
    finally:
        for client in clients:
            await client.close()
        oauth_dcr.web = stub_web
        for name in tuple(sys.modules):
            if any(_module_is(name, root) for root in package_roots):
                sys.modules.pop(name, None)
        sys.modules.update(saved_modules)


async def test_register_404s_when_no_dcr_key_live(dcr_view_client_factory):
    client = await dcr_view_client_factory(dcr_key=None)
    resp = await client.post(
        "/api/ha_mcp_tools/oauth/register",
        json={"redirect_uris": ["https://claude.ai/api/mcp/auth_callback"]},
    )
    assert resp.status == 404


async def test_register_mints_verifiable_client_id(dcr_view_client_factory):
    client = await dcr_view_client_factory(dcr_key=KEY)
    resp = await client.post(
        "/api/ha_mcp_tools/oauth/register",
        json={
            "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
            "client_name": "probe",
            "application_type": "web",
        },
    )
    assert resp.status == 201
    data = await resp.json()
    assert data["token_endpoint_auth_method"] == "none"
    assert data["client_name"] == "probe"
    assert client_redirect_uris(KEY, data["client_id"]) == [
        "https://claude.ai/api/mcp/auth_callback"
    ]


async def test_register_rejects_bad_redirects(dcr_view_client_factory):
    client = await dcr_view_client_factory(dcr_key=KEY)
    for bad in (
        [],
        ["http://not-loopback.example/cb"],
        ["https://ok.example/cb#frag"],
        ["x" * 600],
    ):
        resp = await client.post(
            "/api/ha_mcp_tools/oauth/register", json={"redirect_uris": bad}
        )
        assert resp.status == 400, bad
