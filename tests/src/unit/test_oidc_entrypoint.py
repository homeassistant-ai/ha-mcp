"""OIDC startup forwards configured provider and transport options."""

import pytest

from ha_mcp._vendor import fastmcp

from ._oidc_test_helpers import BASE_URL, CONFIG_URL, started_oidc_server

_ABSENT = object()


async def test_the_provider_requires_openid_and_its_scope_compatibility(monkeypatch):
    """OIDC mode requests the scope that makes a provider return an ID token."""
    mcp, proxy, _ = await started_oidc_server(monkeypatch)

    assert proxy["config_url"] == CONFIG_URL
    assert proxy["client_id"] == "test-id"
    assert proxy["client_secret"] == "test-secret"
    assert proxy["base_url"] == BASE_URL
    assert proxy["require_authorization_consent"] == "external"
    assert proxy["required_scopes"] == ["openid"]
    assert proxy["setup_scope_compatibility_calls"] == 1
    assert mcp.auth is proxy["instance"]


async def test_the_server_is_served_over_http_at_the_configured_port_and_path(
    monkeypatch,
):
    """OIDC uses the "http" transport, matching the sibling HTTP modes."""
    _, _, run_kwargs = await started_oidc_server(monkeypatch, port=9000, path="/custom")

    assert run_kwargs["transport"] == "http"
    assert run_kwargs["port"] == 9000
    assert run_kwargs["path"] == "/custom"


async def test_the_host_origin_guard_is_off_before_the_server_runs(monkeypatch):
    """OIDC runs behind a public proxy or LAN hostname: FastMCP's DNS-rebinding
    guard would answer proxied requests 403/421 before OIDC auth runs."""
    monkeypatch.setattr(fastmcp.settings, "http_host_origin_protection", True)
    monkeypatch.delenv("FASTMCP_HTTP_HOST_ORIGIN_PROTECTION", raising=False)

    await started_oidc_server(monkeypatch)

    assert fastmcp.settings.http_host_origin_protection is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [("test-jwt-key", "test-jwt-key"), (None, None), ("", None)],
    ids=["set", "unset", "empty-keeps-the-derived-default"],
)
async def test_the_jwt_signing_key_comes_from_the_environment(
    monkeypatch, value, expected
):
    """An empty key must not bypass FastMCP's derive-from-client-secret default."""
    _, proxy, _ = await started_oidc_server(monkeypatch, OIDC_JWT_SIGNING_KEY=value)

    assert proxy["jwt_signing_key"] == expected


@pytest.mark.parametrize(
    ("value", "expected", "warns"),
    [
        (
            " https://a.example.com/cb , ,https://b.example.com/cb ",
            ["https://a.example.com/cb", "https://b.example.com/cb"],
            False,
        ),
        ("https://a.example.com/cb", ["https://a.example.com/cb"], False),
        (None, _ABSENT, True),
        (" , , ", _ABSENT, True),
    ],
    ids=["list", "single", "unset", "separators-only"],
)
async def test_allowed_redirect_uris_are_passed_or_their_absence_is_warned(
    monkeypatch, caplog, value, expected, warns
):
    """Without an allowlist any registered client's redirect URI is accepted,
    which an internet-facing deployment needs to hear about."""
    with caplog.at_level("WARNING"):
        _, proxy, _ = await started_oidc_server(
            monkeypatch, OIDC_ALLOWED_CLIENT_REDIRECT_URIS=value
        )

    assert proxy.get("allowed_client_redirect_uris", _ABSENT) == expected
    warned = any(
        "OIDC_ALLOWED_CLIENT_REDIRECT_URIS" in record.message
        for record in caplog.records
    )
    assert warned is warns


@pytest.mark.parametrize(
    ("value", "expected"),
    [("true", True), ("false", _ABSENT), (None, _ABSENT)],
    ids=["true", "false", "unset"],
)
async def test_id_token_verification_is_passed_only_when_enabled(
    monkeypatch, value, expected
):
    _, proxy, _ = await started_oidc_server(monkeypatch, OIDC_VERIFY_ID_TOKEN=value)

    assert proxy.get("verify_id_token", _ABSENT) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("https://mcp.example.com/mcp", "https://mcp.example.com/mcp"), (None, _ABSENT)],
    ids=["set", "unset"],
)
async def test_the_audience_is_passed_only_when_set(monkeypatch, value, expected):
    _, proxy, _ = await started_oidc_server(monkeypatch, OIDC_AUDIENCE=value)

    assert proxy.get("audience", _ABSENT) == expected
