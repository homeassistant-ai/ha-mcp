"""The withdrawn ``ha_mcp_tools/config_get`` command stays gone."""

from __future__ import annotations

from .test_component_ws_search import wsapi


class TestConfigGetWithdrawn:
    """``config_get`` was withdrawn before release: it served an entity's
    ``raw_config``, whose freshness lags the config file between a write and the
    next completed reload, so a get racing a reload returned a stale body. The
    command, its schema, its capability, and its domain gate are all gone — the
    get tools serve automation/script reads from the legacy REST path (which
    reads the fresh config file). These pin that nothing component-side still
    exposes it (issue #1813 tracks a possible file-reading redesign)."""

    def test_capability_not_advertised(self):
        assert "config_get" not in wsapi.CAPABILITIES

    def test_no_command_constant_schema_or_domain_gate(self):
        assert not hasattr(wsapi, "WS_CONFIG_GET")
        assert not hasattr(wsapi, "_config_get_schema")
        assert not hasattr(wsapi, "CONFIG_GET_DOMAINS")

    def test_no_handler_function(self):
        assert not hasattr(wsapi, "_do_config_get")
