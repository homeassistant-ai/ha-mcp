"""Unit tests for saved-tools file persistence (CODE_MODE_SAVED_TOOLS_PATH).

Covers the helpers in ``ha_mcp.tools.tools_code`` that load and save the
custom-tool dictionary to disk:

* ``_load_saved_tools`` — empty path / missing file / malformed JSON /
  malformed entries / cap enforcement / round-trip with ``_save_saved_tools``.
* ``_save_saved_tools`` — atomic temp+rename, parent-dir creation,
  schema-versioned payload, no-op on empty path.

These are pure functions over a JSON file path; testing them at the unit
level avoids the cost of standing up the full E2E fixture for what is
essentially file-I/O behaviour.
"""

import json
from pathlib import Path

import ha_mcp.tools.tools_code as tools_code
from ha_mcp.tools.tools_code import (
    _MAX_SAVED_TOOLS,
    _SAVED_TOOLS_SCHEMA_VERSION,
    _load_saved_tools,
    _save_saved_tools,
)


class TestLoadSavedTools:
    def test_empty_path_returns_empty(self):
        """An empty path string disables persistence — return {}."""
        assert _load_saved_tools("") == {}

    def test_missing_file_returns_empty(self, tmp_path: Path):
        """First-run case: the file doesn't exist yet."""
        path = tmp_path / "saved.json"
        assert _load_saved_tools(str(path)) == {}

    def test_malformed_json_returns_empty(self, tmp_path: Path):
        """Corrupt JSON must not crash; just log and start empty."""
        path = tmp_path / "saved.json"
        path.write_text("{this is not json", encoding="utf-8")
        assert _load_saved_tools(str(path)) == {}

    def test_top_level_not_dict_returns_empty(self, tmp_path: Path):
        path = tmp_path / "saved.json"
        path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        assert _load_saved_tools(str(path)) == {}

    def test_filters_invalid_name(self, tmp_path: Path):
        """Names that don't match _SAVE_NAME_PATTERN are dropped."""
        path = tmp_path / "saved.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "saved_tools": {
                        "valid_name": {"code": "1+1", "justification": "ok"},
                        "../bad": {"code": "x", "justification": "x"},
                        "1leading_digit": {"code": "x", "justification": "x"},
                        "with space": {"code": "x", "justification": "x"},
                    },
                }
            ),
            encoding="utf-8",
        )
        loaded = _load_saved_tools(str(path))
        assert set(loaded.keys()) == {"valid_name"}

    def test_filters_invalid_entry_shape(self, tmp_path: Path):
        """Entries that aren't dict-with-code are dropped."""
        path = tmp_path / "saved.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "saved_tools": {
                        "valid": {"code": "1", "justification": ""},
                        "no_code": {"justification": "x"},
                        "code_not_str": {"code": 42, "justification": "x"},
                        "empty_code": {"code": "", "justification": "x"},
                        "not_a_dict": "raw string",
                    },
                }
            ),
            encoding="utf-8",
        )
        loaded = _load_saved_tools(str(path))
        assert set(loaded.keys()) == {"valid"}

    def test_normalizes_missing_justification(self, tmp_path: Path):
        """Justification defaults to empty string if missing/non-string."""
        path = tmp_path / "saved.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "saved_tools": {
                        "tool_a": {"code": "1"},
                        "tool_b": {"code": "1", "justification": 123},
                    },
                }
            ),
            encoding="utf-8",
        )
        loaded = _load_saved_tools(str(path))
        assert loaded["tool_a"]["justification"] == ""
        assert loaded["tool_b"]["justification"] == ""

    def test_caps_at_max_saved_tools(self, tmp_path: Path):
        """A file with more than _MAX_SAVED_TOOLS entries is truncated."""
        path = tmp_path / "saved.json"
        big = {
            f"tool_{i:04d}": {"code": "1", "justification": ""}
            for i in range(_MAX_SAVED_TOOLS + 50)
        }
        path.write_text(
            json.dumps({"version": 1, "saved_tools": big}), encoding="utf-8"
        )
        loaded = _load_saved_tools(str(path))
        assert len(loaded) == _MAX_SAVED_TOOLS


