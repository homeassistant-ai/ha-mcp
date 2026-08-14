"""Unit tests for CIMD validation + client_id translation (oauth_ha_auth)."""

from __future__ import annotations

import importlib
import sys

import pytest

from ._embedded_stubs import install

install()

from custom_components.ha_mcp_tools import oauth_ha_auth  # noqa: E402
from custom_components.ha_mcp_tools.oauth_dcr import mint_client_id  # noqa: E402
from custom_components.ha_mcp_tools.oauth_ha_auth import (  # noqa: E402
    origin_client_id,
    redirect_matches,
    resolve_forward_client_id,
)

KEY = b"k" * 32


def test_redirect_matches_exact():
    assert redirect_matches(["https://spark.example/cb"], "https://spark.example/cb")
    assert not redirect_matches(
        ["https://spark.example/cb"], "https://spark.example/other"
    )


def test_redirect_matches_loopback_port_agnostic():
    # Claude Code declares http://localhost/callback and http://127.0.0.1/callback
    # without ports; the runtime request carries an ephemeral port (RFC 8252
    # §7.3 requires accepting any port for loopback).
    registered = ["http://localhost/callback", "http://127.0.0.1/callback"]
    assert redirect_matches(registered, "http://localhost:61264/callback")
    assert redirect_matches(registered, "http://127.0.0.1:3118/callback")
    assert not redirect_matches(registered, "http://localhost:61264/other")
    assert not redirect_matches(registered, "http://evil.example:80/callback")


def test_origin_client_id():
    assert (
        origin_client_id("https://accounts.google.example/cb?x=1")
        == "https://accounts.google.example"
    )


@pytest.mark.asyncio
async def test_resolve_same_origin_passthrough():
    # claude.ai's hosted client: client_id and redirect share an origin —
    # forwarded untouched (today's working behavior).
    out = await resolve_forward_client_id(
        session=None,
        dcr_key=None,
        client_id="https://claude.ai/oauth/mcp-oauth-client-metadata",
        redirect_uri="https://claude.ai/api/mcp/auth_callback",
    )
    assert out == "https://claude.ai/oauth/mcp-oauth-client-metadata"


@pytest.mark.asyncio
async def test_resolve_dcr_blob_translates_to_redirect_origin():
    cid = mint_client_id(KEY, ["https://claude.ai/api/mcp/auth_callback"])
    out = await resolve_forward_client_id(
        session=None,
        dcr_key=KEY,
        client_id=cid,
        redirect_uri="https://claude.ai/api/mcp/auth_callback",
    )
    assert out == "https://claude.ai"


@pytest.mark.asyncio
async def test_resolve_dcr_blob_with_unregistered_redirect_passes_through():
    # Fail-safe: no translation without validation — core then applies its own
    # (stricter) same-origin rule to the untouched request.
    cid = mint_client_id(KEY, ["https://claude.ai/api/mcp/auth_callback"])
    out = await resolve_forward_client_id(
        session=None,
        dcr_key=KEY,
        client_id=cid,
        redirect_uri="https://evil.example/cb",
    )
    assert out == cid


@pytest.mark.asyncio
async def test_resolve_multi_origin_registration_passes_through():
    cid = mint_client_id(KEY, ["https://a.example/cb", "https://b.example/cb"])
    out = await resolve_forward_client_id(
        session=None,
        dcr_key=KEY,
        client_id=cid,
        redirect_uri="https://b.example/cb",
    )
    assert out == cid  # translation would disagree with the refresh leg — skip it


def _module_is(name: str, root: str) -> bool:
    """Return whether a module name is ``root`` or one of its children."""
    return name == root or name.startswith(f"{root}.")


@pytest.fixture
async def aiohttp_client_factory():
    """Serve real CIMD documents while the component keeps its aiohttp stub."""
    package_roots = ("aiohttp", "yarl")
    saved_modules = {
        name: module
        for name, module in tuple(sys.modules.items())
        if any(_module_is(name, root) for root in package_roots)
    }
    for name in saved_modules:
        sys.modules.pop(name, None)

    clients = []
    stub_timeout = oauth_ha_auth.CIMD_FETCH_TIMEOUT
    try:
        aiohttp = importlib.import_module("aiohttp")
        aiohttp_web = importlib.import_module("aiohttp.web")
        test_utils = importlib.import_module("aiohttp.test_utils")
        oauth_ha_auth.CIMD_FETCH_TIMEOUT = aiohttp.ClientTimeout(total=5)

        async def factory(*, body, status, self_ref_client_id):
            doc_url = ""

            async def serve_document(_request):
                response_body = dict(body)
                if self_ref_client_id:
                    response_body["client_id"] = doc_url
                return aiohttp_web.json_response(response_body, status=status)

            app = aiohttp_web.Application()
            app.router.add_get("/client.json", serve_document)
            client = test_utils.TestClient(test_utils.TestServer(app, host="localhost"))
            await client.start_server()
            clients.append(client)
            doc_url = str(client.make_url("/client.json"))
            return doc_url, client.session

        yield factory
    finally:
        for client in clients:
            await client.close()
        oauth_ha_auth.CIMD_FETCH_TIMEOUT = stub_timeout
        for name in tuple(sys.modules):
            if any(_module_is(name, root) for root in package_roots):
                sys.modules.pop(name, None)
        sys.modules.update(saved_modules)


@pytest.mark.asyncio
async def test_fetch_cimd_valid_document(aiohttp_client_factory, monkeypatch):
    doc_url, session = await aiohttp_client_factory(
        body={
            "client_id": "PLACEHOLDER_SELF",
            "redirect_uris": ["https://g.example/cb"],
        },
        status=200,
        self_ref_client_id=True,
    )
    monkeypatch.setattr(oauth_ha_auth, "_ALLOWED_SCHEMES", ("http", "https"))
    monkeypatch.setattr(oauth_ha_auth, "_is_loopback_host", lambda _host: False)

    oauth_ha_auth._cimd_cache.clear()
    uris = await oauth_ha_auth.fetch_cimd_redirects(session, doc_url)
    assert uris == ["https://g.example/cb"]


@pytest.mark.asyncio
async def test_fetch_cimd_rejects_mismatched_client_id(
    aiohttp_client_factory, monkeypatch
):
    doc_url, session = await aiohttp_client_factory(
        body={
            "client_id": "https://other.example/x",
            "redirect_uris": ["https://g.example/cb"],
        },
        status=200,
        self_ref_client_id=False,
    )
    monkeypatch.setattr(oauth_ha_auth, "_ALLOWED_SCHEMES", ("http", "https"))
    monkeypatch.setattr(oauth_ha_auth, "_is_loopback_host", lambda _host: False)

    oauth_ha_auth._cimd_cache.clear()
    assert await oauth_ha_auth.fetch_cimd_redirects(session, doc_url) is None


@pytest.mark.asyncio
async def test_fetch_cimd_refuses_ip_literal_and_loopback():
    assert await oauth_ha_auth.fetch_cimd_redirects(None, "https://127.0.0.1/x") is None
    assert (
        await oauth_ha_auth.fetch_cimd_redirects(None, "https://10.0.0.5/doc.json")
        is None
    )
    assert await oauth_ha_auth.fetch_cimd_redirects(None, "https://h.example/") is None
