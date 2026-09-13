"""One HTTP server serves normal and read-only clients against real HA."""

import json

import httpx
import pytest

from ha_mcp.config import get_global_settings
from ha_mcp.utils.data_paths import get_data_dir

from .test_readonly_mode import _build_readonly_server


@pytest.mark.external_only
@pytest.mark.parametrize("tool_search", [False, True])
async def test_readonly_endpoint_with_real_ha(
    ha_container_with_fresh_config, monkeypatch, tmp_path, tool_search
):
    server, ha_client = await _build_readonly_server(
        ha_container_with_fresh_config,
        monkeypatch,
        tmp_path,
        extra_env={"READ_ONLY_MODE": "false", "ENABLE_TOOL_SEARCH": str(tool_search)},
    )
    app = server.mcp.http_app(path="/mcp", stateless_http=True, json_response=True)
    entity = "light.bed_light"
    original_state = None
    try:
        original = await ha_client.get_entity_state(entity)
        assert original["state"] in ("on", "off"), original
        original_state = original["state"]
        target = "off" if original_state == "on" else "on"
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as http,
        ):

            async def call(path, name, arguments):
                response = await http.post(
                    path,
                    headers={"Accept": "application/json, text/event-stream"},
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": name, "arguments": arguments},
                    },
                )
                assert response.status_code == 200, response.text
                result = response.json()["result"]
                return result, json.loads(result["content"][0]["text"])

            # The normal endpoint performs a real write and waits for HA state.
            command = {
                "domain": "light",
                "service": f"turn_{target}",
                "entity_id": entity,
            }
            write_tool = "ha_call_write_tool" if tool_search else "ha_call_service"
            args = (
                {"name": "ha_call_service", "arguments": command}
                if tool_search
                else command
            )
            result, body = await call("/mcp", write_tool, args)
            assert not result.get("isError"), body
            assert (await ha_client.get_entity_state(entity))["state"] == target

            command["service"] = f"turn_{original['state']}"
            result, body = await call("/mcp/readonly", write_tool, args)
            assert result["isError"] is True, body
            assert body["error"]["code"] == "READ_ONLY_MODE", body
            assert (await ha_client.get_entity_state(entity))["state"] == target

            # Existing mixed-tool reads stay available; their writes are blocked.
            result, body = await call(
                "/mcp/readonly",
                "ha_manage_backup",
                {"scope": "edits", "action": "list"},
            )
            assert not result.get("isError"), body
            result, body = await call(
                "/mcp/readonly",
                "ha_manage_backup",
                {"scope": "snapshot", "action": "create"},
            )
            assert result["isError"] is True, body
            assert body["error"]["code"] == "READ_ONLY_MODE", body

            for path, expected in (("/mcp/readonly", True), ("/mcp", False)):
                result, body = await call(
                    path, "ha_get_overview", {"fields": ["read_only_mode"]}
                )
                assert not result.get("isError"), body
                assert body.get("read_only_mode", False) is expected, body
            assert get_global_settings().read_only_mode is False
    finally:
        try:
            if original_state is not None:
                await ha_client.call_service(
                    "light", f"turn_{original_state}", {"entity_id": entity}
                )
        finally:
            await ha_client.close()
            get_data_dir.cache_clear()


@pytest.mark.embedded_only
async def test_embedded_webhook_readonly_endpoint(ha_container_with_fresh_config):
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    from ..utilities.assertions import parse_mcp_result
    from .test_readonly_mode import _expect_read_only_blocked

    url = ha_container_with_fresh_config["embedded_webhook_url"]
    assert url
    async with Client(StreamableHttpTransport(url=url + "/readonly")) as client:
        names = {tool.name for tool in await client.list_tools()}
        assert "ha_call_write_tool" not in names
        assert "ha_call_delete_tool" not in names
        assert "ha_call_service" not in names
        overview = parse_mcp_result(
            await client.call_tool("ha_get_overview", {"fields": ["read_only_mode"]})
        )
        assert overview["read_only_mode"] is True
        await _expect_read_only_blocked(
            client,
            "ha_call_service",
            {
                "domain": "light",
                "service": "turn_on",
                "entity_id": "light.readonly_nonexistent_fixture",
            },
        )
        await _expect_read_only_blocked(
            client, "ha_manage_backup", {"scope": "snapshot", "action": "create"}
        )
    async with Client(StreamableHttpTransport(url=url)) as client:
        overview = parse_mcp_result(
            await client.call_tool("ha_get_overview", {"fields": ["read_only_mode"]})
        )
        assert overview.get("read_only_mode", False) is False