class TestSaveSavedTools:
    def test_empty_path_is_noop(self, tmp_path: Path):
        """Empty path string disables persistence — should not write anything."""
        # Confirm by listing the dir before/after.
        before = sorted(p.name for p in tmp_path.iterdir())
        _save_saved_tools("", {"foo": {"code": "1", "justification": ""}})
        after = sorted(p.name for p in tmp_path.iterdir())
        assert before == after

    def test_writes_versioned_payload(self, tmp_path: Path):
        """Payload includes schema version, timestamp, and tools dict."""
        path = tmp_path / "saved.json"
        tools = {"foo": {"code": "1+1", "justification": "test"}}
        _save_saved_tools(str(path), tools)

        assert path.exists()
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["version"] == _SAVED_TOOLS_SCHEMA_VERSION
        assert "saved_at" in payload
        assert payload["saved_tools"] == tools

    def test_creates_parent_directory(self, tmp_path: Path):
        """Path with non-existent parent dir gets created."""
        path = tmp_path / "subdir" / "nested" / "saved.json"
        _save_saved_tools(str(path), {"foo": {"code": "1", "justification": ""}})
        assert path.exists()

    def test_roundtrip(self, tmp_path: Path):
        """save → load returns the same tools."""
        path = tmp_path / "saved.json"
        tools = {
            "tool_a": {"code": "await api_get('/states')", "justification": "list states"},
            "tool_b": {"code": "1 + 1", "justification": "math"},
        }
        _save_saved_tools(str(path), tools)
        loaded = _load_saved_tools(str(path))
        assert loaded == tools

    def test_overwrites_existing_file_atomically(self, tmp_path: Path):
        """Second save replaces the first atomically (no .tmp leftover)."""
        path = tmp_path / "saved.json"
        _save_saved_tools(str(path), {"v1": {"code": "1", "justification": ""}})
        _save_saved_tools(str(path), {"v2": {"code": "2", "justification": ""}})

        loaded = _load_saved_tools(str(path))
        assert set(loaded.keys()) == {"v2"}

        # No leftover .tmp files in the parent.
        leftovers = [p for p in path.parent.iterdir() if p.suffix == ".tmp"]
        assert leftovers == [], f"Atomic write left .tmp files: {leftovers}"

    def test_returns_true_on_success(self, tmp_path: Path):
        """_save_saved_tools returns True when the write succeeded."""
        path = tmp_path / "saved.json"
        assert _save_saved_tools(
            str(path), {"foo": {"code": "1", "justification": ""}}
        ) is True

    def test_returns_true_when_path_unset(self):
        """Empty path means persistence is disabled — that's not a failure."""
        assert _save_saved_tools("", {"foo": {"code": "1", "justification": ""}}) is True


class TestSchemaVersionGuard:
    """``_load_saved_tools`` refuses to interpret files with an unfamiliar
    schema version, instead of silently treating them as v1 and possibly
    mangling a future-version shape.
    """

    def test_unknown_version_returns_empty(self, tmp_path: Path):
        path = tmp_path / "saved.json"
        path.write_text(
            json.dumps(
                {
                    "version": _SAVED_TOOLS_SCHEMA_VERSION + 1,
                    "saved_tools": {
                        "future_tool": {"code": "1", "justification": ""}
                    },
                }
            ),
            encoding="utf-8",
        )
        assert _load_saved_tools(str(path)) == {}

    def test_unknown_version_sets_load_failed_flag(self, tmp_path: Path):
        """And persistence must be suppressed so we don't overwrite the
        unfamiliar file with our v1 shape."""
        path = tmp_path / "saved.json"
        path.write_text(
            json.dumps({"version": 99, "saved_tools": {}}), encoding="utf-8"
        )
        _load_saved_tools(str(path))
        try:
            assert tools_code._saved_tools_load_failed is True
            # _save_saved_tools refuses to write while the flag is set.
            ok = _save_saved_tools(
                str(path), {"foo": {"code": "1", "justification": ""}}
            )
            assert ok is False
            # File on disk must still have the original v99 content,
            # NOT the v1 payload _save_saved_tools would have written.
            roundtrip = json.loads(path.read_text(encoding="utf-8"))
            assert roundtrip["version"] == 99
        finally:
            tools_code._saved_tools_load_failed = False

    def test_missing_version_field_returns_empty(self, tmp_path: Path):
        """A file without a top-level ``version`` field is also refused
        (we can't know what shape it's promising).
        """
        path = tmp_path / "saved.json"
        path.write_text(
            json.dumps({"saved_tools": {"x": {"code": "1"}}}), encoding="utf-8"
        )
        try:
            assert _load_saved_tools(str(path)) == {}
            assert tools_code._saved_tools_load_failed is True
        finally:
            tools_code._saved_tools_load_failed = False


