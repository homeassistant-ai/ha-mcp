"""Run OIDC startup on the real server; only the identity provider is replaced."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

import pytest

from ha_mcp.__main__ import main_oidc
from ha_mcp._vendor.fastmcp import FastMCP
from ha_mcp._vendor.fastmcp.server.auth.auth import AuthProvider

# Sentinel used by MockOIDCProxy below to distinguish "kwarg not passed" from
# "kwarg passed with a falsy/None value" for the optional constructor args.
_UNSET = object()
OIDC_PROXY_PATCH_TARGET = "ha_mcp.auth.oidc_compat.HaMcpOIDCProxy"
CONFIG_URL = "https://auth.example.com/.well-known/openid-configuration"
BASE_URL = "https://mcp.example.com"


def make_mock_oidc_proxy(capture: dict[str, Any]) -> type[AuthProvider]:
    """Build a MockOIDCProxy class pinned to production's exact kwarg set.

    The real ``HaMcpOIDCProxy`` fetches the provider's discovery document over
    the network, so the provider is the one thing replaced. Unlike a
    ``**kwargs``-catch-all mock, this has an explicit signature with exactly the
    kwargs OIDC startup passes: a kwarg not listed here raises TypeError
    (``TestOIDCProxySignatureSubset`` checks the other direction). The optional
    arguments default to ``_UNSET`` and are recorded only when passed.
    """

    class MockOIDCProxy(AuthProvider):
        def __init__(
            self,
            *,
            config_url,
            client_id,
            client_secret,
            base_url,
            require_authorization_consent,
            required_scopes,
            jwt_signing_key,
            allowed_client_redirect_uris=_UNSET,
            verify_id_token=_UNSET,
            audience=_UNSET,
        ):
            super().__init__(base_url=base_url, required_scopes=required_scopes)
            capture["instance"] = self
            capture["config_url"] = config_url
            capture["client_id"] = client_id
            capture["client_secret"] = client_secret
            capture["base_url"] = base_url
            capture["require_authorization_consent"] = require_authorization_consent
            capture["required_scopes"] = required_scopes
            capture["jwt_signing_key"] = jwt_signing_key
            if allowed_client_redirect_uris is not _UNSET:
                capture["allowed_client_redirect_uris"] = allowed_client_redirect_uris
            if verify_id_token is not _UNSET:
                capture["verify_id_token"] = verify_id_token
            if audience is not _UNSET:
                capture["audience"] = audience

        def setup_scope_compatibility(self):
            """Record that the entrypoint activated OIDC scope compatibility."""
            capture["setup_scope_compatibility_calls"] = (
                capture.get("setup_scope_compatibility_calls", 0) + 1
            )

    return MockOIDCProxy


async def started_oidc_server(
    monkeypatch: pytest.MonkeyPatch,
    *,
    port: int = 8086,
    path: str = "/mcp",
    **env: str | None,
) -> tuple[FastMCP, dict[str, Any], dict[str, Any]]:
    """Run ``ha-mcp-oidc`` (``main_oidc``) with ``env`` (``None`` unsets) on top
    of a valid OIDC setup, the serve call returning at once: the real server it
    served, what the provider was given, and the serve call's arguments."""
    for name, value in {
        "OIDC_CONFIG_URL": CONFIG_URL,
        "OIDC_CLIENT_ID": "test-id",
        "OIDC_CLIENT_SECRET": "test-secret",
        "MCP_BASE_URL": BASE_URL,
        "MCP_PORT": str(port),
        "MCP_SECRET_PATH": path,
        "HOMEASSISTANT_URL": "http://127.0.0.1:8123",
        # OIDC mode shares one Home Assistant credential across its users.
        "HOMEASSISTANT_TOKEN": "oidc-shared-token",
        "MCP_SETTINGS_SECRET_PATH": None,
        "SUPERVISOR_TOKEN": None,
        **env,
    }.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    capture: dict[str, Any] = {}
    monkeypatch.setattr(OIDC_PROXY_PATCH_TARGET, make_mock_oidc_proxy(capture))
    # caplog listens on the root logger, which the CLI's setup replaces.
    monkeypatch.setattr("ha_mcp.__main__.setup_logging", lambda *a, **kw: None)
    runs: list[Coroutine[Any, Any, Any]] = []
    monkeypatch.setattr("ha_mcp.__main__.run_entrypoint", lambda c, _: runs.append(c))
    served: list[tuple[FastMCP, dict[str, Any]]] = []

    async def serve(self: FastMCP, **kwargs: Any) -> None:
        served.append((self, kwargs))

    monkeypatch.setattr(FastMCP, "run_async", serve)
    main_oidc()
    [run] = runs
    await asyncio.gather(run)
    [(mcp, run_kwargs)] = served
    return mcp, capture, run_kwargs
