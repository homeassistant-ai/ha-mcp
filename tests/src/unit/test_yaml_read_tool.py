"""Unit tests for the ha_config_get_yaml MCP tool wrapper (#1788)."""

import asyncio
import fnmatch
import posixpath
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
    """A named target raises, whether or not anything else was resolved."""
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
    """A named target has no expansion to salvage, so it stays an error."""
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


def _bracket_dir(*names):
    """A ``list_files`` mock over ``names``, matching the way the component does.

    The component filters with ``fnmatch.fnmatch(item.name, pattern)``, which is
    why a file called ``svc[a].yaml`` never appears in its own expansion.
    """
    recorded: list[str] = []

    def list_files(payload):
        recorded.append(payload["pattern"])
        # The component normalizes the directory it walks and reports paths
        # relative to the config dir, so ``./packages`` and ``packages`` are
        # the same request and both answer with the normalized form.
        assert posixpath.normpath(payload["path"]) == "packages"
        return {
            "success": True,
            "files": [
                {"path": f"packages/{n}", "is_dir": False}
                for n in names
                if fnmatch.fnmatch(n, payload["pattern"])
            ],
        }

    return list_files, recorded


async def test_bracketed_name_is_read_as_a_literal_file():
    """A bracket class cannot match its own name, so the pattern pass alone
    reports files_searched 0 for a file that is right there."""
    list_files, patterns = _bracket_dir("svc[a].yaml")

    fn, _ = await _make_tool(
        {"list_files": list_files, "read_file": _read_ok("- name: probe\n")}
    )

    out = await fn(yaml_path="rest", file="packages/svc[a].yaml")

    assert out["files_searched"] == 1
    assert [m["file"] for m in out["matches"]] == ["packages/svc[a].yaml"]
    # Sorted: the two lookups are gathered, so their arrival order is a
    # scheduling detail and asserting it would be a latent flake.
    assert sorted(patterns) == ["svc[[]a].yaml", "svc[a].yaml"]


async def test_bracketed_name_is_not_masked_by_a_sibling():
    """The sibling the class matches must not stand in for the exact name.

    Both are legitimate readings of the same string, so both are searched -
    stopping at the pattern hit would return a match from another file while
    the file the caller named stayed closed.
    """
    list_files, patterns = _bracket_dir("svc[a].yaml", "svca.yaml")

    fn, _ = await _make_tool(
        {"list_files": list_files, "read_file": _read_ok("- name: probe\n")}
    )

    out = await fn(yaml_path="rest", file="packages/svc[a].yaml")

    assert out["files_searched"] == 2
    assert [m["file"] for m in out["matches"]] == [
        "packages/svc[a].yaml",
        "packages/svca.yaml",
    ]
    # Sorted: the two lookups are gathered, so their arrival order is a
    # scheduling detail and asserting it would be a latent flake.
    assert sorted(patterns) == ["svc[[]a].yaml", "svc[a].yaml"]


async def test_bracketed_literal_keeps_the_single_target_error_contract():
    """The literal lookup returns the requested file, so it is a named target
    and a parse failure raises instead of degrading to a warning - the same
    contract a path without metacharacters gets."""
    list_files, _ = _bracket_dir("svc[a].yaml")

    fn, _ = await _make_tool(
        {
            "list_files": list_files,
            "read_file": {
                "success": True,
                "path": "packages/svc[a].yaml",
                "content": "...",
                "subtree": None,
                "parse_error": "not valid YAML at line 3, column 5",
            },
        }
    )

    with pytest.raises(ToolError):
        await fn(yaml_path="rest", file="packages/svc[a].yaml")


async def test_named_file_keeps_its_contract_when_a_sibling_tags_along():
    """Strictness belongs to the target, not to the request.

    The sibling arrives because the class matched it, and it must not turn the
    explicitly named file's parse failure into a warning - otherwise whether a
    broken file is diagnosed depends on what else happens to sit next to it.
    """
    list_files, _ = _bracket_dir("svc[a].yaml", "svca.yaml")

    def read(payload):
        if payload["path"] == "packages/svc[a].yaml":
            return {
                "success": True,
                "path": payload["path"],
                "content": "...",
                "subtree": None,
                "parse_error": "not valid YAML at line 3, column 5",
            }
        return _read_ok("- name: sibling\n")

    fn, _ = await _make_tool({"list_files": list_files, "read_file": read})

    with pytest.raises(ToolError):
        await fn(yaml_path="rest", file="packages/svc[a].yaml")


