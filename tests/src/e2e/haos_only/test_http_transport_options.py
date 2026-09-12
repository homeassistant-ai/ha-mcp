"""One serial app restart scenario; never instantiate the shared MCP client."""

from typing import Any

import httpx
import pytest

from ..utilities.addon_restart import (
    call_tool_fresh,
    get_instance_id,
    post_advanced_settings,
    restart_self,
    restore_advanced_settings,
    wait_for_addon_replacement,
)
from ..utilities.streamable_http import parse_mcp_response

_FIELDS = ("http_transport_diagnostics", "http_json_response", "log_level")


async def _read_settings(url: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30.0) as http:
        response = await http.get(url)
    response.raise_for_status()
    return {
        row["field"]: row["value"]
        for row in response.json()["fields"]
        if row["field"] in _FIELDS
    }


async def _probe_response(addon_url: str) -> str:
    """Check a real read-only tool response and return its wire content type."""
    async with httpx.AsyncClient(timeout=30.0) as http:
        response = await http.post(
            addon_url,
            headers={"Accept": "application/json, text/event-stream"},
            json={
                "jsonrpc": "2.0",
                "id": 2367,
                "method": "tools/call",
                "params": {"name": "ha_get_overview", "arguments": {}},
            },
        )
    response.raise_for_status()
    content_type = response.headers["content-type"]
    result = parse_mcp_response(content_type, response.content)
    assert result is not None and result.get("id") == 2367
    assert "error" not in result and not result["result"].get("isError", False)
    assert result["result"]["content"]
    return content_type


@pytest.mark.inaddon_only
@pytest.mark.addon_disruptive
async def test_http_options_apply_and_restore_after_restart(
    ha_container_with_fresh_config: dict[str, Any],
) -> None:
    """Enable both once, prove JSON and diagnostics, then restore and fence cleanup."""
    from haos_runtime import HA_MCP_TEST_SECRET_PATH

    addon_url = ha_container_with_fresh_config.get("addon_mcp_url")
    assert addon_url, "inaddon container_info has no addon_mcp_url"
    base = addon_url.split("/mcp", 1)[0]
    settings = f"{base}{HA_MCP_TEST_SECRET_PATH}/api/settings"
    advanced, restart, info = (
        f"{settings}/{part}" for part in ("advanced", "restart", "info")
    )
    saved = await _read_settings(advanced)
    assert set(saved) == set(_FIELDS)
    assert saved["http_transport_diagnostics"] is False
    assert saved["http_json_response"] is False
    baseline_content_type = await _probe_response(addon_url)
    baseline = await get_instance_id(info)
    data = await call_tool_fresh(addon_url, "ha_get_app", {})
    slug = next(
        a["slug"]
        for a in data["addons"]
        if a["name"] == "Home Assistant MCP Server (Dev)"
    )
    try:
        await post_advanced_settings(
            advanced,
            {
                "http_transport_diagnostics": True,
                "http_json_response": True,
                "log_level": "INFO",
            },
        )
        submission_error = None
        try:
            await restart_self(restart)
        except (httpx.HTTPError, TimeoutError) as error:
            submission_error = error
        await wait_for_addon_replacement(
            info, addon_url, baseline, submission_error=submission_error
        )
        assert (await _probe_response(addon_url)).startswith("application/json")
        logs = await call_tool_fresh(
            addon_url,
            "ha_get_logs",
            {
                "source": "supervisor",
                "slug": slug,
                "limit": 100,
                "search": "response_complete=True",
            },
        )
        assert "ha_mcp.http_transport" in logs["log"]
        assert "request_complete=True" in logs["log"]
        assert "response_complete=True" in logs["log"]
    finally:
        await restore_advanced_settings(
            advanced, restart, info, addon_url, changes=saved
        )
    assert await _read_settings(advanced) == saved
    assert await _probe_response(addon_url) == baseline_content_type
