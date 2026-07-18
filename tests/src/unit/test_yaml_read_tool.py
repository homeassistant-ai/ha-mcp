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


@pytest.fixture(autouse=True)
def _filesystem_tools_enabled(monkeypatch):
    """The tool registers only with filesystem tools on — it returns
    config-file contents, so it sits behind the same flag as ha_read_file.

    Autouse because every test below needs a registered tool; the gate itself
    is asserted by TestGating.
    """
    from ha_mcp import config as ha_mcp_config

    # enable_filesystem_tools is a beta sub-flag, so the master toggle has to
    # be on too or the master gate forces it back off (BETA_FEATURE_FIELDS).
    monkeypatch.setenv("ENABLE_BETA_FEATURES", "true")
    monkeypatch.setenv("HAMCP_ENABLE_FILESYSTEM_TOOLS", "true")
    monkeypatch.setattr(ha_mcp_config, "_settings", None)
    yield
    ha_mcp_config._settings = None


class _Raw:
    """Marks a value that call_service returns verbatim.

    Everything else is wrapped in HA's ``{"service_response": ...}`` envelope,
    which is always a dict — so this is the only way to exercise a caller's
    handling of a non-dict service result.
    """

    def __init__(self, value):
        self.value = value


def _service_mock(responses: dict):
    """call_service mock answering the bootstrap plus per-service responses.

    ``responses`` maps a service name to either a single response dict or a
    callable taking the payload (so read_file can answer per-file). Wrap a
    value in ``_Raw`` to skip the service_response envelope.
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
        if isinstance(result, _Raw):
            return result.value
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
    # None when the feature gate refused to register (see TestGating).
    return (captured["fns"][0] if captured.get("fns") else None), client


def _read_ok(subtree, parsed=None):
    body = {"success": True, "path": "x", "content": "...", "subtree": subtree}
    if parsed is not None:
        body["parsed"] = parsed
    return body


class TestGating:
    """Which flag the tool hangs on — reading is not editing, but it IS a
    config-file read."""

    async def test_registers_without_yaml_editing_flag(self, monkeypatch):
        """Registers with YAML *editing* off.

        This is the whole reason it is a separate module from
        tools_yaml_config — that module's register function returns early when
        the editing flag is off, and reading a fragment is not an edit.
        """
        from ha_mcp import config as ha_mcp_config

        monkeypatch.setenv("ENABLE_YAML_CONFIG_EDITING", "false")
        monkeypatch.setattr(ha_mcp_config, "_settings", None)

        fn, _ = await _make_tool({"read_file": _read_ok("rest:\n")})

        assert fn is not None

    async def test_not_registered_without_filesystem_tools_flag(self, monkeypatch):
        """Does NOT register with filesystem tools off.

        It returns config-file contents through the same read_file/list_files
        component services as ha_read_file/ha_list_files. An install that
        turned those off must not get a config-read surface back through this
        tool.
        """
        from ha_mcp import config as ha_mcp_config

        monkeypatch.setenv("HAMCP_ENABLE_FILESYSTEM_TOOLS", "false")
        monkeypatch.setattr(ha_mcp_config, "_settings", None)

        fn, _ = await _make_tool({"read_file": _read_ok("rest:\n")})

        assert fn is None


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
    """A single explicit target has no siblings to salvage, so it still raises."""
    fn, _ = await _make_tool(
        {"read_file": {"success": False, "error": "File does not exist: nope.yaml"}}
    )

    with pytest.raises(ToolError):
        await fn(yaml_path="rest", file="nope.yaml")


async def test_glob_read_failure_warns_instead_of_aborting():
    """One unreadable file must not discard the matches already found.

    Reachable without contrivance: the glob is not restricted to *.yaml, so
    `packages/*` turns up a README the component refuses to read.
    """

    def read(payload):
        if payload["path"] == "packages/README.md":
            return {"success": False, "error": "Path not allowed: packages/README.md"}
        return _read_ok("- name: my_alert\n")

    fn, _ = await _make_tool(
        {
            "list_files": {
                "success": True,
                "files": [
                    {"path": "packages/README.md", "is_dir": False},
                    {"path": "packages/alert2.yaml", "is_dir": False},
                ],
            },
            "read_file": read,
        }
    )

    out = await fn(yaml_path="alert2", file="packages/*")

    assert out["count"] == 1
    assert out["matches"][0]["file"] == "packages/alert2.yaml"
    assert out["files_searched"] == 2
    assert out["warnings"] == [
        "packages/README.md was not searched: Path not allowed: packages/README.md."
    ]


async def test_glob_read_exception_warns_instead_of_aborting():
    """A read that blows up is the same failure class as one that says no."""

    def read(payload):
        if payload["path"] == "packages/broken.yaml":
            raise RuntimeError("connection reset")
        return _read_ok("- name: my_alert\n")

    fn, _ = await _make_tool(
        {
            "list_files": {
                "success": True,
                "files": [
                    {"path": "packages/broken.yaml", "is_dir": False},
                    {"path": "packages/alert2.yaml", "is_dir": False},
                ],
            },
            "read_file": read,
        }
    )

    out = await fn(yaml_path="alert2", file="packages/*.yaml")

    assert out["count"] == 1
    assert out["matches"][0]["file"] == "packages/alert2.yaml"
    assert out["warnings"] == [
        "packages/broken.yaml was not searched: connection reset."
    ]


async def test_glob_malformed_response_warns_instead_of_aborting():
    """A response that is not a dict at all is the same failure class.

    Every way "this one file could not be searched" can present must degrade
    the same way under a glob, or the inconsistency just moves.
    """

    def read(payload):
        if payload["path"] == "packages/weird.yaml":
            return _Raw("not a dict at all")
        return _read_ok("- name: my_alert\n")

    fn, _ = await _make_tool(
        {
            "list_files": {
                "success": True,
                "files": [
                    {"path": "packages/weird.yaml", "is_dir": False},
                    {"path": "packages/alert2.yaml", "is_dir": False},
                ],
            },
            "read_file": read,
        }
    )

    out = await fn(yaml_path="alert2", file="packages/*.yaml")

    assert out["count"] == 1
    assert out["matches"][0]["file"] == "packages/alert2.yaml"
    assert out["warnings"] == [
        "packages/weird.yaml was not searched: unexpected read_file response."
    ]


async def test_single_file_malformed_response_still_raises():
    """With one target there is nothing to salvage, so it stays an error."""
    fn, _ = await _make_tool({"read_file": _Raw("not a dict at all")})

    with pytest.raises(ToolError):
        await fn(yaml_path="rest", file="configuration.yaml")


async def test_empty_glob_matches_nothing_without_raising():
    """A glob matching no files is an empty result, not an error.

    files_searched=0 is what separates it from "no file defines the key".
    """
    fn, _ = await _make_tool(
        {
            "list_files": {"success": True, "files": []},
            "read_file": _read_ok("x\n"),
        }
    )

    out = await fn(yaml_path="alert2", file="packages/*.yaml")

    assert out["success"] is True
    assert out["matches"] == []
    assert out["count"] == 0
    assert out["files_searched"] == 0
    assert "warnings" not in out


async def test_list_failure_raises_tool_error():
    fn, _ = await _make_tool(
        {
            "list_files": {"success": False, "error": "Path not allowed.", "files": []},
            "read_file": _read_ok("x\n"),
        }
    )

    with pytest.raises(ToolError):
        await fn(yaml_path="alert2", file="packages/*.yaml")


async def test_parse_error_warns_instead_of_reading_as_a_non_match():
    """A file that could not be parsed must not read as "key not defined".

    The glob case is the dangerous one: one broken package would otherwise make
    the whole search report a clean absence.
    """

    def read(payload):
        if payload["path"] == "packages/broken.yaml":
            return {
                "success": True,
                "path": payload["path"],
                "content": "...",
                "subtree": None,
                "parse_error": "not valid YAML at line 3, column 5",
            }
        return _read_ok("- name: ok\n")

    fn, _ = await _make_tool(
        {
            "list_files": {
                "success": True,
                "files": [
                    {"path": "packages/broken.yaml", "is_dir": False},
                    {"path": "packages/good.yaml", "is_dir": False},
                ],
            },
            "read_file": read,
        }
    )

    out = await fn(yaml_path="alert2", file="packages/*.yaml")

    assert out["count"] == 1
    assert out["matches"][0]["file"] == "packages/good.yaml"
    assert out["warnings"] == [
        "packages/broken.yaml was not searched: not valid YAML at line 3, column 5."
    ]


async def test_single_file_parse_error_raises_instead_of_reporting_no_match():
    """A single explicit target that will not parse has no siblings to salvage.

    Soft-degrading to a warning would make a real parse failure indistinguishable
    from "key absent" at the success/count level, so it raises instead.
    """
    fn, _ = await _make_tool(
        {
            "read_file": {
                "success": True,
                "path": "configuration.yaml",
                "content": "...",
                "subtree": None,
                "parse_error": "not valid YAML at line 3, column 5",
            }
        }
    )

    with pytest.raises(ToolError):
        await fn(yaml_path="rest", file="configuration.yaml")


async def test_no_warnings_key_when_nothing_degraded():
    """`warnings` is omitted when empty, per the tool return contract."""
    fn, _ = await _make_tool({"read_file": _read_ok("method: GET\n")})

    out = await fn(yaml_path="rest", file="configuration.yaml")

    assert "warnings" not in out


async def test_root_level_glob_lists_config_root():
    """A glob with no directory part asks the lister for the config root.

    The component denies that (the root is not in ALLOWED_READ_DIRS), which is
    the pre-existing lister boundary — root files stay readable one-by-one via
    an explicit `file`. This pins the '.' the tool sends, so the request is a
    deliberate deny rather than a malformed path.
    """
    seen: dict = {}

    def list_files(payload):
        seen["path"] = payload["path"]
        return {"success": False, "error": "Path not allowed.", "files": []}

    fn, _ = await _make_tool({"list_files": list_files, "read_file": _read_ok("x\n")})

    with pytest.raises(ToolError):
        await fn(yaml_path="rest", file="*.yaml")
    assert seen["path"] == "."
