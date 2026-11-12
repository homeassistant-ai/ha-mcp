"""Annotation reads against real FastMCP tools, not stand-ins with guessed names."""

from types import SimpleNamespace

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ha_mcp._vendor.fastmcp import FastMCP
from ha_mcp._vendor.fastmcp.tools import Tool
from ha_mcp._vendor.mcp.types import ToolAnnotations
from ha_mcp.policy.approval_queue import ApprovalQueue
from ha_mcp.read_only import is_read_safe
from ha_mcp.settings_ui import build_settings_handlers


def _noop() -> None:
    return None


def _tool(name: str, **annotations: bool) -> Tool:
    return Tool.from_function(
        _noop, name=name, annotations=ToolAnnotations(**annotations)
    )


def _settings_ui(*tools: Tool) -> TestClient:
    """The settings UI routes over a real FastMCP registry holding ``tools``."""
    mcp = FastMCP("annotations")
    for tool in tools:
        mcp.add_tool(tool)
    server = SimpleNamespace(mcp=mcp, approval_queue=ApprovalQueue())
    handlers = build_settings_handlers(server)  # type: ignore[arg-type]
    return TestClient(
        Starlette(
            routes=[
                Route("/api/settings/tools", handlers["get_tools"]),
                Route("/api/policy/tool-schema", handlers["policy_get_tool_schema"]),
            ]
        )
    )


def _offers_value_pickers(client: TestClient, name: str) -> bool:
    reply = client.get("/api/policy/tool-schema", params={"name": name})
    assert reply.status_code == 200, reply.text
    return reply.json()["is_write_or_destructive"]


def test_read_only_tool():
    tool = _tool("ha_read", read_only_hint=True)
    assert is_read_safe(tool) is True
    assert _offers_value_pickers(_settings_ui(tool), "ha_read") is False


def test_destructive_tool():
    tool = _tool("ha_delete", destructive_hint=True)
    assert is_read_safe(tool) is False
    assert _offers_value_pickers(_settings_ui(tool), "ha_delete") is True


def test_settings_ui_capability_badges_follow_the_annotations():
    client = _settings_ui(
        _tool("ha_read", read_only_hint=True),
        _tool("ha_delete_x", destructive_hint=True),
    )

    reply = client.get("/api/settings/tools")

    assert reply.status_code == 200, reply.text
    rows = {row["name"]: row for row in reply.json()["tools"]}
    assert rows["ha_read"]["category"] == "read"
    assert rows["ha_delete_x"]["category"] == "delete"
