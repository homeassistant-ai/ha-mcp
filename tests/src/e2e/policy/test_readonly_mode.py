"""Real e2e tests for Read Only Mode (#1569).

Boots a fresh in-process ha-mcp server with ``READ_ONLY_MODE=true``
against the testcontainer HA (function-scoped — the session-scoped
``mcp_client`` boots without the flag) and verifies the full contract:

- write-capable tools disappear from ``tools/list``;
- pure read tools keep working;
- exempt mixed read/write tools stay listed, their read actions work,
  and their write actions return the structured READ_ONLY_MODE error;
- direct calls to hidden write tools return the same error;
- with tool search enabled, proxy-dispatched writes are blocked too;
- ``ha_get_overview`` reports the mode to the LLM;
- the real catalog satisfies the invariant that every mandatory tool is
  either read-safe or exempt (a unit test cannot see the real registered
  catalog, so this real-catalog check lives in the e2e suite).

Requires Docker (testcontainers); runs in CI.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from test_constants import TEST_TOKEN

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.read_only import READ_ONLY_EXEMPT_TOOLS, is_read_safe
from ha_mcp.server import HomeAssistantSmartMCPServer
from ha_mcp.settings_ui import MANDATORY_TOOLS
from ha_mcp.utils.data_paths import get_data_dir

from ..utilities.assertions import parse_mcp_result, tool_error_to_result


async def _expect_read_only_blocked(
    client: Client, tool: str, args: dict[str, Any]
) -> dict[str, Any]:
    """Call ``tool`` and return the parsed READ_ONLY_MODE error body.

    Accept both the raised-ToolError and isError-result transports (see
    test_approval_flow._expect_blocked for the rationale).
    """
    try:
        result = await client.call_tool(tool, args)
    except ToolError as exc:
        body = tool_error_to_result(exc)
    else:
        body = parse_mcp_result(result)
    assert body.get("error", {}).get("code") == "READ_ONLY_MODE", body
    return body


async def _build_readonly_server(
    container_info, monkeypatch, tmp_path, *, extra_env: dict[str, str] | None = None
):
    if container_info.get("backend") == "haos_inaddon":
        pytest.skip(
            "Inaddon backend uses the addon's own MCP endpoint; this test "
            "needs an in-process server with READ_ONLY_MODE=true."
        )

    monkeypatch.setenv("READ_ONLY_MODE", "true")
    for key, value in (extra_env or {}).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(tmp_path))
    get_data_dir.cache_clear()

    # Reset cached settings so the new server picks up the env vars.
    import ha_mcp.config

    monkeypatch.setattr(ha_mcp.config, "_settings", None)

    base_url = container_info["base_url"]
    token = container_info.get("token", TEST_TOKEN)
    ha_client = HomeAssistantClient(base_url=base_url, token=token)
    server = HomeAssistantSmartMCPServer(client=ha_client)
    return server, ha_client


@pytest.fixture
async def readonly_mcp(ha_container_with_fresh_config, monkeypatch, tmp_path):
    """Read-only server with the default full catalog (tool search off)."""
    server, ha_client = await _build_readonly_server(
        ha_container_with_fresh_config, monkeypatch, tmp_path
    )
    client = Client(server.mcp)
    async with client:
        yield client, server
    await ha_client.close()
    get_data_dir.cache_clear()


@pytest.fixture
async def readonly_toolsearch_mcp(
    ha_container_with_fresh_config, monkeypatch, tmp_path
):
    """Read-only server with tool search on — exercises proxy dispatch."""
    server, ha_client = await _build_readonly_server(
        ha_container_with_fresh_config,
        monkeypatch,
        tmp_path,
        extra_env={"ENABLE_TOOL_SEARCH": "true"},
    )
    client = Client(server.mcp)
    async with client:
        yield client, server
    await ha_client.close()
    get_data_dir.cache_clear()


@pytest.fixture
async def readonly_codemode_mcp(ha_container_with_fresh_config, monkeypatch, tmp_path):
    """Read-only server with code mode enabled so ha_manage_custom_tool
    (a mixed read/write exempt tool) is registered — its list_saved read
    must work while code execution is blocked."""
    server, ha_client = await _build_readonly_server(
        ha_container_with_fresh_config,
        monkeypatch,
        tmp_path,
        extra_env={"ENABLE_CODE_MODE": "true", "ENABLE_BETA_FEATURES": "true"},
    )
    client = Client(server.mcp)
    async with client:
        yield client, server
    await ha_client.close()
    get_data_dir.cache_clear()


@pytest.mark.asyncio
async def test_write_tools_hidden_exempt_and_read_tools_listed(readonly_mcp):
    client, _server = readonly_mcp
    tools = await client.list_tools()
    names = {t.name for t in tools}

    # Pure write tools are gone from the catalog.
    for write_tool in ("ha_config_set_scene", "ha_restart", "ha_call_service"):
        assert write_tool not in names, f"{write_tool} should be hidden"

    # Pure read tools and always-registered exempt mixed tools remain.
    for kept in (
        "ha_get_state",
        "ha_search",
        "ha_manage_backup",
        "ha_manage_pipeline",
        "ha_manage_energy_prefs",
        "ha_manage_radio",
    ):
        assert kept in names, f"{kept} should stay listed"

    # Everything still listed is either annotated read-only or exempt.
    for tool in tools:
        assert is_read_safe(tool) or tool.name in READ_ONLY_EXEMPT_TOOLS, (
            f"{tool.name} is write-capable but survived the catalog filter"
        )


@pytest.mark.asyncio
async def test_real_catalog_mandatory_tools_stay_available(readonly_mcp):
    """Every MANDATORY tool must be read-safe or exempt — otherwise
    read-only mode would block a tool the settings UI refuses to
    disable. Run against the REAL registered catalog so a future
    mandatory write tool fails here at PR time."""
    _client, server = readonly_mcp
    catalog = await server.mcp.local_provider._list_tools()
    by_name = {t.name: t for t in catalog}
    for name in MANDATORY_TOOLS:
        tool = by_name.get(name)
        assert tool is not None, f"mandatory tool {name} not registered"
        assert is_read_safe(tool) or name in READ_ONLY_EXEMPT_TOOLS, (
            f"mandatory tool {name} is write-capable but not exempt — "
            "read-only mode would dead-end it"
        )
    # Exempt names must reference real tools (typo guard). Feature-gated
    # tools are only registered when their flag is on — this fixture has
    # code mode off, so ha_manage_custom_tool is skipped here; its real
    # registration + read/write classification is covered against a
    # code-mode-enabled server by
    # test_code_mode_tool_read_works_and_execution_blocked.
    feature_gated = {"ha_manage_custom_tool"}
    for name in READ_ONLY_EXEMPT_TOOLS:
        if name in feature_gated:
            continue
        assert name in by_name, f"exempt tool {name} not found in catalog"


@pytest.mark.asyncio
async def test_read_tools_still_work(readonly_mcp):
    client, _server = readonly_mcp
    result = await client.call_tool("ha_search", {"query": "light"})
    body = parse_mcp_result(result)
    assert body.get("success") is True, body


@pytest.mark.asyncio
async def test_overview_reports_read_only_mode(readonly_mcp):
    client, _server = readonly_mcp
    result = await client.call_tool("ha_get_overview", {})
    body = parse_mcp_result(result)
    assert body.get("read_only_mode") is True, body
    assert "Read Only Mode is ON" in body.get("read_only_mode_hint", ""), body


@pytest.mark.asyncio
async def test_direct_write_tool_call_blocked(readonly_mcp):
    client, _server = readonly_mcp
    body = await _expect_read_only_blocked(
        client,
        "ha_call_service",
        {"domain": "light", "service": "turn_on", "entity_id": "light.bed_light"},
    )
    assert body["tool_name"] == "ha_call_service"
    # The error must point the user at the toggle.
    assert "Read Only Mode" in body["error"]["message"]


@pytest.mark.asyncio
async def test_exempt_tool_read_action_works(readonly_mcp):
    client, _server = readonly_mcp
    result = await client.call_tool(
        "ha_manage_backup", {"scope": "edits", "action": "list"}
    )
    body = parse_mcp_result(result)
    assert body.get("success") is True, body


@pytest.mark.asyncio
async def test_exempt_tool_write_action_blocked(readonly_mcp):
    client, _server = readonly_mcp
    body = await _expect_read_only_blocked(
        client, "ha_manage_backup", {"scope": "snapshot", "action": "create"}
    )
    assert body["blocked_operation"], body
    # The error teaches what remains available on this tool.
    assert "list" in body["error"]["message"], body


@pytest.mark.asyncio
async def test_radio_read_action_works(readonly_mcp):
    """ha_manage_radio is exempt: a read action (network_status) stays
    callable in read-only mode. Z-Wave JS is not configured on the test
    container, so this also exercises the graceful integration-absent read
    path (available=False but success=True)."""
    client, _server = readonly_mcp
    result = await client.call_tool(
        "ha_manage_radio", {"radio": "zwave", "action": "network_status"}
    )
    body = parse_mcp_result(result)
    assert body.get("success") is True, body


@pytest.mark.asyncio
async def test_radio_write_action_blocked(readonly_mcp):
    """A ha_manage_radio write action (zwave 'add') is blocked with the
    structured READ_ONLY_MODE error before the handler runs."""
    client, _server = readonly_mcp
    body = await _expect_read_only_blocked(
        client, "ha_manage_radio", {"radio": "zwave", "action": "add"}
    )
    assert body["tool_name"] == "ha_manage_radio", body
    assert body["blocked_operation"], body


@pytest.mark.asyncio
async def test_proxy_dispatched_write_blocked_with_tool_search(readonly_toolsearch_mcp):
    """ha_call_write_tool re-dispatches through the middleware chain, so
    the inner call must hit the read-only blocker even though the proxy
    itself passes through."""
    client, _server = readonly_toolsearch_mcp
    try:
        result = await client.call_tool(
            "ha_call_write_tool",
            {
                "name": "ha_set_zone",
                "arguments": {
                    "name": "ro_test_zone",
                    "latitude": 1.0,
                    "longitude": 1.0,
                },
            },
        )
    except ToolError as exc:
        body = tool_error_to_result(exc)
    else:
        body = parse_mcp_result(result)
    assert body.get("error", {}).get("code") == "READ_ONLY_MODE", body


@pytest.mark.asyncio
async def test_energy_prefs_dry_run_works_and_get_is_unchanged(readonly_mcp):
    """The exempt energy tool's dry-run preview (mode='set', dry_run=True)
    is a non-writing path and must succeed in read-only mode; a follow-up
    mode='get' must show prefs were not mutated. An empty config produces
    no shape errors and never writes (dry_run skips the config_hash
    requirement)."""
    client, _server = readonly_mcp

    before = parse_mcp_result(
        await client.call_tool("ha_manage_energy_prefs", {"mode": "get"})
    )
    assert before.get("success") is True, before

    dry = parse_mcp_result(
        await client.call_tool(
            "ha_manage_energy_prefs",
            {"mode": "set", "config": {}, "dry_run": True},
        )
    )
    assert dry.get("success") is True, dry
    assert dry.get("dry_run") is True, dry

    after = parse_mcp_result(
        await client.call_tool("ha_manage_energy_prefs", {"mode": "get"})
    )
    assert after.get("success") is True, after
    # The dry-run must not have changed the persisted config hash.
    assert after.get("config_hash") == before.get("config_hash"), (before, after)


@pytest.mark.asyncio
async def test_string_envelope_proxy_write_blocked(readonly_toolsearch_mcp):
    """The categorized proxy tolerates ``arguments`` as a JSON STRING and
    parses it AFTER the read-only middleware runs, so the middleware must
    coerce it too. A string-envelope write action of an exempt tool must
    still surface READ_ONLY_MODE (this also pins the proxy re-dispatch
    middleware re-entry)."""
    client, server = readonly_toolsearch_mcp
    catalog = await server.mcp.local_provider._list_tools()
    names = {t.name for t in catalog}

    # ha_manage_addon registers unconditionally (not supervisor-gated), but
    # fall back to the always-registered energy tool if a future change
    # gates it, so this test stays meaningful on a non-HAOS container.
    if "ha_manage_addon" in names:
        inner_name = "ha_manage_addon"
        inner_args = '{"slug": "x", "action": "install"}'
    else:
        inner_name = "ha_manage_energy_prefs"
        inner_args = '{"mode": "set", "config": {}}'

    try:
        result = await client.call_tool(
            "ha_call_write_tool",
            {"name": inner_name, "arguments": inner_args},
        )
    except ToolError as exc:
        body = tool_error_to_result(exc)
    else:
        body = parse_mcp_result(result)
    assert body.get("error", {}).get("code") == "READ_ONLY_MODE", body
    assert body.get("tool_name") == inner_name, body


@pytest.mark.asyncio
async def test_search_results_exclude_non_exempt_write_tools(readonly_toolsearch_mcp):
    """ha_search_tools must never surface a hidden (non-exempt write) tool:
    the read-only catalog filter runs before the BM25 index is built, so a
    search can only return read-safe or exempt tools."""
    client, server = readonly_toolsearch_mcp
    result = await client.call_tool("ha_search_tools", {"query": "automation create"})
    body = parse_mcp_result(result)

    # Pull the result tool names from whatever shape the search returns —
    # currently a top-level list of tool dicts; tolerate a dict envelope
    # with a list under a conventional key too.
    entries: list = body if isinstance(body, list) else []
    if isinstance(body, dict):
        for key in ("tools", "results", "matches"):
            entries.extend(body.get(key) or [])
    result_names: set[str] = set()
    for entry in entries:
        if isinstance(entry, dict):
            name = entry.get("name") or entry.get("tool_name")
            if isinstance(name, str):
                result_names.add(name)
    # Guard against a vacuous pass: this query matches the read-side
    # automation tools, so an empty extraction means the response shape
    # drifted past the handling above, not that nothing matched.
    assert result_names, f"could not extract any tool names from search: {body}"

    # Known pure-write tools must be absent from the search surface.
    for write_tool in ("ha_config_set_automation", "ha_set_zone", "ha_call_service"):
        assert write_tool not in result_names, (
            f"{write_tool} is a non-exempt write tool but appeared in search: {body}"
        )

    # Stronger: every surfaced name must be read-safe or exempt, judged
    # against the UNFILTERED catalog.
    catalog = await server.mcp.local_provider._list_tools()
    by_name = {t.name: t for t in catalog}
    for name in result_names:
        tool = by_name.get(name)
        if tool is None:
            continue  # proxy meta-tool or alias — not a real catalog write
        assert is_read_safe(tool) or name in READ_ONLY_EXEMPT_TOOLS, (
            f"search surfaced write-capable {name} that is not exempt: {body}"
        )


@pytest.mark.asyncio
async def test_code_mode_tool_read_works_and_execution_blocked(readonly_codemode_mcp):
    """With code mode enabled, ha_manage_custom_tool (a mixed read/write
    exempt tool) is listed; its list_saved read works, and a code-execution
    call is blocked with READ_ONLY_MODE."""
    client, _server = readonly_codemode_mcp

    tools = await client.list_tools()
    names = {t.name for t in tools}
    if "ha_manage_custom_tool" not in names:
        pytest.skip(
            "ha_manage_custom_tool not registered (pydantic-monty missing or "
            "code mode unavailable in this environment)"
        )

    listed = parse_mcp_result(
        await client.call_tool("ha_manage_custom_tool", {"list_saved": True})
    )
    assert listed.get("success") is True, listed

    body = await _expect_read_only_blocked(
        client,
        "ha_manage_custom_tool",
        {"code": "1 + 1", "justification": "read-only mode test"},
    )
    assert body["tool_name"] == "ha_manage_custom_tool", body


@pytest.mark.inaddon_only
@pytest.mark.asyncio
async def test_inaddon_read_only_mode_blocks_radio_writes(
    ha_container_with_fresh_config, mcp_client
):
    """Read-only mode on the REAL inaddon add-on (every test above skips inaddon
    via ``_build_readonly_server``, which can only inject ``READ_ONLY_MODE`` into
    a fresh in-process server).

    Enables read_only_mode through the add-on's OWN settings API — which merges it
    into the Supervisor add-on options the production way (a bare options POST is
    full-replacement and would drop required keys) — self-restarts ONLY the add-on
    (~10s; Home Assistant is untouched) so it boots with ``READ_ONLY_MODE=true``,
    asserts ha_manage_radio's read action still works while a write is blocked with
    the structured READ_ONLY_MODE error, then RESTORES read-only to off.

    xdist runs a worker's tests serially and each worker owns an isolated add-on
    (``_haos_worker_setup``), so bracketing read-only around just this test and
    restoring it in ``finally`` is safe at any position. (Ordering tricks do NOT
    help — ``--dist loadscope`` groups tests by module, so a single "run last"
    marker can't make this the last thing on its worker.) The two dev-add-on
    restarts also drop the SHARED session ``mcp_client`` connection
    (test_supervisor_inaddon.py documents that restarting the dev add-on kills
    mcp_client for later tests), so the ``finally`` also warms that client back up
    so whatever module loadscope hands this worker next gets a live session.
    """
    import asyncio
    import time

    import httpx
    from fastmcp.client.transports import StreamableHttpTransport
    from haos_runtime import HA_MCP_TEST_SECRET_PATH, wait_for_addon_mcp_ready

    container_info = ha_container_with_fresh_config
    addon_mcp_url = container_info.get("addon_mcp_url")
    assert addon_mcp_url, "inaddon backend should expose addon_mcp_url"
    # The settings UI is mounted at the secret-path root (see TestSettingsUiRestartReal).
    base = addon_mcp_url.split("/mcp", 1)[0]
    settings = f"{base}{HA_MCP_TEST_SECRET_PATH}/api/settings"
    _transient = (AssertionError, TimeoutError, OSError, httpx.HTTPError, RuntimeError)

    async def _set_read_only(enabled: bool) -> None:
        """POST the flag (handler merges into Supervisor options) + self-restart
        the add-on. Empty restart body -> target='self', which the handler
        schedules in the background so this 200 flushes before the bounce."""
        async with httpx.AsyncClient(timeout=30.0) as http:
            resp = await http.post(
                f"{settings}/features", json={"flags": {"read_only_mode": enabled}}
            )
            assert resp.status_code == 200, (
                f"set read_only_mode={enabled}: {resp.status_code} {resp.text[:300]!r}"
            )
            resp = await http.post(f"{settings}/restart", json={})
            assert resp.status_code == 200, (
                f"restart add-on: {resp.status_code} {resp.text[:300]!r}"
            )

    async def _await_read_only(expected: bool) -> None:
        """Poll, reconnecting each round, until the add-on's CATALOG reflects
        read_only_mode == expected — i.e. the restart actually took effect. In
        read-only mode the catalog filter hides write tools, so ``ha_call_service``
        is absent iff read-only is on. Keying on a positive signal from a real
        ``list_tools`` response (rather than the absence of an overview key, which
        a degraded payload could also fake) ensures we only confirm against a
        healthy, fully-booted add-on."""
        deadline = time.monotonic() + 180.0
        last: object = None
        while time.monotonic() < deadline:
            try:
                url = wait_for_addon_mcp_ready(timeout=30.0)
                async with Client(StreamableHttpTransport(url=url)) as mcp:
                    names = {t.name for t in await mcp.list_tools()}
                if ("ha_call_service" not in names) is expected:
                    return
                last = len(names)
            except _transient as err:
                last = err
            await asyncio.sleep(3)
        raise AssertionError(
            f"read-only catalog did not become {expected} within 180s (last={last!r})"
        )

    try:
        await _set_read_only(True)
        await _await_read_only(True)
        url = wait_for_addon_mcp_ready(timeout=30.0)
        async with Client(StreamableHttpTransport(url=url)) as mcp:
            result = await mcp.call_tool(
                "ha_manage_radio", {"radio": "zwave", "action": "network_status"}
            )
            assert parse_mcp_result(result).get("success") is True, result
            blocked = await _expect_read_only_blocked(
                mcp, "ha_manage_radio", {"radio": "zwave", "action": "add"}
            )
            assert blocked.get("tool_name") == "ha_manage_radio", blocked
    finally:
        # Restore read-only OFF: this worker's remaining tests share the add-on
        # and need writes, so a leaked read-only would cascade READ_ONLY_MODE into
        # all of them. Retry the whole set+restart until it's confirmed off.
        restore_deadline = time.monotonic() + 300.0
        while True:
            try:
                await _set_read_only(False)
                await _await_read_only(False)
                break
            except _transient:
                if time.monotonic() >= restore_deadline:
                    raise
                await asyncio.sleep(3)
        # The dev-add-on restarts above dropped the SHARED session mcp_client's
        # connection. Warm it back up so the next test loadscope schedules on this
        # worker gets a live session rather than a stale one (a read tool is enough
        # to force re-establishment; retry while the add-on finishes coming up).
        warm_deadline = time.monotonic() + 120.0
        while True:
            try:
                await mcp_client.call_tool("ha_get_overview", {})
                break
            except _transient:
                if time.monotonic() >= warm_deadline:
                    raise
                await asyncio.sleep(3)
