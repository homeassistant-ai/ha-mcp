"""OIDC upstream stand-in shared by startup wiring tests."""

import pytest

# Sentinel used by MockOIDCProxy below to distinguish "kwarg not passed" from
# "kwarg passed with a falsy/None value" for the optional constructor args.
_UNSET = object()
OIDC_PROXY_PATCH_TARGET = "ha_mcp.auth.oidc_compat.HaMcpOIDCProxy"


def make_mock_oidc_proxy(capture: dict) -> type:
    """Build a MockOIDCProxy class pinned to production's exact kwarg set.

    Unlike a ``**kwargs``-catch-all mock, this has an explicit signature with
    exactly the kwargs ``run_oidc_server`` passes. If production ever passes
    a kwarg not listed here, this raises TypeError instead of silently
    swallowing it — catching drift in *our own* call, not in FastMCP's
    constructor (see ``TestOIDCProxySignatureSubset`` for that check).
    ``required_scopes`` is mandatory in production. The remaining optional
    constructor arguments default to the ``_UNSET`` sentinel and are only
    recorded in ``capture`` when actually passed.
    """

    class MockOIDCProxy:
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
def isolated_oidc_runtime():
    """Leave no startup state behind when the serve coroutine is intercepted."""
    from ha_mcp.__main__ import reset_main_runtime
    from ha_mcp.settings_ui import reset_http_settings_state

    reset_main_runtime()
    reset_http_settings_state()
    yield
    reset_main_runtime()
    reset_http_settings_state()