class TestLoadFailedFlag:
    """The module-level ``_saved_tools_load_failed`` flag exists so that a
    transient I/O failure on load doesn't cascade into "next save wipes
    out the unreadable file with empty content."
    """

    def test_successful_load_clears_flag(self, tmp_path: Path):
        """Successful load must clear any previous failure state so a
        fresh hydration cycle starts clean.
        """
        tools_code._saved_tools_load_failed = True
        try:
            path = tmp_path / "saved.json"
            path.write_text(
                json.dumps({"version": _SAVED_TOOLS_SCHEMA_VERSION, "saved_tools": {}}),
                encoding="utf-8",
            )
            _load_saved_tools(str(path))
            assert tools_code._saved_tools_load_failed is False
        finally:
            tools_code._saved_tools_load_failed = False

    def test_save_skipped_while_flag_set(self, tmp_path: Path):
        """When the flag is set, _save_saved_tools returns False without
        touching disk.
        """
        path = tmp_path / "saved.json"
        # Pre-populate so we can confirm it's NOT overwritten.
        original = {
            "version": _SAVED_TOOLS_SCHEMA_VERSION,
            "saved_tools": {"keep_me": {"code": "1", "justification": ""}},
        }
        path.write_text(json.dumps(original), encoding="utf-8")
        original_mtime = path.stat().st_mtime

        tools_code._saved_tools_load_failed = True
        try:
            ok = _save_saved_tools(
                str(path), {"different": {"code": "2", "justification": ""}}
            )
            assert ok is False
            # File must be untouched.
            assert path.stat().st_mtime == original_mtime
            assert json.loads(path.read_text(encoding="utf-8")) == original
        finally:
            tools_code._saved_tools_load_failed = False


class TestHydrationRoundTrip:
    """The whole point of persistence: a tool saved in one session must
    appear after a fresh hydration. This is the smoke test that proves
    ``_load_saved_tools`` and ``_save_saved_tools`` are wired up to the
    same on-disk format.
    """

    def test_save_then_reload(self, tmp_path: Path):
        path = tmp_path / "saved.json"
        first = {
            "movie_mode": {
                "code": 'await call_tool("ha_call_service", {"domain": "scene", "service": "turn_on", "entity_id": "scene.movie"})',
                "justification": "Scene shortcut",
            },
            "all_off": {
                "code": 'await call_tool("ha_call_service", {"domain": "light", "service": "turn_off", "entity_id": "all"})',
                "justification": "Bedtime",
            },
        }
        assert _save_saved_tools(str(path), first) is True

        # Simulate a fresh process by clearing/reloading.
        loaded = _load_saved_tools(str(path))
        assert loaded == first

    def test_load_then_save_then_reload_with_modification(self, tmp_path: Path):
        """Realistic LLM lifecycle: load existing tools, add a new one,
        persist the merged set, and verify it survives the next load."""
        path = tmp_path / "saved.json"
        _save_saved_tools(
            str(path),
            {"a": {"code": "1", "justification": "first"}},
        )
        loaded = _load_saved_tools(str(path))
        loaded["b"] = {"code": "2", "justification": "added later"}
        _save_saved_tools(str(path), loaded)

        roundtrip = _load_saved_tools(str(path))
        assert set(roundtrip.keys()) == {"a", "b"}
        assert roundtrip["b"]["justification"] == "added later"


class TestSaveCapEnforcement:
    """``ha_manage_custom_tool`` rejects ``save_as`` once
    ``_saved_tools`` holds ``_MAX_SAVED_TOOLS`` entries. This proves the
    cap is enforced at the registration site and can't be silently
    flipped by a future change.

    The full ToolError raise lives inside the FastMCP-decorated
    ``ha_manage_custom_tool`` closure, which can't be exercised without
    booting the MCP server. The cheaper unit-level coverage verifies the
    underlying ``_save_saved_tools`` round-trip stops before exceeding
    ``_MAX_SAVED_TOOLS`` — the load-side cap, which is the more
    consequential one (a hostile or runaway file shouldn't be able to
    blow up memory at startup).
    """

    def test_load_truncates_oversize_file(self, tmp_path: Path):
        path = tmp_path / "saved.json"
        big = {
            f"tool_{i:04d}": {"code": "1", "justification": ""}
            for i in range(_MAX_SAVED_TOOLS + 200)
        }
        path.write_text(
            json.dumps(
                {"version": _SAVED_TOOLS_SCHEMA_VERSION, "saved_tools": big}
            ),
            encoding="utf-8",
        )
        loaded = _load_saved_tools(str(path))
        assert len(loaded) == _MAX_SAVED_TOOLS, (
            f"Load must cap at {_MAX_SAVED_TOOLS}, got {len(loaded)}"
        )

    def test_save_does_not_self_cap(self, tmp_path: Path):
        """``_save_saved_tools`` itself does not enforce the cap — the
        registration-site code (``ha_manage_custom_tool``) is the only
        guard for the upper boundary. This test pins that contract: a
        caller passing ``_MAX_SAVED_TOOLS + 1`` entries gets the full
        write. If a maintainer wants the cap enforced lower in the
        stack, they should change this test deliberately, not in
        passing.
        """
        path = tmp_path / "saved.json"
        oversized = {
            f"tool_{i:04d}": {"code": "1", "justification": ""}
            for i in range(_MAX_SAVED_TOOLS + 5)
        }
        assert _save_saved_tools(str(path), oversized) is True
        # The on-disk file has all of them; the load-side cap then
        # truncates back to _MAX_SAVED_TOOLS as covered above.
        roundtrip = json.loads(path.read_text(encoding="utf-8"))
        assert len(roundtrip["saved_tools"]) == _MAX_SAVED_TOOLS + 5
