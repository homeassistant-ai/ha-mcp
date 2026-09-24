"""Integration log levels through ha_set_integration(log_level=...)."""

import pytest

from ...utilities.assertions import MCPAssertions

_DOMAIN = "sun"


async def _logger_level(mcp: MCPAssertions, domain: str) -> str:
    raw = await mcp.call_tool_success(
        "ha_get_logs", {"source": "logger", "search": domain}
    )
    data = raw.get("data", raw)
    return next(e["level"] for e in data["loggers"] if e["domain"] == domain)


@pytest.mark.integrations
async def test_log_level_debug_then_default(mcp_client):
    """DEBUG takes effect immediately; DEFAULT restores the configured level."""
    mcp = MCPAssertions(mcp_client)
    try:
        result = await mcp.call_tool_success(
            "ha_set_integration", {"domain": _DOMAIN, "log_level": "DEBUG"}
        )
        assert result["action"] == "set_log_level"
        assert await _logger_level(mcp, _DOMAIN) == "DEBUG"
    finally:
        await mcp.call_tool_success(
            "ha_set_integration", {"domain": _DOMAIN, "log_level": "DEFAULT"}
        )
    assert await _logger_level(mcp, _DOMAIN) != "DEBUG"


@pytest.mark.integrations
async def test_log_level_unknown_integration_is_not_found(mcp_client):
    mcp = MCPAssertions(mcp_client)
    await mcp.call_tool_failure(
        "ha_set_integration",
        {"domain": "not_a_real_integration_xyz", "log_level": "DEBUG"},
        expected_error="not_a_real_integration_xyz",
    )
