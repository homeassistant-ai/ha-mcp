"""Each endpoint URL serves one fixed tool catalog."""

import json
from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient

from ha_mcp import tool_catalog
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
    ToolCatalogInstructionsMiddleware,
    catalog_endpoint,
    tool_search_active,
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
    """A client capability asking for a catalog, which the server ignores."""
    return {"extensions": {"io.github.homeassistant-ai/tool-catalog": {"mode": mode}}}


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


ENDPOINT_MODES = (
    ("/mcp", "full"),
    ("/mcp/catalog/search", "search"),
    ("/mcp/readonly/catalog/search", "search"),
)


@pytest.mark.parametrize("stateless", [False, True])
def test_initialize_describes_the_endpoints_catalog(stateless):
    """Client capabilities never change the catalog: clients do not refresh
    their tool list, so only the URL may choose it."""
    app = _server().http_app(path="/mcp", stateless_http=stateless)
    with TestClient(app) as client:
        for path, mode in ENDPOINT_MODES:
            other = "full" if mode == "search" else "search"
            result, session = _initialize(client, path, _extension(other))
            assert result["instructions"] == f"{mode} text"
            names = _tool_names(client, path, headers=session)
            assert ("ha_search_tools" in names) == (mode == "search")


def test_discover_describes_the_endpoints_catalog():
    app = _server().http_app(path="/mcp", stateless_http=True)
    with TestClient(app) as client:
        for path, mode in ENDPOINT_MODES:
            other = "full" if mode == "search" else "search"
            _response, reply = _post(
                client,
                path,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "server/discover",
                    "params": _modern_params(_extension(other)),
                },
                _modern_headers("server/discover"),
            )
            assert reply["result"]["instructions"] == f"{mode} text"
            names = _tool_names(
                client,
                path,
                _modern_params(_extension(other)),
                _modern_headers("tools/list"),
            )
            assert ("ha_search_tools" in names) == (mode == "search")


@pytest.mark.parametrize("failure", ["import", "install"])
def test_missing_search_transform_serves_the_full_catalog_everywhere(
    monkeypatch, failure
):
    """Without the transform, a search URL must not promise search tools."""
    from ha_mcp import transforms
    from ha_mcp.server import HomeAssistantSmartMCPServer

    monkeypatch.setattr(tool_catalog, "_search_available", True)
    if failure == "import":
        monkeypatch.delattr(transforms, "CategorizedSearchTransform")
    else:
        monkeypatch.setattr(
            transforms, "CategorizedSearchTransform", MagicMock(side_effect=ValueError)
        )
    get_global_settings().enable_tool_search = True
    srv = HomeAssistantSmartMCPServer.__new__(HomeAssistantSmartMCPServer)
    srv.settings = get_global_settings()
    srv.mcp = mcp = HttpTransportFastMCP("no search", instructions="search text")
    srv._instructions = {"full": "full text", "search": "search text"}
    srv._user_enabled_tools, srv._user_pinned_tools = set(), []
    mcp.tool(annotations={"readOnlyHint": True})(read_tool)
    srv._apply_tool_search()
    with catalog_endpoint("search"):
        assert not tool_search_active()
    app = mcp.http_app(path="/mcp", stateless_http=True)
    with TestClient(app) as client:
        for path in ("/mcp", "/mcp/catalog/search"):
            assert _tool_names(client, path) == {"read_tool"}
            assert _initialize(client, path, {})[0]["instructions"] == "full text"


def test_a_later_server_with_the_search_transform_serves_search(monkeypatch):
    """The component rebuilds its server in place; one failed import must not
    leave every later server serving the full catalog."""
    from ha_mcp.server import HomeAssistantSmartMCPServer

    monkeypatch.setattr(tool_catalog, "_search_available", False)
    srv = HomeAssistantSmartMCPServer.__new__(HomeAssistantSmartMCPServer)
    srv.settings = get_global_settings()
    srv.mcp = HttpTransportFastMCP("search")
    srv._instructions = {"full": "full text", "search": "search text"}
    srv._user_enabled_tools, srv._user_pinned_tools = set(), []
    srv.mcp.tool(annotations={"readOnlyHint": True})(read_tool)
    srv._apply_tool_search()
    with catalog_endpoint("search"):
        assert tool_search_active()
    app = srv.mcp.http_app(path="/mcp", stateless_http=True)
    with TestClient(app) as client:
        assert "ha_search_tools" in _tool_names(client, "/mcp/catalog/search")
        assert _tool_names(client, "/mcp/catalog/full") == {"read_tool"}


def read_tool() -> str:
    return "read succeeded"


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


@pytest.mark.parametrize("mode", ["full", "search"])
def test_bug_reports_show_the_connection_catalog(mode):
    """A report filed from a suffixed URL names the catalog the agent actually
    had, which ENABLE_TOOL_SEARCH alone does not tell."""
    from ha_mcp.tools.bug_report_config import collect_config_toggles

    with catalog_endpoint(mode):
        toggles = collect_config_toggles(get_global_settings())
    assert toggles["enable_tool_search"] is False
    assert toggles["connection_catalog"] == mode