async def test_file_named_like_the_glob_is_still_an_expansion():
    """A pattern is not a name, even when a file happens to be called that.

    ``*`` and ``?`` match their own literal names, so a file genuinely called
    ``*.yaml`` comes back out of its own expansion. Nobody named it, so its
    read failure stays a warning. Deciding provenance by comparing the target
    against the requested string instead would read it as the named file and
    let one unreadable oddity discard every match already found.
    """

    def list_files(payload):
        return {
            "success": True,
            "files": [
                {"path": f"packages/{n}", "is_dir": False}
                for n in ("*.yaml", "a.yaml", "b.yaml")
                if fnmatch.fnmatch(n, payload["pattern"])
            ],
        }

    def read(payload):
        if payload["path"] == "packages/*.yaml":
            return {"success": False, "error": "Path not allowed: packages/*.yaml"}
        return _read_ok("- name: found\n")

    fn, _ = await _make_tool({"list_files": list_files, "read_file": read})

    out = await fn(yaml_path="found", file="packages/*.yaml")

    assert out["count"] == 2
    assert out["files_searched"] == 3
    assert out["warnings"] == [
        "packages/*.yaml was not searched: Path not allowed: packages/*.yaml."
    ]


async def test_equivalent_spelling_is_still_the_named_target():
    """The literal lookup reports the path the component walked, so an
    equivalent caller-side spelling like ``./packages/...`` still resolves to
    the same named target."""
    list_files, _ = _bracket_dir("svc[a].yaml")

    fn, _ = await _make_tool(
        {
            "list_files": list_files,
            "read_file": {
                "success": True,
                "path": "packages/svc[a].yaml",
                "content": "...",
                "subtree": None,
                "parse_error": "not valid YAML at line 3, column 5",
            },
        }
    )

    with pytest.raises(ToolError):
        await fn(yaml_path="rest", file="./packages/svc[a].yaml")


async def test_bracketed_directory_with_a_plain_name_is_read_directly():
    """Only the name is a pattern. The component resolves directories
    literally, so a bracketed folder must not send the request through
    expansion - which would answer a typo with a silent empty result instead of
    the component's "File does not exist"."""

    def list_files(payload):
        raise AssertionError(f"expansion must not run: {payload}")

    read_paths: list[str] = []

    def read(payload):
        read_paths.append(payload["path"])
        return _read_ok("- name: probe\n")

    fn, _ = await _make_tool({"list_files": list_files, "read_file": read})

    out = await fn(yaml_path="rest", file="pack[a]ges/svc.yaml")

    assert read_paths == ["pack[a]ges/svc.yaml"]
    assert out["files_searched"] == 1


async def test_single_match_glob_still_warns_when_its_read_blows_up():
    """An expanded target is lenient however few of them there are.

    The count is an accident of the directory, so a one-file expansion must
    still degrade a failed read to a warning rather than aborting the search,
    exactly as a two-file one does. Leniency follows from the target having
    come out of an expansion, not from how many did.
    """
    fn, _ = await _make_tool(
        {
            "list_files": {
                "success": True,
                "files": [{"path": "packages/only.yaml", "is_dir": False}],
            },
            "read_file": MagicMock(
                side_effect=ConnectionResetError("connection reset")
            ),
        }
    )

    out = await fn(yaml_path="rest", file="packages/*.yaml")

    assert out["success"] is True
    assert out["files_searched"] == 1
    assert out["warnings"] == ["packages/only.yaml was not searched: connection reset."]


