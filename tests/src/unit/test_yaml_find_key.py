"""Unit tests for the Core-annotation YAML key finder."""

from __future__ import annotations

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError

from .test_yaml_read_tool import _make_tool, _read_ok


@pytest.fixture(autouse=True)
def _filesystem_tools_enabled(monkeypatch):
    from ha_mcp import config as ha_mcp_config
    from ha_mcp.tools.tools_filesystem import _reset_caller_token_cache

    _reset_caller_token_cache()
    monkeypatch.setenv("ENABLE_BETA_FEATURES", "true")
    monkeypatch.setenv("HAMCP_ENABLE_FILESYSTEM_TOOLS", "true")
    monkeypatch.setattr(ha_mcp_config, "_settings", None)
    yield
    _reset_caller_token_cache()
    ha_mcp_config._settings = None


class TestFindYamlKeyFiles:
    async def test_filters_core_discovered_files_by_the_requested_glob(
        self, monkeypatch
    ):
        from ha_mcp.tools import tools_yaml_read

        async def fake_service(client, service, data):
            assert service == "find_yaml_key"
            assert data == {"yaml_path": "template"}
            return {
                "success": True,
                "files": [
                    "custom_packages/nested/mcp.yaml",
                    "other.yaml",
                ],
            }

        monkeypatch.setattr(tools_yaml_read, "call_mcp_tools_service", fake_service)

        files = await tools_yaml_read._find_yaml_key_files(
            object(), "custom_packages/*.yaml", "template"
        )

        assert files == ["custom_packages/nested/mcp.yaml"]

    async def test_bracketed_directory_is_literal(self, monkeypatch):
        """A class in a folder name must not select a sibling folder."""
        from ha_mcp.tools import tools_yaml_read

        async def fake_service(client, service, data):
            return {
                "success": True,
                "files": ["pack[a]ges/svc.yaml", "packages/svc.yaml"],
            }

        monkeypatch.setattr(tools_yaml_read, "call_mcp_tools_service", fake_service)

        files = await tools_yaml_read._find_yaml_key_files(
            object(), "pack[a]ges/*.yaml", "template"
        )

        assert files == ["pack[a]ges/svc.yaml"]

    async def test_bracketed_file_name_keeps_the_literal(self, monkeypatch):
        """A file named svc[a].yaml cannot fnmatch itself; keep it exactly."""
        from ha_mcp.tools import tools_yaml_read

        async def fake_service(client, service, data):
            return {
                "success": True,
                "files": ["packages/svc[a].yaml", "packages/svca.yaml"],
            }

        monkeypatch.setattr(tools_yaml_read, "call_mcp_tools_service", fake_service)

        files = await tools_yaml_read._find_yaml_key_files(
            object(), "packages/svc[a].yaml", "template"
        )

        assert files == ["packages/svc[a].yaml", "packages/svca.yaml"]

    async def test_non_list_files_falls_back(self, monkeypatch):
        from ha_mcp.tools import tools_yaml_read

        async def fake_service(client, service, data):
            return {"success": True, "files": "not-a-list"}

        monkeypatch.setattr(tools_yaml_read, "call_mcp_tools_service", fake_service)

        assert (
            await tools_yaml_read._find_yaml_key_files(
                object(), "custom_packages/*.yaml", "template"
            )
            is None
        )

    async def test_component_failure_falls_back(self, monkeypatch):
        from ha_mcp.tools import tools_yaml_read

        async def fake_service(client, service, data):
            raise RuntimeError("component unavailable")

        monkeypatch.setattr(tools_yaml_read, "call_mcp_tools_service", fake_service)

        assert (
            await tools_yaml_read._find_yaml_key_files(
                object(), "custom_packages/*.yaml", "template"
            )
            is None
        )


async def test_tool_uses_core_discovery_for_a_glob():
    from .test_yaml_read_tool import _make_tool, _read_ok

    fn, client = await _make_tool(
        {
            "find_yaml_key": {
                "success": True,
                "files": ["custom_packages/nested/mcp.yaml", "other.yaml"],
            },
            "list_files": {"success": True, "files": []},
            "read_file": _read_ok("template:\n  - trigger: []\n"),
        }
    )

    out = await fn(yaml_path="template", file="custom_packages/*.yaml")

    assert out["count"] == 1
    assert out["files_searched"] == 1
    assert out["matches"][0]["file"] == "custom_packages/nested/mcp.yaml"
    assert any(
        c.args[1] == "find_yaml_key" for c in client.call_service.await_args_list
    )


