"""Custom-tool persistence through registration and the actual MCP tool.

Files are operator-owned input. Loading, saving, recovery and size limits are
observed through the same tool calls an agent uses, with Core's real client.
"""

import json
from pathlib import Path

import pytest

from ha_mcp._vendor.fastmcp import FastMCP
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.config import get_global_settings
from ha_mcp.tools.tools_code import register_code_tools


@pytest.fixture
def start(core_client: HomeAssistantClient, monkeypatch, tmp_path):
    settings = get_global_settings()
    monkeypatch.setattr(settings, "enable_beta_features", True)
    monkeypatch.setattr(settings, "enable_code_mode", True)

    def build(path):
        monkeypatch.setattr(settings, "code_mode_saved_tools_path", str(path))
        mcp = FastMCP("saved-tool-persistence")
        register_code_tools(mcp, core_client)
        return mcp

    # Registration with a missing file clears the process-wide cache and any
    # previous failed-load state without reaching into the module's globals.
    build(tmp_path / "initial-empty.json")
    yield build
    build(tmp_path / "final-empty.json")


async def _call(mcp, **arguments):
    result = await mcp.call_tool("ha_manage_custom_tool", arguments)
    assert result.structured_content is not None
    assert result.structured_content["success"] is True
    return result.structured_content["data"]


async def _save(mcp, name="example", code="1 + 1", justification="test persistence"):
    return await _call(mcp, code=code, justification=justification, save_as=name)


def _write(path, entries, **metadata):
    path.write_text(json.dumps({"version": 1, "saved_tools": entries, **metadata}))


@pytest.mark.parametrize("contents", [None, "{not JSON", "[1, 2, 3]"])
async def test_missing_or_malformed_file_starts_with_empty_catalog(
    start, tmp_path, contents
):
    path = tmp_path / "saved.json"
    if contents is not None:
        path.write_text(contents)
    assert (await _call(start(path), list_saved=True))["saved_tools"] == {}


async def test_invalid_names_and_entry_shapes_never_enter_catalog(start, tmp_path):
    path = tmp_path / "saved.json"
    _write(
        path,
        {
            "valid": {"code": "1", "justification": "valid"},
            "../bad": {"code": "1"},
            "1leading_digit": {"code": "1"},
            "with space": {"code": "1"},
            "no_code": {"justification": "x"},
            "code_not_str": {"code": 42},
            "empty_code": {"code": ""},
            "not_a_dict": "raw string",
        },
    )
    tools = (await _call(start(path), list_saved=True))["saved_tools"]
    assert tools == {"valid": {"code": "1", "justification": "valid"}}


async def test_missing_or_non_string_justification_is_normalized(start, tmp_path):
    path = tmp_path / "saved.json"
    _write(path, {"a": {"code": "1"}, "b": {"code": "2", "justification": 123}})
    tools = (await _call(start(path), list_saved=True))["saved_tools"]
    assert tools == {
        "a": {"code": "1", "justification": ""},
        "b": {"code": "2", "justification": ""},
    }


async def test_saved_tools_survive_restart_and_overwrite_atomically(start, tmp_path):
    path = tmp_path / "new" / "nested" / "saved.json"
    mcp = start(path)
    saved = await _save(mcp, "first", "1 + 1")
    assert saved["saved_as"] == "first"
    payload = json.loads(path.read_text())
    assert payload["version"] == 1
    assert payload["saved_at"]
    assert payload["saved_tools"]["first"]["code"] == "1 + 1"
    mcp = start(path)
    assert (await _call(mcp, run_saved="first"))["result"] == 2
    await _save(mcp, "second", "2 + 2", "added later")
    await _save(mcp, "first", "3 + 3", "updated")
    reloaded = start(path)
    tools = (await _call(reloaded, list_saved=True))["saved_tools"]
    assert set(tools) == {"first", "second"}
    assert tools["second"]["justification"] == "added later"
    assert (await _call(reloaded, run_saved="first"))["result"] == 6
    assert not list(path.parent.glob("*.tmp"))


async def test_empty_path_keeps_tools_in_memory_and_warns_without_writing(
    start, tmp_path
):
    before = sorted(tmp_path.rglob("*"))
    mcp = start("")
    assert (await _call(mcp, list_saved=True))["saved_tools"] == {}
    saved = await _save(mcp)
    assert saved["saved_as"] == "example"
    assert "memory only" in saved["save_warning"]
    assert (await _call(mcp, run_saved="example"))["result"] == 2
    assert sorted(tmp_path.rglob("*")) == before


@pytest.mark.parametrize("version", [None, 2147483647])
async def test_unfamiliar_schema_is_not_overwritten(start, tmp_path, version):
    path = tmp_path / "saved.json"
    original: dict[str, object] = {"saved_tools": {"keep_me": {"code": "1"}}}
    if version is not None:
        original["version"] = version
    path.write_text(json.dumps(original))
    before = path.read_bytes()
    mcp = start(path)
    assert (await _call(mcp, list_saved=True))["saved_tools"] == {}
    result = await _save(mcp)
    assert result["saved_as"] is None
    assert "rolled back" in result["save_warning"]
    assert path.read_bytes() == before
    assert (await _call(mcp, list_saved=True))["saved_tools"] == {}


async def test_successful_reload_recovers_after_failed_load(start, tmp_path):
    path = tmp_path / "saved.json"
    _write(path, {}, version=2147483647)
    mcp = start(path)
    assert (await _save(mcp))["saved_as"] is None
    _write(path, {})
    recovered = start(path)
    assert (await _save(recovered))["saved_as"] == "example"
    assert (await _call(start(path), run_saved="example"))["result"] == 2


async def test_unreadable_file_is_preserved_on_attempted_save(
    start, tmp_path, monkeypatch
):
    path = tmp_path / "saved.json"
    _write(path, {"keep_me": {"code": "1"}})
    original = path.read_bytes()
    read_text = Path.read_text

    def read(self, *args, **kwargs):
        if self == path:
            raise PermissionError("saved-tools volume unavailable")
        return read_text(self, *args, **kwargs)

    with monkeypatch.context() as patcher:
        patcher.setattr(Path, "read_text", read)
        mcp = start(path)
    assert (await _save(mcp))["saved_as"] is None
    assert path.read_bytes() == original


async def test_load_caps_large_files_and_tool_rejects_growth_at_the_cap(
    start, tmp_path
):
    path = tmp_path / "saved.json"
    entries = {f"tool_{i:04d}": {"code": "1"} for i in range(1000)}
    _write(path, entries)
    mcp = start(path)
    listed = (await _call(mcp, list_saved=True))["saved_tools"]
    assert len(listed) == 256
    with pytest.raises(ToolError, match="cache is full"):
        await _save(mcp, "overflow")
    # Replacing one existing entry is still allowed at capacity.
    first = next(iter(listed))
    assert (await _save(mcp, first, "2 + 2"))["saved_as"] == first
    assert len((await _call(start(path), list_saved=True))["saved_tools"]) == len(
        listed
    )
