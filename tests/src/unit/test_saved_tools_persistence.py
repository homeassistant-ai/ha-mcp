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