async def test_tool_falls_back_when_discovery_has_no_files():
    from .test_yaml_read_tool import _make_tool, _read_ok

    fn, client = await _make_tool(
        {
            "find_yaml_key": {"success": True, "files": []},
            "list_files": {
                "success": True,
                "files": [{"path": "custom_packages/a.yaml", "is_dir": False}],
            },
            "read_file": _read_ok("template:\n"),
        }
    )

    out = await fn(yaml_path="template", file="custom_packages/*.yaml")

    assert out["count"] == 1
    assert any(c.args[1] == "list_files" for c in client.call_service.await_args_list)


async def test_tool_merges_glob_matches_that_discovery_missed():
    """A package Core rejected is absent from discovery but must stay visible."""
    from .test_yaml_read_tool import _make_tool, _read_ok

    fn, _ = await _make_tool(
        {
            "find_yaml_key": {
                "success": True,
                "files": ["custom_packages/zgood.yaml"],
            },
            "list_files": {
                "success": True,
                "files": [
                    {"path": "custom_packages/zgood.yaml", "is_dir": False},
                    {"path": "custom_packages/a-rejected.yaml", "is_dir": False},
                ],
            },
            "read_file": _read_ok("template:\n"),
        }
    )

    out = await fn(yaml_path="template", file="custom_packages/*.yaml")

    assert out["count"] == 2
    assert [match["file"] for match in out["matches"]] == [
        "custom_packages/a-rejected.yaml",
        "custom_packages/zgood.yaml",
    ]


async def test_tool_warns_when_glob_expansion_fails_after_discovery():
    """A listing failure must not discard files discovery already found."""
    from .test_yaml_read_tool import _make_tool, _read_ok

    fn, _ = await _make_tool(
        {
            "find_yaml_key": {
                "success": True,
                "files": ["custom_packages/good.yaml"],
            },
            "list_files": {"success": False, "error": "listing failed"},
            "read_file": _read_ok("template:\n"),
        }
    )

    out = await fn(yaml_path="template", file="custom_packages/*.yaml")

    assert out["count"] == 1
    assert out["warnings"] == [
        "Glob expansion failed: listing failed. Files matching the pattern may be missing from this result."
    ]


async def test_listing_failure_names_the_bracketed_literal():
    """A named literal must not vanish behind a generic listing failure."""
    from .test_yaml_read_tool import _make_tool, _read_ok

    fn, _ = await _make_tool(
        {
            "find_yaml_key": {
                "success": True,
                "files": ["packages/svc1.yaml"],
            },
            "list_files": {"success": False, "error": "listing failed"},
            "read_file": _read_ok("template:\n"),
        }
    )

    out = await fn(yaml_path="template", file="packages/svc[12].yaml")

    assert out["count"] == 1
    assert out["warnings"] == [
        "Glob expansion failed: listing failed. Files matching the pattern may be missing from this result.",
        "packages/svc[12].yaml was not searched because its listing failed.",
    ]


async def test_listing_failure_does_not_flag_a_discovered_bracketed_literal():
    """A discovered named literal is read, so it is not reported unsearched."""
    from .test_yaml_read_tool import _make_tool, _read_ok

    fn, _ = await _make_tool(
        {
            "find_yaml_key": {
                "success": True,
                "files": ["packages/svc[12].yaml"],
            },
            "list_files": {"success": False, "error": "listing failed"},
            "read_file": _read_ok("template:\n"),
        }
    )

    out = await fn(yaml_path="template", file="packages/svc[12].yaml")

    assert out["count"] == 1
    assert out["warnings"] == [
        "Glob expansion failed: listing failed. Files matching the pattern "
        "may be missing from this result."
    ]


async def test_listing_failure_with_empty_discovery_warns():
    """Discovery ran but found nothing; a listing failure is still a warning."""
    from .test_yaml_read_tool import _make_tool

    fn, _ = await _make_tool(
        {
            "find_yaml_key": {"success": True, "files": []},
            "list_files": {"success": False, "error": "Path not allowed"},
        }
    )

    out = await fn(yaml_path="sensor", file="*.yaml")

    assert out["success"] is True
    assert out["count"] == 0
    assert out["warnings"] == [
        "Glob expansion failed: Path not allowed. Files matching the pattern "
        "may be missing from this result."
    ]


async def test_tool_warns_when_discovery_is_unavailable():
    """A fallback-only search must say subfolders may be missing."""
    from .test_yaml_read_tool import _make_tool, _read_ok

    fn, _ = await _make_tool(
        {
            "find_yaml_key": {"success": False, "error": "component too old"},
            "list_files": {
                "success": True,
                "files": [{"path": "custom_packages/a.yaml", "is_dir": False}],
            },
            "read_file": _read_ok("template:\n"),
        }
    )

    out = await fn(yaml_path="template", file="custom_packages/*.yaml")

    assert out["count"] == 1
    assert any(
        "packages or subfolders may be missing" in warning.lower()
        for warning in out["warnings"]
    )


