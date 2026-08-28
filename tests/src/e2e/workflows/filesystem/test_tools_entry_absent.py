"""E2E coverage for the topology where the File & YAML Tools entry is absent (#2292).

Runs only on the no-tools lanes (``E2E_NO_TOOLS_ENTRY=1``), which come in two
shapes — both real installs, both previously untested:

- **no component at all** (plain container, HAOS inaddon): the user never
  installed the HA-MCP integration.
- **server entry only** (container ``embedded``, HAOS ``embedded``): the
  integration is installed and its in-process "HA-MCP Server" entry is set up,
  but the second "HA-MCP File & YAML Tools" entry was never added. Since
  component 2.1.0 that server entry also registers the ``ha_mcp_tools/*``
  WebSocket surface (#2291), so the shared component capabilities answer while
  the privileged filesystem / YAML *services* stay gone.

What this module pins, per #2292's acceptance criteria:

1. Every gated tool fails with the structured, actionable "add the entry"
   error rather than an opaque service-not-found (``_raise_tools_entry_not_set_up``
   in ``src/ha_mcp/tools/tools_filesystem.py``). The tools themselves ARE
   registered here — the feature flags stay on — so a missing tool is a
   failure in its own right.
2. Home Assistant exposes no ``ha_mcp_tools`` service domain at all: the
   privileged services register only in the tools entry's setup.
3. Shared capabilities still work, per topology — the component WS surface on
   the server-entry lanes, the legacy REST path where there is no component.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx
import pytest
from test_constants import TEST_TOKEN

from ...utilities.assertions import MCPAssertions, parse_mcp_result

logger = logging.getLogger(__name__)

pytestmark = pytest.mark.no_tools_only

# Minimal valid arguments per gated tool, each chosen to reach the component
# availability check rather than tripping an earlier guard:
# ``ha_delete_file`` needs ``confirm=True``, and ``ha_config_set_yaml`` uses
# ``remove`` so it skips the storage-mode dashboard collision probe.
_GATED_TOOL_CALLS: dict[str, dict[str, Any]] = {
    "ha_list_files": {"path": "www/"},
    "ha_read_file": {"path": "configuration.yaml"},
    "ha_write_file": {
        "path": "www/e2e_no_tools_entry.txt",
        "content": "should never be written",
    },
    "ha_delete_file": {"path": "www/e2e_no_tools_entry.txt", "confirm": True},
    "ha_config_get_yaml": {"yaml_path": "homeassistant"},
    "ha_config_set_yaml": {"yaml_path": "command_line", "action": "remove"},
}

# Every gated tool reports COMPONENT_NOT_INSTALLED at the TOP level. The write
# tools carry ``@with_auto_backup(mandatory=True)``, whose pre-write snapshot
# reads the file through the same missing services and so fails first — the
# decorator unwraps that specific cause back to the component error
# (``_component_not_installed_cause`` in ``src/ha_mcp/tools/auto_backup.py``)
# instead of burying it inside BACKUP_CAPTURE_FAILED, so the caller always
# sees "add the entry" as the primary error (#2292).
_EXPECTED_ERROR_CODE = "COMPONENT_NOT_INSTALLED"

# Stable substrings of ``_raise_tools_entry_not_set_up``'s message +
# suggestions. Deliberately not the exact prose — the wording is free to
# improve; what must survive is naming the entry and how to add it.
_ACTIONABLE_SUBSTRINGS = (
    "COMPONENT_NOT_INSTALLED",
    "HA-MCP File & YAML Tools",
    "Add entry",
)


def _server_entry_present() -> bool:
    """True on the lanes that still set up the in-process server entry.

    Both embedded lanes keep the component installed and its "HA-MCP Server"
    entry set up; every other no-tools lane has no active ha_mcp_tools config
    entry at all.
    """
    return (
        os.environ.get("E2E_BACKEND") == "embedded"
        or os.environ.get("HAOS_TEST_MODE") == "embedded"
    )


@pytest.mark.filesystem
@pytest.mark.parametrize("tool_name", sorted(_GATED_TOOL_CALLS))
async def test_gated_tool_fails_actionably(mcp_client, tool_name):
    """Each gated tool fails with the actionable "add the entry" error.

    Pins acceptance criterion 1: without the tools entry the six privileged
    tools are unusable, and the caller is told exactly what to add rather than
    getting an opaque service-not-found.
    """
    tools = {tool.name for tool in await mcp_client.list_tools()}
    assert tool_name in tools, (
        f"{tool_name} is not registered on this lane. The no-tools lanes keep "
        f"the feature flags ON — the tools must exist and refuse, not vanish. "
        f"Registered: {sorted(tools)}"
    )

    async with MCPAssertions(mcp_client) as mcp:
        data = await mcp.call_tool_failure(tool_name, _GATED_TOOL_CALLS[tool_name])

    error = data.get("error")
    assert isinstance(error, dict), f"{tool_name} should fail structurally: {data}"
    assert error.get("code") == _EXPECTED_ERROR_CODE, (
        f"{tool_name} reported {error.get('code')!r}, expected "
        f"{_EXPECTED_ERROR_CODE!r}: {data}"
    )

    serialized = str(data)
    missing = [text for text in _ACTIONABLE_SUBSTRINGS if text not in serialized]
    assert not missing, (
        f"{tool_name}'s failure does not tell the user how to fix it "
        f"(missing {missing}): {data}"
    )
    logger.info("%s refused with %s and actionable guidance", tool_name, error["code"])


@pytest.mark.filesystem
async def test_ha_mcp_tools_service_domain_absent(ha_container_with_fresh_config):
    """Home Assistant exposes no ``ha_mcp_tools`` services at all.

    Pins acceptance criterion 2, HA-side rather than through the server: the
    privileged services register in the tools entry's ``async_setup_entry``
    only, so without that entry the domain never appears — including on the
    server-entry lanes, where the component IS loaded.
    """
    base_url = ha_container_with_fresh_config["base_url"]
    token = ha_container_with_fresh_config.get("token", TEST_TOKEN)

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{base_url}/api/services",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200, f"/api/services failed: {resp.text!r}"

    domains = {entry.get("domain") for entry in resp.json() if isinstance(entry, dict)}
    assert "ha_mcp_tools" not in domains, (
        "ha_mcp_tools services are registered, so the File & YAML Tools entry "
        "IS set up — this lane is not testing the topology it claims to. "
        "Check the E2E_NO_TOOLS_ENTRY staging in conftest / haos_runtime."
    )
    logger.info("No ha_mcp_tools service domain — gated services are absent")


@pytest.mark.filesystem
async def test_shared_capabilities_survive_per_topology(mcp_client):
    """Shared, non-gated capabilities still work — via whichever path the
    topology provides.

    Pins acceptance criterion 3. On the server-entry lanes the component's
    ``ha_mcp_tools/info`` probe answers even with no tools entry (#2291), which
    is exactly what makes the gating meaningful rather than "the component is
    simply gone"; ``ha_report_issue``'s diagnostics report that probe's result.
    Where there is no component, the same shared surface must still be served
    by the legacy REST path.
    """
    diagnostics = parse_mcp_result(
        await mcp_client.call_tool("ha_report_issue", {"fields": "diagnostic_info"})
    )
    info = diagnostics.get("diagnostic_info", {})
    assert isinstance(info, dict), f"no diagnostic_info in response: {diagnostics}"

    # Same verdict on every no-tools lane: the entry is not set up.
    assert "not set up" in (info.get("tools_entry_status") or ""), (
        f"tools_entry_status should report the missing entry: {info}"
    )

    if _server_entry_present():
        # The WS surface answers, so the version probe resolves — proof the
        # server entry registers ``ha_mcp_tools/info`` on its own (#2291).
        assert info.get("component_version"), (
            f"the server entry should still answer the component caps probe: {info}"
        )
        # And a shared component-backed capability still serves normally.
        helpers = parse_mcp_result(
            await mcp_client.call_tool("ha_config_list_helpers", {"helper_type": "all"})
        )
        assert helpers.get("success"), f"shared helper listing failed: {helpers}"
        logger.info(
            "Server entry present: caps answer (component %s), shared tools work",
            info["component_version"],
        )
        return

    # No component at all — the shared surface falls back to legacy REST.
    assert info.get("component_version") is None, (
        f"no component is installed on this lane, yet caps reported one: {info}"
    )
    search = parse_mcp_result(
        await mcp_client.call_tool("ha_search", {"domain_filter": "light", "limit": 5})
    )
    assert search.get("success"), f"legacy-path search failed: {search}"
    logger.info("No component: shared tools still served by the legacy REST path")
