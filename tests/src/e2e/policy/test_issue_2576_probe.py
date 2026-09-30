"""Diagnostic experiment for #2576; captures server behavior without an LLM."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_constants import TEST_TOKEN

import ha_mcp.config
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.server import HomeAssistantSmartMCPServer
from ha_mcp.utils.data_paths import get_data_dir

QUERIES = (
    "lumières allumées",
    "lights on",
    "light state",
    "get light status",
    "get current entity state",
    "ha_get_state",
    "ha_search",
    "which lights are on my desktop",
)


async def _rpc(
    http: httpx.AsyncClient, method: str, params: dict[str, Any]
) -> dict[str, Any]:
    response = await http.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert "error" not in payload, payload
    result = payload["result"]
    assert not result.get("isError"), result
    return result


def _body(result: dict[str, Any]) -> Any:
    structured = result.get("structuredContent")
    if structured is not None:
        return structured.get("result", structured)
    return json.loads(result["content"][0]["text"])


@pytest.mark.external_only
@pytest.mark.parametrize(
    "pinned", [False, True], ids=["state-unpinned", "state-pinned"]
)
async def test_issue_2576_catalog_and_real_state_reads(
    ha_container_with_fresh_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pinned: bool,
) -> None:
    """Compare locale and pinning while querying the real catalog and HA lights."""
    for key, value in {
        "ENABLE_TOOL_SEARCH": "true",
        "PINNED_TOOLS": "ha_get_state" if pinned else "",
        "HA_MCP_CONFIG_DIR": str(tmp_path),
        "ENABLE_CODE_MODE": "false",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(ha_mcp.config, "_settings", None)
    get_data_dir.cache_clear()
    ha = HomeAssistantClient(
        base_url=ha_container_with_fresh_config["base_url"],
        token=ha_container_with_fresh_config.get("token", TEST_TOKEN),
    )
    evidence: dict[str, Any] = {"pinned": pinned, "locales": {}}
    artifact_dir = Path(os.environ.get("ISSUE_2576_ARTIFACT_DIR", str(tmp_path)))
    await asyncio.to_thread(artifact_dir.mkdir, parents=True, exist_ok=True)
    try:
        server = HomeAssistantSmartMCPServer(client=ha)
        app = server.mcp.http_app(path="/mcp", stateless_http=True, json_response=True)
        async with app.router.lifespan_context(app):
            for locale in ("en", "fr"):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://testserver",
                    headers={
                        "Accept": "application/json, text/event-stream",
                        "Accept-Language": locale,
                        "Cookie": f"ha_mcp_locale={locale}",
                    },
                ) as http:
                    initialized = await _rpc(
                        http,
                        "initialize",
                        {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {},
                            "clientInfo": {"name": "issue-2576-probe", "version": "1"},
                        },
                    )
                    listed = (await _rpc(http, "tools/list", {}))["tools"]
                    catalog = {tool["name"]: tool for tool in listed}
                    assert "ha_search" in catalog
                    assert "ha_report_issue" in catalog
                    assert ("ha_get_state" in catalog) == pinned
                    observations: dict[str, Any] = {
                        "initialize": initialized,
                        "catalog": catalog,
                        "searches": {},
                    }
                    evidence["locales"][locale] = observations
                    for query in QUERIES:
                        raw = await _rpc(
                            http,
                            "tools/call",
                            {
                                "name": "ha_search_tools",
                                "arguments": {"query": query},
                            },
                        )
                        results = _body(raw)
                        assert isinstance(results, list), raw
                        observations["searches"][query] = raw
                        print(
                            json.dumps(
                                {
                                    "pinned": pinned,
                                    "locale": locale,
                                    "query": query,
                                    "names": [tool["name"] for tool in results],
                                },
                                ensure_ascii=False,
                            )
                        )
                    exact = _body(observations["searches"]["ha_get_state"])
                    if pinned:
                        assert all(tool["name"] != "ha_get_state" for tool in exact)
                        state_tool = catalog["ha_get_state"]
                    else:
                        state_tool = next(
                            tool for tool in exact if tool["name"] == "ha_get_state"
                        )
                    assert "entity_id" in state_tool["inputSchema"]["properties"]
                    assert "entity_id" in state_tool["inputSchema"]["required"]
                    lights_raw = await _rpc(
                        http,
                        "tools/call",
                        {
                            "name": "ha_search",
                            "arguments": {
                                "query": "",
                                "domain_filter": "light",
                                "limit": 50,
                            },
                        },
                    )
                    lights = _body(lights_raw)
                    assert lights["success"] is True, lights
                    entity_id = lights["entities"][0]["entity_id"]
                    actual = await ha.get_entity_state(entity_id)
                    state_raw = await _rpc(
                        http,
                        "tools/call",
                        {
                            "name": "ha_get_state",
                            "arguments": {"entity_id": entity_id},
                        },
                    )
                    state = _body(state_raw)
                    assert state.get("success") is not False, state
                    record = state.get("data", state)
                    assert record["state"] == actual["state"], state
                    observations["lights"] = lights_raw
                    observations["state"] = state_raw
            english, french = evidence["locales"]["en"], evidence["locales"]["fr"]
            assert english["catalog"] == french["catalog"]
            assert english["searches"] == french["searches"]
            assert (
                english["initialize"]["instructions"]
                == french["initialize"]["instructions"]
            )
    finally:
        (artifact_dir / f"pinned-{pinned}.json").write_text(
            json.dumps(evidence, indent=2, ensure_ascii=False) + "\n"
        )
        await ha.close()
        get_data_dir.cache_clear()
