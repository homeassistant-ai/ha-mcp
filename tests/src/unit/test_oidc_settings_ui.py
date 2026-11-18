"""OIDC provider compatibility and settings route isolation."""

import inspect

from ha_mcp._vendor.fastmcp.server.auth.oidc_proxy import OIDCProxy
from ha_mcp.settings_ui import get_http_settings_prefix, is_http_settings_mounted

from ._oauth_startup import route_paths
from ._oidc_test_helpers import make_mock_oidc_proxy, started_oidc_server


def test_every_provider_argument_startup_passes_is_accepted_by_oidc_proxy():
    """The stand-in provider pins the arguments startup passes; FastMCP's real
    ``OIDCProxy`` must still accept each of them."""
    passed = set(inspect.signature(make_mock_oidc_proxy({}).__init__).parameters)
    passed.discard("self")

    accepted = set(inspect.signature(OIDCProxy.__init__).parameters)

    assert passed <= accepted, f"OIDCProxy no longer accepts {passed - accepted}"


async def _paths(monkeypatch, **env):
    mcp, _, _ = await started_oidc_server(monkeypatch, **env)
    return route_paths(mcp.http_app(path="/mcp", stateless_http=True).routes)


async def test_settings_are_served_off_a_secret_path_never_under_the_mcp_path(
    monkeypatch,
):
    """OIDC serves the settings UI behind a dedicated secret path, never the
    OIDC-known MCP path, whose custom routes bypass auth (GHSA-mx64-982r-65vg)."""
    paths = await _paths(monkeypatch, HA_MCP_DISABLE_SETTINGS_UI=None)

    assert any(p.startswith("/private_") and p.endswith("/settings") for p in paths)
    assert not any(p.startswith("/mcp/") and "settings" in p for p in paths)
    assert get_http_settings_prefix() is None
    assert is_http_settings_mounted() is True


async def test_the_disable_switch_mounts_no_settings(monkeypatch):
    paths = await _paths(monkeypatch, HA_MCP_DISABLE_SETTINGS_UI="1")

    assert not any("settings" in p for p in paths)
    assert is_http_settings_mounted() is False
