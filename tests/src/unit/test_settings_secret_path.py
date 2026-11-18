"""OAuth startup mounts settings at a secret route never disclosed to MCP clients."""

import logging

import pytest

from ha_mcp.settings_ui import (
    get_http_settings_prefix,
    is_http_settings_mounted,
    reset_http_settings_state,
)

from ._oauth_startup import route_paths, started_oauth_server


@pytest.fixture(autouse=True)
def reset_settings():
    reset_http_settings_state()
    yield
    reset_http_settings_state()


async def _paths(monkeypatch, **env):
    mcp = await started_oauth_server(monkeypatch, **env)
    return route_paths(mcp.http_app(path="/mcp", stateless_http=True).routes)


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " on "])
async def test_truthy_values_disable_even_an_explicit_path(monkeypatch, value):
    paths = await _paths(
        monkeypatch,
        HA_MCP_DISABLE_SETTINGS_UI=value,
        MCP_SETTINGS_SECRET_PATH="/private_custom",
    )
    assert not any("settings" in p for p in paths)
    assert not is_http_settings_mounted()


@pytest.mark.parametrize("value", [None, "0", "false", "no", "off", "", "  "])
async def test_falsy_or_unset_values_keep_settings_enabled(monkeypatch, value):
    paths = await _paths(monkeypatch, HA_MCP_DISABLE_SETTINGS_UI=value)
    assert any(p.startswith("/private_") and p.endswith("/settings") for p in paths)
    assert is_http_settings_mounted()
    assert get_http_settings_prefix() is None


@pytest.mark.parametrize("value", ["disable", "disabled"])
async def test_an_unrecognized_disable_value_fails_closed_and_warns(
    monkeypatch, caplog, value
):
    with caplog.at_level(logging.WARNING):
        paths = await _paths(monkeypatch, HA_MCP_DISABLE_SETTINGS_UI=value)
    assert not any("settings" in p for p in paths)
    assert "not a recognized" in caplog.text


@pytest.mark.parametrize("path", ["/private_fixed", "private_fixed"])
async def test_an_explicit_path_is_normalized_mounted_and_not_advertised(
    monkeypatch, path
):
    paths = await _paths(
        monkeypatch, MCP_SETTINGS_SECRET_PATH=path, HA_MCP_DISABLE_SETTINGS_UI=None
    )
    assert "/private_fixed/settings" in paths
    assert "/private_fixed/api/settings/tools" in paths
    assert "/mcp/api/settings/advanced" not in paths
    assert "/settings" not in paths
    assert get_http_settings_prefix() is None
    assert is_http_settings_mounted()


async def test_generated_paths_are_unique_and_have_entropy(monkeypatch):
    first = await _paths(monkeypatch, HA_MCP_DISABLE_SETTINGS_UI=None)
    second = await _paths(monkeypatch, HA_MCP_DISABLE_SETTINGS_UI=None)
    one = next(
        p for p in first if p.startswith("/private_") and p.endswith("/settings")
    )
    two = next(
        p for p in second if p.startswith("/private_") and p.endswith("/settings")
    )
    assert one != two
    assert len(one.removesuffix("/settings")) > len("/private_") + 15


@pytest.mark.parametrize("path", ["/mcp", "/mcp/", "/", "/private_{token}"])
async def test_unsafe_secret_paths_mount_no_settings_routes(monkeypatch, path):
    paths = await _paths(
        monkeypatch, MCP_SETTINGS_SECRET_PATH=path, HA_MCP_DISABLE_SETTINGS_UI=None
    )
    assert not any("settings" in p for p in paths)
    assert not is_http_settings_mounted()
