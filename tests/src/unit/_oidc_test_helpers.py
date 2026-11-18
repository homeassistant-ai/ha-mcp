"""Run OIDC startup on the real server; only the identity provider is replaced."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from ha_mcp.__main__ import reset_main_runtime, run_oidc_server
from ha_mcp._vendor.fastmcp import FastMCP
from ha_mcp._vendor.fastmcp.server.auth.auth import AuthProvider
from ha_mcp.settings_ui import reset_http_settings_state

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
    kwargs ``run_oidc_server`` passes: a kwarg not listed here raises TypeError
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


@pytest.fixture(autouse=True)
def isolated_oidc_runtime() -> Iterator[None]:
    """Clear startup state before a test and on every exit after it."""
    reset_main_runtime()
    reset_http_settings_state()
    try:
        yield
    finally:
        reset_main_runtime()
        reset_http_settings_state()


async def started_oidc_server(
    monkeypatch: pytest.MonkeyPatch,
    *,
    port: int = 8086,
    path: str = "/mcp",
    **env: str | None,
) -> tuple[FastMCP, dict[str, Any], dict[str, Any]]:
    """Run ``run_oidc_server`` with ``env`` (``None`` unsets) and the serve call
    returning at once: the real server it served, what the provider was given,
    and the serve call's arguments."""
    monkeypatch.setenv("HOMEASSISTANT_URL", "http://127.0.0.1:8123")
    # OIDC mode shares one Home Assistant credential across its users.
    monkeypatch.setenv("HOMEASSISTANT_TOKEN", "oidc-shared-token")
    monkeypatch.delenv("MCP_SETTINGS_SECRET_PATH", raising=False)
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    for name, value in env.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    capture: dict[str, Any] = {}
    monkeypatch.setattr(OIDC_PROXY_PATCH_TARGET, make_mock_oidc_proxy(capture))
    served: list[tuple[FastMCP, dict[str, Any]]] = []

    async def serve(self: FastMCP, **kwargs: Any) -> None:
        served.append((self, kwargs))

    monkeypatch.setattr(FastMCP, "run_async", serve)
    await run_oidc_server(
        config_url=CONFIG_URL,
        client_id="test-id",
        client_secret="test-secret",
        base_url=BASE_URL,
        host="0.0.0.0",
        port=port,
        path=path,
    )
    [(mcp, run_kwargs)] = served
    return mcp, capture, run_kwargs
