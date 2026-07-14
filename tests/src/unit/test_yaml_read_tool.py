"""Unit tests for the ha_config_get_yaml MCP tool wrapper (#1788)."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError


@pytest.fixture(autouse=True)
def _reset_caller_token_cache():
    """The filesystem wrapper caches the bootstrap token per-client; each test
    builds a fresh client, so drop stale entries from a recycled id()."""
    from ha_mcp.tools.tools_filesystem import _reset_caller_token_cache

    _reset_caller_token_cache()
    yield
    _reset_caller_token_cache()


def _service_mock(responses: dict):
    """call_service mock answering the bootstrap plus per-service responses.

    ``responses`` maps a service name to either a single response dict or a
    callable taking the payload (so read_file can answer per-file).
    """

    async def fake_call_service(domain, service, payload, **kwargs):
        if service == "get_caller_token":
            from ha_mcp.tools.tools_filesystem import MIN_COMPONENT_VERSION

            return {
                "service_response": {
                    "success": True,
                    "token": "test-token",
                    "version": MIN_COMPONENT_VERSION,
                }
            }
        handler = responses[service]
        result = handler(payload) if callable(handler) else handler
        return {"service_response": result}

    return AsyncMock(side_effect=fake_call_service)


async def _make_tool(responses: dict):
    """Build a minimal mcp + client harness around register_yaml_read_tools."""
    from ha_mcp.tools.tools_yaml_read import register_yaml_read_tools

    captured: dict = {}

    class FakeMCP:
        def add_tool(self, method):
            captured.setdefault("fns", []).append(method)

    client = MagicMock()
    client.get_services = AsyncMock(
        return_value=[
            {
                "domain": "ha_mcp_tools",
                "services": {
                    "get_caller_token": {},
                    "read_file": {},
                    "list_files": {},
                },
            }
        ]
    )
    client.call_service = _service_mock(responses)

    mcp = FakeMCP()
    register_yaml_read_tools(mcp, client)
    return captured["fns"][0], client


def _read_ok(subtree, parsed=None):
    body = {"success": True, "path": "x", "content": "...", "subtree": subtree}
    if parsed is not None:
        body["parsed"] = parsed
    return body


async def test_registers_without_yaml_editing_flag(monkeypatch):
    """The read tool is ungated: it must register even with YAML editing off.

    This is the whole reason it is a separate module from tools_yaml_config —
    that module's register function returns early when the flag is off.
    """
    from ha_mcp import config as ha_mcp_config

    monkeypatch.setenv("ENABLE_YAML_CONFIG_EDITING", "false")
    monkeypatch.setenv("ENABLE_BETA_FEATURES", "false")
    monkeypatch.setattr(ha_mcp_config, "_settings", None)
    try:
        fn, _ = await _make_tool({"read_file": _read_ok("rest:\n")})
        assert fn is not None
    finally:
        ha_mcp_config._settings = None


async def test_single_file_returns_match():
    fn, client = await _make_tool({"read_file": _read_ok("method: GET\n")})

    out = await fn(yaml_path="rest", file="configuration.yaml")

    assert out["success"] is True
    assert out["count"] == 1
    assert out["files_searched"] == 1
    assert out["matches"] == [
        {
            "file": "configuration.yaml",
            "yaml_path": "rest",
            "content": "method: GET\n",
        }
    ]


async def test_absent_key_is_a_non_match_not_an_error():
    """subtree=None means the file parsed but has no such key."""
    fn, _ = await _make_tool({"read_file": _read_ok(None)})

    out = await fn(yaml_path="nope", file="configuration.yaml")

    assert out["success"] is True
    assert out["matches"] == []
    assert out["count"] == 0
    assert out["files_searched"] == 1


async def test_glob_expands_and_filters_to_defining_files():
    """The discovery case: only files that actually define the key match."""

    def read(payload):
        if payload["path"] == "packages/alert2.yaml":
            return _read_ok("- name: my_alert\n")
        return _read_ok(None)

    fn, client = await _make_tool(
        {
            "list_files": {
                "success": True,
                "files": [
                    {"path": "packages/lights.yaml", "is_dir": False},
                    {"path": "packages/alert2.yaml", "is_dir": False},
                ],
            },
            "read_file": read,
        }
    )

    out = await fn(yaml_path="alert2", file="packages/*.yaml")

    assert out["count"] == 1
    assert out["files_searched"] == 2
    assert out["matches"][0]["file"] == "packages/alert2.yaml"
    # list_files is asked for the directory, with the file-name pattern.
    list_call = next(
        c for c in client.call_service.await_args_list if c.args[1] == "list_files"
    )
    assert list_call.args[2]["path"] == "packages"
    assert list_call.args[2]["pattern"] == "*.yaml"


async def test_glob_skips_directories_and_sorts():
    def read(payload):
        return _read_ok(f"from: {payload['path']}\n")

    fn, _ = await _make_tool(
        {
            "list_files": {
                "success": True,
                "files": [
                    {"path": "packages/b.yaml", "is_dir": False},
                    {"path": "packages/nested", "is_dir": True},
                    {"path": "packages/a.yaml", "is_dir": False},
                ],
            },
            "read_file": read,
        }
    )

    out = await fn(yaml_path="k", file="packages/*.yaml")

    assert [m["file"] for m in out["matches"]] == [
        "packages/a.yaml",
        "packages/b.yaml",
    ]


async def test_include_content_false_discovers_without_bodies():
    fn, _ = await _make_tool({"read_file": _read_ok("- name: my_alert\n")})

    out = await fn(
        yaml_path="alert2", file="packages/alert2.yaml", include_content=False
    )

    assert out["count"] == 1
    assert "content" not in out["matches"][0]
    assert out["matches"][0]["file"] == "packages/alert2.yaml"


async def test_include_parsed_requests_and_returns_parsed():
    fn, client = await _make_tool(
        {"read_file": _read_ok("api_key: !secret k\n", {"api_key": "!secret k"})}
    )

    out = await fn(yaml_path="rest", file="configuration.yaml", include_parsed=True)

    assert out["matches"][0]["parsed"] == {"api_key": "!secret k"}
    read_call = next(
        c for c in client.call_service.await_args_list if c.args[1] == "read_file"
    )
    assert read_call.args[2]["include_parsed"] is True


async def test_include_parsed_not_sent_by_default():
    """Default calls must not send include_parsed at all — the component's
    schema is strict, and the flag costs a parse the caller didn't ask for."""
    fn, client = await _make_tool({"read_file": _read_ok("method: GET\n")})

    await fn(yaml_path="rest", file="configuration.yaml")

    read_call = next(
        c for c in client.call_service.await_args_list if c.args[1] == "read_file"
    )
    assert "include_parsed" not in read_call.args[2]


async def test_read_failure_raises_tool_error():
    fn, _ = await _make_tool(
        {"read_file": {"success": False, "error": "File does not exist: nope.yaml"}}
    )

    with pytest.raises(ToolError):
        await fn(yaml_path="rest", file="nope.yaml")


async def test_list_failure_raises_tool_error():
    fn, _ = await _make_tool(
        {
            "list_files": {"success": False, "error": "Path not allowed.", "files": []},
            "read_file": _read_ok("x\n"),
        }
    )

    with pytest.raises(ToolError):
        await fn(yaml_path="alert2", file="packages/*.yaml")