async def test_glob_without_brackets_is_resolved_in_one_call():
    """The second lookup is scoped to bracketed names; ``*.yaml`` matching
    nothing is a genuinely empty glob, not an ambiguous literal."""
    list_files, patterns = _bracket_dir("lights.yaml")

    fn, _ = await _make_tool({"list_files": list_files, "read_file": _read_ok("x\n")})

    out = await fn(yaml_path="alert2", file="packages/nothing*.yaml")

    assert out["files_searched"] == 0
    assert patterns == ["nothing*.yaml"]


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
    """A named target that will not parse raises rather than degrading.

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


async def test_named_file_read_exception_raises_instead_of_warning():
    """The strict half of the exception path, in its simplest shape.

    Since ``return_exceptions`` is unconditional, a named target whose read
    blows up reaches an explicit re-raise rather than aborting the gather.
    Dropping that re-raise would hand the caller ``success: true, count: 0``
    for a file that was never opened; the mixed case below covers the same
    door with an expanded sibling alongside.
    """
    fn, _ = await _make_tool(
        {"read_file": MagicMock(side_effect=ConnectionResetError("connection reset"))}
    )

    with pytest.raises(ToolError):
        await fn(yaml_path="rest", file="configuration.yaml")


async def test_named_bracketed_target_read_exception_raises_despite_sibling():
    """A tagging-along sibling does not soften the named file's contract.

    The mixed case: the class turns up a sibling that reads fine while the file
    the caller actually named blows up. Reporting the sibling's result with a
    warning would answer a question nobody asked.
    """
    list_files, _ = _bracket_dir("svc[a].yaml", "svca.yaml")

    def read(payload):
        if payload["path"] == "packages/svc[a].yaml":
            raise ConnectionResetError("connection reset")
        return _read_ok("- name: sibling\n")

    fn, _ = await _make_tool({"list_files": list_files, "read_file": read})

    with pytest.raises(ToolError):
        await fn(yaml_path="rest", file="packages/svc[a].yaml")


async def test_message_less_read_exception_still_names_a_reason():
    """``str()`` is empty on a bare ``TimeoutError()``, so the class name stands
    in - otherwise the warning reads "was not searched: ." and names nothing."""
    fn, _ = await _make_tool(
        {
            "list_files": {
                "success": True,
                "files": [{"path": "packages/only.yaml", "is_dir": False}],
            },
            "read_file": MagicMock(side_effect=TimeoutError()),
        }
    )

    out = await fn(yaml_path="rest", file="packages/*.yaml")

    assert out["warnings"] == ["packages/only.yaml was not searched: TimeoutError."]


async def test_absent_bracketed_literal_is_reported_not_implied():
    """The named file does not exist, only a sibling the class matches.

    Without a word about it the caller reads ``files_searched: 1`` as "your
    file was searched and has no such key" - the same affirmative "I looked"
    this PR removes, wearing a different coat.
    """
    list_files, _ = _bracket_dir("svca.yaml")

    fn, _ = await _make_tool(
        {"list_files": list_files, "read_file": _read_ok("- name: sibling\n")}
    )

    out = await fn(yaml_path="rest", file="packages/svc[a].yaml")

    assert out["files_searched"] == 1
    assert out["count"] == 1
    expected_warning = (
        "No file is literally named packages/svc[a].yaml; searched the 1 "
        "file(s) matching it as a pattern."
    )
    assert out["warnings"] == [expected_warning]
    # The counterexample to counting unreadable files off the warnings list:
    # this warning comes from the resolver, and the one read succeeded.
    assert out["files_unreadable"] == 0


async def test_bracket_class_searches_its_siblings():
    """The motivating case for keeping the pattern pass at all.

    ``svc[12].yaml`` is written as a class on purpose and no such file exists;
    both members must be searched, and the caller is told the literal was not
    among them.
    """
    list_files, _ = _bracket_dir("svc1.yaml", "svc2.yaml")

    fn, _ = await _make_tool(
        {"list_files": list_files, "read_file": _read_ok("- name: member\n")}
    )

    out = await fn(yaml_path="rest", file="packages/svc[12].yaml")

    assert out["files_searched"] == 2
    assert [m["file"] for m in out["matches"]] == [
        "packages/svc1.yaml",
        "packages/svc2.yaml",
    ]
    expected_warning = (
        "No file is literally named packages/svc[12].yaml; searched the 2 "
        "file(s) matching it as a pattern."
    )
    assert out["warnings"] == [expected_warning]


async def test_literal_lookup_failure_raises():
    """Both lookups walk the same directory, so a listing failure on the
    literal one is a broken request, not one unreadable file - it raises rather
    than silently returning only what the pattern pass found."""

    def list_files(payload):
        if payload["pattern"] == "svc[[]a].yaml":
            return {"success": False, "error": "Path not allowed.", "files": []}
        return {
            "success": True,
            "files": [{"path": "packages/svca.yaml", "is_dir": False}],
        }

    fn, _ = await _make_tool({"list_files": list_files, "read_file": _read_ok("x\n")})

    with pytest.raises(ToolError):
        await fn(yaml_path="rest", file="packages/svc[a].yaml")


async def test_unclosed_bracket_resolves_to_one_target():
    """An unclosed bracket is literal to ``fnmatch``, so it matches its own
    name and both lookups return it. The merge is what keeps that a single
    target rather than reading the same file twice."""
    list_files, patterns = _bracket_dir("svc[a.yaml")

    fn, _ = await _make_tool(
        {"list_files": list_files, "read_file": _read_ok("- name: probe\n")}
    )

    out = await fn(yaml_path="rest", file="packages/svc[a.yaml")

    assert out["files_searched"] == 1
    assert [m["file"] for m in out["matches"]] == ["packages/svc[a.yaml"]
    assert sorted(patterns) == ["svc[[]a.yaml", "svc[a.yaml"]
    assert "warnings" not in out


async def test_unclosed_bracket_read_failure_raises():
    """An unclosed bracket names a file, so its failed read is not a warning.

    Both lookups return it - it is literal to ``fnmatch``, so it matches its
    own name - and provenance has to come from the literal one. Reading it off
    the pattern lookup instead would make this file expanded, degrade the
    failure to a warning, and answer ``success: true, count: 0`` for the file
    the caller named exactly.
    """
    list_files, _ = _bracket_dir("svc[a.yaml")

    fn, _ = await _make_tool(
        {
            "list_files": list_files,
            "read_file": MagicMock(
                side_effect=ConnectionResetError("connection reset")
            ),
        }
    )

    with pytest.raises(ToolError):
        await fn(yaml_path="rest", file="packages/svc[a.yaml")


async def test_cancellation_propagates_even_on_an_expanded_target():
    """Leniency covers unreadable files, not a cancelled search.

    ``CancelledError`` is not one file being unreadable: absorbing it into a
    warning beside ``success: true`` would report a search that was called off
    as one that ran and found nothing. It propagates whatever the target's
    provenance, which is what every other per-item fan-out in the codebase
    does.
    """
    fn, _ = await _make_tool(
        {
            "list_files": {
                "success": True,
                "files": [{"path": "packages/only.yaml", "is_dir": False}],
            },
            "read_file": MagicMock(side_effect=asyncio.CancelledError()),
        }
    )

    with pytest.raises(asyncio.CancelledError):
        await fn(yaml_path="rest", file="packages/*.yaml")


async def test_files_unreadable_counts_the_reads_that_failed():
    """The summary fields must not read as a completed search.

    ``count: 0`` beside ``files_searched: 3`` is the affirmative "I looked"
    this tool exists to stop making, so the count of files that were not
    searched travels with them rather than only in prose. Not searched is the
    wider class the warnings already name: a file that was opened and read but
    would not parse counts here too, because its key was never inspected.
    """
    fn, _ = await _make_tool(
        {
            "list_files": {
                "success": True,
                "files": [
                    {"path": f"packages/{name}.yaml", "is_dir": False}
                    for name in ("a", "b", "c")
                ],
            },
            "read_file": MagicMock(
                side_effect=ConnectionResetError("connection reset")
            ),
        }
    )

    out = await fn(yaml_path="rest", file="packages/*.yaml")

    assert out["count"] == 0
    assert out["files_searched"] == 3
    assert out["files_unreadable"] == 3
    # Contents, not just the length: three copies of one file's warning would
    # satisfy a count while describing a different failure.
    assert out["warnings"] == [
        "packages/a.yaml was not searched: connection reset.",
        "packages/b.yaml was not searched: connection reset.",
        "packages/c.yaml was not searched: connection reset.",
    ]


async def test_resolver_warning_and_read_warning_both_survive():
    """The two warning sources are independent and both reach the caller.

    The resolver's "nothing is literally named that" and a per-file read
    failure describe different things, so one must not stand in for the other -
    and only the read counts as an unreadable file.
    """
    list_files, _ = _bracket_dir("svca.yaml", "svcb.yaml")

    def read(payload):
        if payload["path"] == "packages/svca.yaml":
            raise ConnectionResetError("connection reset")
        return _read_ok("- name: sibling\n")

    fn, _ = await _make_tool({"list_files": list_files, "read_file": read})

    out = await fn(yaml_path="rest", file="packages/svc[ab].yaml")

    assert out["count"] == 1
    assert out["files_searched"] == 2
    assert out["files_unreadable"] == 1
    resolver_warning = (
        "No file is literally named packages/svc[ab].yaml; searched the 2 "
        "file(s) matching it as a pattern."
    )
    assert out["warnings"] == [
        resolver_warning,
        "packages/svca.yaml was not searched: connection reset.",
    ]


async def test_nothing_matches_either_reading_says_so_directly():
    """Neither reading found anything, and the warning says that plainly.

    Reporting "searched the 0 file(s) matching it as a pattern" describes a
    search that did not happen. The result stays a success because a bracketed
    name is a glob spelling too, and an empty glob is not an error here - but
    silence would make a typo'd bracketed name look like a key that is simply
    not defined.
    """
    list_files, _ = _bracket_dir("other.yaml")

    fn, _ = await _make_tool({"list_files": list_files, "read_file": _read_ok("x\n")})

    out = await fn(yaml_path="rest", file="packages/svc[a].yaml")

    assert out["success"] is True
    assert out["files_searched"] == 0
    assert out["count"] == 0
    assert out["files_unreadable"] == 0
    expected_warning = (
        "No file is literally named packages/svc[a].yaml, and nothing matched "
        "it as a pattern either."
    )
    assert out["warnings"] == [expected_warning]


async def test_mixed_pattern_keeps_its_literal_lookup_but_not_the_warning():
    """``*`` alongside the bracket is pattern syntax, so nothing was named.

    A caller writing ``[ab]*.yaml`` is not naming a file, and telling them no
    file is called that would hang a ``warnings`` key on an ordinary,
    completely successful glob. The literal lookup still runs, because a file
    can genuinely carry that name.
    """
    list_files, patterns = _bracket_dir("a1.yaml", "b1.yaml")

    fn, _ = await _make_tool(
        {"list_files": list_files, "read_file": _read_ok("- name: probe\n")}
    )

    out = await fn(yaml_path="rest", file="packages/[ab]*.yaml")

    assert out["files_searched"] == 2
    assert "warnings" not in out
    assert sorted(patterns) == ["[[]ab][*].yaml", "[ab]*.yaml"]


async def test_mixed_pattern_literal_hit_is_still_an_expansion():
    """Under a pattern, even an exact hit is an expansion match.

    The caller wrote ``*``, so nothing was named - and a file that happens to
    be called ``[ab]*.yaml`` must not become the strict target, or one failed
    read of it would discard the matches the pattern half already found. Same
    shape the resolver removed for ``*.yaml``, reachable through the literal
    lookup instead, so one predicate has to drive both halves.
    """
    list_files, _ = _bracket_dir("[ab]*.yaml", "a1.yaml")

    def read(payload):
        if payload["path"] == "packages/[ab]*.yaml":
            raise ConnectionResetError("connection reset")
        return _read_ok("- name: probe\n")

    fn, _ = await _make_tool({"list_files": list_files, "read_file": read})

    out = await fn(yaml_path="rest", file="packages/[ab]*.yaml")

    assert out["count"] == 1
    assert out["matches"][0]["file"] == "packages/a1.yaml"
    assert out["files_searched"] == 2
    assert out["files_unreadable"] == 1
    assert out["warnings"] == [
        "packages/[ab]*.yaml was not searched: connection reset."
    ]


async def test_both_lookups_failing_raises():
    """The realistic component failure is path-based and hits both lookups.

    Whichever of the two concurrent calls reports first, the request is broken
    in the same way, so it raises rather than returning a half-resolved
    expansion.
    """
    fn, _ = await _make_tool(
        {
            "list_files": {"success": False, "error": "Path not allowed.", "files": []},
            "read_file": _read_ok("x\n"),
        }
    )

    with pytest.raises(ToolError):
        await fn(yaml_path="rest", file="packages/svc[a].yaml")
