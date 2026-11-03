"""One server gives each connection the tool catalog it negotiated."""

import json

import pytest
from starlette.testclient import TestClient

from ha_mcp._vendor import fastmcp
from ha_mcp._vendor.fastmcp.tools import Tool
from ha_mcp._vendor.mcp_types import (
    CLIENT_CAPABILITIES_META_KEY,
    PROTOCOL_VERSION_META_KEY,
)
from ha_mcp.config import get_global_settings, reset_global_settings
from ha_mcp.http_transport import HttpTransportFastMCP
from ha_mcp.read_only import ReadOnlyToolsTransform
from ha_mcp.tool_catalog import (
    TOOL_CATALOG_EXTENSION_ID,
    ToolCatalogExtension,
    ToolCatalogInstructionsMiddleware,
    catalog_endpoint,
)
from ha_mcp.transforms.categorized_search import (
    CategorizedSearchTransform,
    SearchKeywordsTransform,
)
from ha_mcp.utils.data_paths import get_data_dir

FULL = {"read", "write"}
SEARCH = {
    "ha_search_tools",
    "ha_call_read_tool",
    "ha_call_write_tool",
    "ha_call_delete_tool",
}
ACCEPT = {"Accept": "application/json, text/event-stream"}
MODERN = "2026-07-28"


def _modern_headers(method):
    return {"mcp-protocol-version": MODERN, "mcp-method": method}


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("HOMEASSISTANT_URL", "http://localhost:8123")
    monkeypatch.setenv("HOMEASSISTANT_TOKEN", "test-token")
    monkeypatch.setenv("READ_ONLY_MODE", "false")
    monkeypatch.setenv("ENABLE_TOOL_SEARCH", "false")
    monkeypatch.setattr(fastmcp.settings, "http_host_origin_protection", False)
    get_data_dir.cache_clear()
    reset_global_settings()
    yield
    reset_global_settings()
    get_data_dir.cache_clear()


def _server() -> HttpTransportFastMCP:
    mcp = HttpTransportFastMCP("catalog negotiation")

    @mcp.tool(annotations={"readOnlyHint": True})
    def read() -> str:
        return "read succeeded"

    @mcp.tool(annotations={"readOnlyHint": False})
    def write() -> str:
        return "write succeeded"

    mcp.add_transform(ReadOnlyToolsTransform())
    mcp.add_transform(CategorizedSearchTransform())
    mcp.add_extension(ToolCatalogExtension())
    mcp.add_middleware(ToolCatalogInstructionsMiddleware(lambda mode: f"{mode} text"))
    return mcp


def _post(client, path, body, headers=None):
    response = client.post(path, headers={**ACCEPT, **(headers or {})}, json=body)
    assert response.status_code == 200, response.text
    if response.headers["content-type"].startswith("application/json"):
        return response, response.json()
    return response, next(
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    )


def _tool_names(client, path, params=None, headers=None) -> set[str]:
    body = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": params or {}}
    _response, reply = _post(client, path, body, headers)
    return {tool["name"] for tool in reply["result"]["tools"]}


