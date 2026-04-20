"""Unit tests for the Node-RED tools module (`tools_nodered.py`).

These tests exercise the per-tool logic against a mocked NodeRedClient. The
client itself is a thin httpx wrapper — its exception-classification paths
are exercised end-to-end through ``exception_to_structured_error`` here.

Live integration tests against a real Node-RED instance would belong under
``tests/src/integration/`` and be gated behind ``NODERED_LIVE_TESTS=true``;
they are deliberately not added in this commit.
"""

import json
from unittest.mock import AsyncMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.client.nodered_client import (
    NodeRedAPIError,
    NodeRedAuthError,
    NodeRedConnectionError,
)
from ha_mcp.tools.tools_nodered import NodeRedTools

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _flows_fixture() -> list[dict]:
    """A representative /flows payload: two tabs, a few nodes, one config node."""
    return [
        {"id": "tab1", "type": "tab", "label": "Lights", "disabled": False},
        {"id": "tab2", "type": "tab", "label": "Bathroom", "disabled": False},
        {
            "id": "n1",
            "type": "inject",
            "z": "tab1",
            "name": "Morning Trigger",
            "x": 100,
            "y": 100,
            "wires": [["n2"]],
        },
        {
            "id": "n2",
            "type": "function",
            "z": "tab1",
            "name": "Compute Brightness",
            "x": 250,
            "y": 100,
            "wires": [[]],
            "func": "return msg;",
        },
        {
            "id": "n3",
            "type": "api-call-service",
            "z": "tab2",
            "name": "Turn on Fan",
            "x": 100,
            "y": 200,
            "wires": [[]],
        },
        {"id": "cfg1", "type": "server", "name": "HA Server"},  # config node, no z
    ]


@pytest.fixture
def mock_client():
    client = AsyncMock()
    client.get_flows = AsyncMock(return_value=_flows_fixture())
    client.post_flows = AsyncMock(return_value="rev-abc-123")
    client.get_settings = AsyncMock(
        return_value={
            "version": "4.0.2",
            "httpNodeRoot": "/",
            "paletteCategories": ["common", "function"],
            "flowEncryptionType": "user",
            "editorTheme": {"projects": {"enabled": False}},
        }
    )
    client.inject = AsyncMock(return_value="OK")
    return client


@pytest.fixture
def tools(mock_client):
    return NodeRedTools(mock_client)


def _bare(method):
    """Strip @log_tool_usage / @tool wrappers to call the underlying coroutine."""
    fn = method
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _parse_tool_error(exc: ToolError) -> dict:
    """Tool errors are JSON-serialised structured responses."""
    return json.loads(str(exc))


# ---------------------------------------------------------------------------
# Read-only tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_flows_summarises_tabs_and_nodes(tools):
    result = await _bare(tools.ha_list_nodered_flows)(tools)
    assert result["success"] is True
    data = result["data"]
    assert data["total_tabs"] == 2
    assert data["total_nodes"] == 6
    assert data["nodes_per_tab"] == {"tab1": 2, "tab2": 1}
    assert data["config_nodes_count"] == 1
    assert {t["id"] for t in data["tabs"]} == {"tab1", "tab2"}


@pytest.mark.asyncio
async def test_get_flow_returns_tab_with_nodes(tools):
    result = await _bare(tools.ha_get_nodered_flow)(tools, flow_id="tab1")
    assert result["success"] is True
    data = result["data"]
    assert data["id"] == "tab1"
    assert data["label"] == "Lights"
    assert data["node_count"] == 2
    assert {n["id"] for n in data["nodes"]} == {"n1", "n2"}