async def test_discovered_bracketed_literal_keeps_strict_parse_errors():
    """A named bracketed file must not soften a parse failure into a warning."""
    from ha_mcp._vendor.fastmcp.exceptions import ToolError

    from .test_yaml_read_tool import _make_tool

    def read(payload):
        return {
            "success": True,
            "path": payload["path"],
            "content": "...",
            "subtree": None,
            "parse_error": "not valid YAML at line 3, column 5",
        }

    fn, _ = await _make_tool(
        {
            "find_yaml_key": {
                "success": True,
                "files": ["packages/svc[a].yaml"],
            },
            "list_files": {
                "success": True,
                "files": [{"path": "packages/svc[a].yaml", "is_dir": False}],
            },
            "read_file": read,
        }
    )

    with pytest.raises(ToolError):
        await fn(yaml_path="rest", file="packages/svc[a].yaml")


async def test_tool_warns_when_a_discovered_path_is_not_readable():
    """A merged include keeps the parent annotation; say the path is not there."""
    from .test_yaml_read_tool import _make_tool, _read_ok

    fn, _ = await _make_tool(
        {
            "find_yaml_key": {
                "success": True,
                "files": ["custom_packages/merged.yaml"],
            },
            "list_files": {
                "success": True,
                "files": [{"path": "custom_packages/merged.yaml", "is_dir": False}],
            },
            "read_file": _read_ok(None),
        }
    )

    out = await fn(yaml_path="shell_command.mcp_secret", file="custom_packages/*.yaml")

    assert out["count"] == 0
    assert any(
        warning.startswith("custom_packages/merged.yaml is annotated as defining")
        for warning in out["warnings"]
    )


def test_failure_reason_falls_back_outside_the_envelope():
    from ha_mcp.tools import tools_yaml_read

    assert tools_yaml_read._failure_reason(RuntimeError("boom")) == "boom"
    assert tools_yaml_read._failure_reason(ValueError('"a string"')) == '"a string"'


async def test_list_failure_warns_when_discovery_ran():
    """Discovery answered, so a listing failure only narrows the result."""
    fn, _ = await _make_tool(
        {
            "list_files": {"success": False, "error": "Path not allowed.", "files": []},
            "read_file": _read_ok("x\n"),
        }
    )

    out = await fn(yaml_path="alert2", file="packages/*.yaml")

    assert out["success"] is True
    assert out["count"] == 0
    assert out["warnings"] == [
        "Glob expansion failed: Path not allowed. Files matching the pattern may be missing from this result."
    ]


async def test_list_failure_raises_when_discovery_is_unavailable():
    """Without discovery the glob is the only route, so a failure still raises."""
    fn, _ = await _make_tool(
        {
            "find_yaml_key": {"success": False, "error": "component too old"},
            "list_files": {"success": False, "error": "Path not allowed.", "files": []},
            "read_file": _read_ok("x\n"),
        }
    )

    with pytest.raises(ToolError):
        await fn(yaml_path="alert2", file="packages/*.yaml")


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

    out = await fn(yaml_path="rest", file="*.yaml")
    assert out["success"] is True
    assert out["warnings"] == [
        "Glob expansion failed: Path not allowed. Files matching the pattern may be missing from this result."
    ]
    assert seen["path"] == "."


async def test_literal_lookup_failure_drops_pattern_matches_with_a_warning():
    """A failed bracketed lookup raises immediately; discovery turns it into a warning."""
    from .test_yaml_read_tool import _make_tool, _read_ok

    def list_files(payload):
        if payload["pattern"] == "svc[[]a].yaml":
            return {"success": False, "error": "Path not allowed.", "files": []}
        return {
            "success": True,
            "files": [{"path": "packages/svca.yaml", "is_dir": False}],
        }

    fn, _ = await _make_tool({"list_files": list_files, "read_file": _read_ok("x\n")})

    out = await fn(yaml_path="rest", file="packages/svc[a].yaml")

    assert out["success"] is True
    assert out["count"] == 0
    assert out["matches"] == []
    assert out["warnings"] == [
        "Glob expansion failed: Path not allowed. Files matching the pattern may be missing from this result.",
        "packages/svc[a].yaml was not searched because its listing failed.",
    ]


async def test_both_lookups_failing_raises():
    """The realistic component failure is path-based and hits both lookups.

    Whichever of the two concurrent calls reports first, the request is broken
    in the same way, so it raises rather than returning a half-resolved
    expansion.
    """
    fn, _ = await _make_tool(
        {
            "find_yaml_key": {"success": False, "error": "component too old"},
            "list_files": {"success": False, "error": "Path not allowed.", "files": []},
            "read_file": _read_ok("x\n"),
        }
    )

    with pytest.raises(ToolError):
        await fn(yaml_path="rest", file="packages/svc[a].yaml")