def _initialize(client, path, capabilities):
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": capabilities,
            "clientInfo": {"name": "test", "version": "1"},
        },
    }
    response, reply = _post(client, path, body)
    if "mcp-session-id" not in response.headers:
        return reply["result"], {}
    session = {"mcp-session-id": response.headers["mcp-session-id"]}
    initialized = client.post(
        path,
        headers={**ACCEPT, **session},
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    assert initialized.status_code == 202, initialized.text
    return reply["result"], session


def _extension(mode):
    return {"extensions": {TOOL_CATALOG_EXTENSION_ID: {"mode": mode}}}


def _modern_params(capabilities):
    """2026-07-28 requests carry the version and capabilities in ``_meta``."""
    return {
        "_meta": {
            PROTOCOL_VERSION_META_KEY: MODERN,
            CLIENT_CAPABILITIES_META_KEY: capabilities,
        }
    }


def test_endpoint_selects_the_catalog_without_changing_other_connections():
    app = _server().http_app(path="/mcp", stateless_http=True)
    with TestClient(app) as client:
        assert _tool_names(client, "/mcp") == FULL
        assert _tool_names(client, "/mcp/catalog/search") == SEARCH
        assert _tool_names(client, "/mcp/catalog/search/") == SEARCH
        assert _tool_names(client, "/mcp") == FULL
        assert _tool_names(client, "/mcp/readonly/catalog/search") == {
            "ha_search_tools",
            "ha_call_read_tool",
        }
        assert _tool_names(client, "/mcp/readonly/catalog/full") == {"read"}
        for path in (
            "/mcp/catalog",
            "/mcp/catalog/bogus",
            "/mcp/catalog/search/readonly",
            "/mcp/catalog/search/extra",
        ):
            assert client.post(path, headers=ACCEPT, json={}).status_code == 404


def test_server_default_applies_and_endpoint_overrides_it():
    get_global_settings().enable_tool_search = True
    app = _server().http_app(path="/mcp", stateless_http=True)
    with TestClient(app) as client:
        assert _tool_names(client, "/mcp") == SEARCH
        assert _tool_names(client, "/mcp/catalog/full") == FULL


def test_initialize_capability_selects_the_session_catalog_and_instructions():
    app = _server().http_app(path="/mcp")
    with TestClient(app) as client:
        result, session = _initialize(client, "/mcp/catalog/search", _extension("full"))
        assert result["instructions"] == "full text"
        assert _tool_names(client, "/mcp/catalog/search", headers=session) == FULL

        result, session = _initialize(client, "/mcp/catalog/search", {})
        assert result["instructions"] == "search text"
        assert _tool_names(client, "/mcp/catalog/search", headers=session) == SEARCH


def test_stateless_http_describes_the_catalog_later_requests_get():
    """Stateless HTTP forgets initialize capabilities, so they must not
    change the instructions either: those would promise the wrong tools."""
    app = _server().http_app(path="/mcp", stateless_http=True)
    with TestClient(app) as client:
        result, _ = _initialize(client, "/mcp", _extension("search"))
        assert result["instructions"] == "full text"
        assert _tool_names(client, "/mcp") == FULL
        result, _ = _initialize(client, "/mcp/catalog/search", _extension("full"))
        assert result["instructions"] == "search text"
        assert _tool_names(client, "/mcp/catalog/search") == SEARCH


def test_unknown_client_mode_falls_back_to_the_connection_default():
    app = _server().http_app(path="/mcp")
    with TestClient(app) as client:
        result, session = _initialize(client, "/mcp", _extension("everything"))
        assert result["instructions"] == "full text"
        assert _tool_names(client, "/mcp", headers=session) == FULL


def test_per_request_meta_selects_the_catalog():
    """Protocol 2026-07-28 repeats client capabilities in each request."""
    app = _server().http_app(path="/mcp", stateless_http=True)
    with TestClient(app) as client:
        for capabilities, expected in (
            (_extension("search"), SEARCH),
            ({}, FULL),
        ):
            names = _tool_names(
                client,
                "/mcp",
                _modern_params(capabilities),
                _modern_headers("tools/list"),
            )
            assert names == expected


def test_discover_advertises_modes_and_the_connection_default():
    app = _server().http_app(path="/mcp", stateless_http=True)
    with TestClient(app) as client:
        for path, default in (("/mcp", "full"), ("/mcp/catalog/search", "search")):
            _response, reply = _post(
                client,
                path,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "server/discover",
                    "params": _modern_params({}),
                },
                _modern_headers("server/discover"),
            )
            result = reply["result"]
            assert result["capabilities"]["extensions"][TOOL_CATALOG_EXTENSION_ID] == {
                "modes": ["full", "search"],
                "default": default,
            }
            assert result["instructions"] == f"{default} text"


async def test_full_catalog_connection_keeps_its_tools_and_descriptions():
    """Search-only rewrites never reach a connection that chose the full list."""
    tools = [
        Tool.from_function(lambda: None, name=name, description="Original.")
        for name in ("read", "write")
    ]
    search = CategorizedSearchTransform()
    keywords = SearchKeywordsTransform(overrides={"read": "Narrowed."})
    with catalog_endpoint("full"):
        assert await search.transform_tools(tools) == tools
        assert {t.description for t in await keywords.list_tools(tools)} == {
            "Original."
        }
        for name in ("ha_search_tools", "ha_call_read_tool"):
            assert await search.get_tool(name, _missing) is None
    with catalog_endpoint("search"):
        listed = {t.name for t in await search.transform_tools(tools)}
        assert listed == SEARCH
        assert (await keywords.list_tools(tools))[0].description == "Narrowed."


async def _missing(name, *, version=None):
    return None