@pytest.mark.asyncio
async def test_get_flow_unknown_id_raises_resource_not_found(tools):
    with pytest.raises(ToolError) as exc:
        await _bare(tools.ha_get_nodered_flow)(tools, flow_id="missing")
    err = _parse_tool_error(exc.value)
    assert err["success"] is False
    assert err["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert err["identifier"] == "missing"


@pytest.mark.asyncio
async def test_get_nodes_filters_by_type_and_name_substring(tools):
    result = await _bare(tools.ha_search_nodered_nodes)(
        tools, node_type="function", search_name="brightness"
    )
    assert result["success"] is True
    data = result["data"]
    assert data["matches"] == 1
    assert data["nodes"][0]["id"] == "n2"
    assert data["truncated"] is False


@pytest.mark.asyncio
async def test_get_nodes_restricts_to_flow_id(tools):
    result = await _bare(tools.ha_search_nodered_nodes)(tools, flow_id="tab2")
    assert result["success"] is True
    assert {n["id"] for n in result["data"]["nodes"]} == {"n3"}


@pytest.mark.asyncio
async def test_get_nodes_search_is_case_insensitive(tools):
    result = await _bare(tools.ha_search_nodered_nodes)(tools, search_name="MORNING")
    ids = {n["id"] for n in result["data"]["nodes"]}
    assert "n1" in ids


@pytest.mark.asyncio
async def test_get_settings_passes_through_runtime_info(tools):
    result = await _bare(tools.ha_get_nodered_settings)(tools)
    assert result["data"]["version"] == "4.0.2"
    assert "common" in result["data"]["palette_categories"]


# ---------------------------------------------------------------------------
# Inject
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inject_node_calls_client_and_reports_success(tools, mock_client):
    result = await _bare(tools.ha_call_nodered_inject_node)(tools, node_id="n1")
    mock_client.inject.assert_awaited_once_with("n1")
    assert result["data"]["node_id"] == "n1"


# ---------------------------------------------------------------------------
# Patch node
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_node_updates_fields_and_redeploys(tools, mock_client):
    result = await _bare(tools.ha_update_nodered_node)(
        tools, node_id="n2", patches={"name": "Renamed", "func": "return null;"}
    )
    assert result["success"] is True
    deployed = mock_client.post_flows.await_args.args[0]
    n2 = next(n for n in deployed if n["id"] == "n2")
    assert n2["name"] == "Renamed"
    assert n2["func"] == "return null;"
    assert set(result["data"]["node"]["patched_fields"]) == {"name", "func"}


@pytest.mark.asyncio
async def test_patch_node_missing_raises_resource_not_found(tools, mock_client):
    with pytest.raises(ToolError) as exc:
        await _bare(tools.ha_update_nodered_node)(
            tools, node_id="ghost", patches={"name": "x"}
        )
    err = _parse_tool_error(exc.value)
    assert err["error"]["code"] == "RESOURCE_NOT_FOUND"
    mock_client.post_flows.assert_not_called()


@pytest.mark.asyncio
async def test_patch_node_rejects_type_change(tools, mock_client):
    with pytest.raises(ToolError) as exc:
        await _bare(tools.ha_update_nodered_node)(
            tools, node_id="n1", patches={"type": "function"}
        )
    err = _parse_tool_error(exc.value)
    assert err["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    mock_client.post_flows.assert_not_called()


# ---------------------------------------------------------------------------
# Patch flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_flow_applies_multiple_patches(tools, mock_client):
    result = await _bare(tools.ha_update_nodered_flow_nodes)(
        tools,
        flow_id="tab1",
        node_patches=[
            {"node_id": "n1", "patches": {"name": "Trigger v2"}},
            {"node_id": "n2", "patches": {"func": "return 42;"}},
        ],
    )
    assert result["data"]["message"].startswith("Patched 2")
    deployed = mock_client.post_flows.await_args.args[0]
    by_id = {n["id"]: n for n in deployed}
    assert by_id["n1"]["name"] == "Trigger v2"
    assert by_id["n2"]["func"] == "return 42;"


@pytest.mark.asyncio
async def test_patch_flow_collects_item_errors_for_wrong_flow(tools, mock_client):
    result = await _bare(tools.ha_update_nodered_flow_nodes)(
        tools,
        flow_id="tab1",
        node_patches=[
            {"node_id": "n1", "patches": {"name": "ok"}},
            {"node_id": "n3", "patches": {"name": "wrong tab"}},  # n3 is on tab2
        ],
    )
    assert result["success"] is True
    assert len(result["data"]["patched_nodes"]) == 1
    assert result["data"]["errors"] is not None
    assert (
        result["data"]["errors"][0]["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    )


@pytest.mark.asyncio
async def test_patch_flow_unknown_flow_raises(tools, mock_client):
    with pytest.raises(ToolError) as exc:
        await _bare(tools.ha_update_nodered_flow_nodes)(
            tools, flow_id="missing", node_patches=[{"node_id": "n1", "patches": {}}]
        )
    err = _parse_tool_error(exc.value)
    assert err["error"]["code"] == "RESOURCE_NOT_FOUND"
    mock_client.post_flows.assert_not_called()


@pytest.mark.asyncio
async def test_patch_flow_all_failures_raises(tools, mock_client):
    with pytest.raises(ToolError) as exc:
        await _bare(tools.ha_update_nodered_flow_nodes)(
            tools,
            flow_id="tab1",
            node_patches=[{"node_id": "ghost", "patches": {"name": "x"}}],
        )
    err = _parse_tool_error(exc.value)
    assert err["error"]["code"] == "VALIDATION_FAILED"
    mock_client.post_flows.assert_not_called()


# ---------------------------------------------------------------------------
# Replace flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replace_flow_swaps_nodes_and_forces_z(tools, mock_client):
    new_nodes = [
        {"id": "new1", "type": "inject", "name": "New Inject", "wires": []},
        {"id": "new2", "type": "debug", "name": "Dbg", "wires": []},
    ]
    result = await _bare(tools.ha_replace_nodered_flow_nodes)(
        tools, flow_id="tab1", new_flow_nodes=new_nodes
    )
    assert result["data"]["old_node_count"] == 2
    assert result["data"]["new_node_count"] == 2

    deployed = mock_client.post_flows.await_args.args[0]
    deployed_ids = {n["id"] for n in deployed}
    # Old tab1 nodes gone, new ones present, tab2 + config preserved
    assert {"n1", "n2"}.isdisjoint(deployed_ids)
    assert {"new1", "new2", "tab1", "tab2", "n3", "cfg1"} <= deployed_ids
    # New nodes had z forced to flow_id
    for node in deployed:
        if node["id"] in {"new1", "new2"}:
            assert node["z"] == "tab1"


@pytest.mark.asyncio
async def test_replace_flow_unknown_flow_raises(tools, mock_client):
    with pytest.raises(ToolError) as exc:
        await _bare(tools.ha_replace_nodered_flow_nodes)(
            tools, flow_id="missing", new_flow_nodes=[]
        )
    err = _parse_tool_error(exc.value)
    assert err["error"]["code"] == "RESOURCE_NOT_FOUND"
    mock_client.post_flows.assert_not_called()


# ---------------------------------------------------------------------------
# Add flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_flow_appends_tab_and_nodes(tools, mock_client):
    new_tab = {"id": "tab3", "type": "tab", "label": "Garage"}
    new_nodes = [{"id": "g1", "type": "inject", "name": "Garage Trigger", "wires": []}]
    result = await _bare(tools.ha_create_nodered_flow)(
        tools, flow_tab=new_tab, flow_nodes=new_nodes
    )
    assert result["data"]["flow_id"] == "tab3"
    deployed = mock_client.post_flows.await_args.args[0]
    deployed_by_id = {n["id"]: n for n in deployed}
    assert deployed_by_id["tab3"]["type"] == "tab"
    assert deployed_by_id["g1"]["z"] == "tab3"


@pytest.mark.asyncio
async def test_add_flow_rejects_duplicate_id(tools, mock_client):
    with pytest.raises(ToolError) as exc:
        await _bare(tools.ha_create_nodered_flow)(
            tools,
            flow_tab={"id": "tab1", "type": "tab", "label": "dup"},
            flow_nodes=[],
        )
    err = _parse_tool_error(exc.value)
    assert err["error"]["code"] == "RESOURCE_ALREADY_EXISTS"
    mock_client.post_flows.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_tab,expected_code",
    [
        ({"id": "x", "label": "y"}, "VALIDATION_INVALID_PARAMETER"),  # type missing
        ({"type": "tab", "label": "y"}, "VALIDATION_MISSING_PARAMETER"),  # id
        ({"id": "x", "type": "tab"}, "VALIDATION_MISSING_PARAMETER"),  # label
    ],
)
async def test_add_flow_validates_tab_shape(tools, mock_client, bad_tab, expected_code):
    with pytest.raises(ToolError) as exc:
        await _bare(tools.ha_create_nodered_flow)(
            tools, flow_tab=bad_tab, flow_nodes=[]
        )
    assert _parse_tool_error(exc.value)["error"]["code"] == expected_code
    mock_client.post_flows.assert_not_called()


# ---------------------------------------------------------------------------
# Delete flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_flow_removes_tab_and_its_nodes(tools, mock_client):
    result = await _bare(tools.ha_delete_nodered_flow)(tools, flow_id="tab1")
    assert result["data"]["deleted_node_count"] == 3  # tab1 + n1 + n2
    deployed_ids = {n["id"] for n in mock_client.post_flows.await_args.args[0]}
    assert "tab1" not in deployed_ids
    assert "n1" not in deployed_ids
    assert "n2" not in deployed_ids
    assert {"tab2", "n3", "cfg1"} <= deployed_ids


@pytest.mark.asyncio
async def test_delete_flow_unknown_raises(tools, mock_client):
    with pytest.raises(ToolError) as exc:
        await _bare(tools.ha_delete_nodered_flow)(tools, flow_id="missing")
    err = _parse_tool_error(exc.value)
    assert err["error"]["code"] == "RESOURCE_NOT_FOUND"
    mock_client.post_flows.assert_not_called()


# ---------------------------------------------------------------------------
# Update flows (raw deploy)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_flows_passes_through(tools, mock_client):
    payload = [{"id": "tabX", "type": "tab", "label": "Replacement"}]
    result = await _bare(tools.ha_replace_nodered_flows)(tools, flows=payload)
    mock_client.post_flows.assert_awaited_once_with(payload)
    assert result["data"]["node_count"] == 1


# ---------------------------------------------------------------------------
# Client error mapping (verifies tool wraps client exceptions structurally)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connection_error_maps_to_connection_failed(tools, mock_client):
    mock_client.get_flows.side_effect = NodeRedConnectionError(
        "connection refused on socket"
    )
    with pytest.raises(ToolError) as exc:
        await _bare(tools.ha_list_nodered_flows)(tools)
    code = _parse_tool_error(exc.value)["error"]["code"]
    assert code in ("CONNECTION_FAILED", "CONNECTION_TIMEOUT")


@pytest.mark.asyncio
async def test_timeout_message_maps_to_timeout_operation(tools, mock_client):
    """The client formats timeouts with the word 'timeout' so the upstream
    message-based classifier returns TIMEOUT_OPERATION — guard against the
    classifier being narrowed in a future upstream change."""
    mock_client.get_flows.side_effect = NodeRedConnectionError(
        "Node-RED request timeout after 30s"
    )
    with pytest.raises(ToolError) as exc:
        await _bare(tools.ha_list_nodered_flows)(tools)
    assert _parse_tool_error(exc.value)["error"]["code"] == "TIMEOUT_OPERATION"


@pytest.mark.asyncio
async def test_auth_error_maps_to_auth_invalid_token(tools, mock_client):
    mock_client.get_flows.side_effect = NodeRedAuthError("auth failed 401")
    with pytest.raises(ToolError) as exc:
        await _bare(tools.ha_list_nodered_flows)(tools)
    code = _parse_tool_error(exc.value)["error"]["code"]
    assert code == "AUTH_INVALID_TOKEN"


@pytest.mark.asyncio
async def test_api_error_classified_by_helpers(tools, mock_client):
    mock_client.get_flows.side_effect = NodeRedAPIError(
        "Node-RED 500", status_code=500, response_text="boom"
    )
    with pytest.raises(ToolError) as exc:
        await _bare(tools.ha_list_nodered_flows)(tools)
    err = _parse_tool_error(exc.value)
    # Falls through to internal-error / service-call-failed via message classifier
    assert err["error"]["code"] in (
        "INTERNAL_ERROR",
        "SERVICE_CALL_FAILED",
    )
