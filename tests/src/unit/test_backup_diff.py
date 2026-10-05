"""Snapshot diffs: the JSON patch for config snapshots and the unified diff
for text snapshots (``ha_mcp.backup_diff``)."""

from typing import Any

import pytest

from ha_mcp.backup_diff import (
    _MAX_DIFF_LINES,
    _build_text_diff_response,
    _compute_json_patch,
    _pointer_segment,
    _summarize_patch_counts,
)


class TestPointerSegment:
    """RFC 6901 §3 escape order: ``~`` first, then ``/``."""

    def test_plain_segment_passes_through(self) -> None:
        assert _pointer_segment("alias") == "alias"

    def test_slash_escapes_to_tilde_one(self) -> None:
        assert _pointer_segment("a/b") == "a~1b"

    def test_tilde_escapes_to_tilde_zero(self) -> None:
        assert _pointer_segment("a~b") == "a~0b"

    def test_tilde_one_in_input_round_trips(self) -> None:
        # Forward order escapes ``~`` → ``~0`` first, so the literal
        # ``~1`` becomes ``~01``. The reverse order is the real hazard:
        # a literal ``/`` escaped to ``~1`` would then be corrupted to
        # ``~01`` by the ``~`` pass (see ``_pointer_segment``).
        assert _pointer_segment("~1") == "~01"


class TestDiffNode:
    """``_compute_json_patch`` patch shape against representative configs."""

    def test_identical_configs_produce_empty_patch(self) -> None:
        out: list[dict[str, Any]] = []
        truncated = _compute_json_patch({"a": 1}, {"a": 1}, 50, out)
        assert out == []
        assert truncated is False

    def test_scalar_change_is_replace(self) -> None:
        out: list[dict[str, Any]] = []
        _compute_json_patch({"a": 2}, {"a": 1}, 50, out)
        assert out == [{"op": "replace", "path": "/a", "value": 2}]

    def test_missing_key_in_current_is_add(self) -> None:
        out: list[dict[str, Any]] = []
        _compute_json_patch({"a": 1, "b": 2}, {"a": 1}, 50, out)
        assert out == [{"op": "add", "path": "/b", "value": 2}]

    def test_extra_key_in_current_is_remove(self) -> None:
        out: list[dict[str, Any]] = []
        _compute_json_patch({"a": 1}, {"a": 1, "b": 2}, 50, out)
        assert out == [{"op": "remove", "path": "/b"}]

    def test_bool_vs_int_does_not_collapse(self) -> None:
        # ``True == 1`` in Python but they represent different states for
        # HA-side toggles; the diff must treat the type change as a
        # replace, not silently equate them.
        out: list[dict[str, Any]] = []
        _compute_json_patch({"enabled": True}, {"enabled": 1}, 50, out)
        assert out == [{"op": "replace", "path": "/enabled", "value": True}]

    def test_list_length_grew_in_current_emits_remove_in_reverse(self) -> None:
        out: list[dict[str, Any]] = []
        _compute_json_patch([1, 2], [1, 2, 3, 4], 50, out)
        # Removes in reverse index order so each op stays valid against
        # the document state it gets applied to.
        assert out == [
            {"op": "remove", "path": "/3"},
            {"op": "remove", "path": "/2"},
        ]

    def test_list_length_shrunk_in_current_emits_append(self) -> None:
        out: list[dict[str, Any]] = []
        _compute_json_patch([1, 2, 3], [1, 2], 50, out)
        assert out == [{"op": "add", "path": "/-", "value": 3}]

    def test_nested_change_uses_pointer_path(self) -> None:
        stored = {"trigger": [{"platform": "state", "entity_id": "binary_sensor.x"}]}
        current = {"trigger": [{"platform": "time", "entity_id": "binary_sensor.x"}]}
        out: list[dict[str, Any]] = []
        _compute_json_patch(stored, current, 50, out)
        assert out == [
            {"op": "replace", "path": "/trigger/0/platform", "value": "state"}
        ]

    def test_key_with_slash_gets_escaped(self) -> None:
        out: list[dict[str, Any]] = []
        _compute_json_patch({"a/b": 2}, {"a/b": 1}, 50, out)
        assert out == [{"op": "replace", "path": "/a~1b", "value": 2}]

    def test_root_type_swap_emits_root_replace(self) -> None:
        # When the captured shape is a dict but HA now returns a list
        # (schema migration, integration replaced), the only sane patch
        # is "replace whole doc" — JSON-Pointer ``""`` per RFC 6901 §6.
        out: list[dict[str, Any]] = []
        _compute_json_patch({"a": 1}, [1, 2], 50, out)
        assert out == [{"op": "replace", "path": "", "value": {"a": 1}}]

    def test_truncation_flag_set_when_max_ops_reached(self) -> None:
        # Build a config that produces > max_ops change ops.
        stored = {f"k{i}": i for i in range(10)}
        current: dict[str, Any] = {}
        out: list[dict[str, Any]] = []
        truncated = _compute_json_patch(stored, current, 3, out)
        assert truncated is True
        assert len(out) == 3
        # All three are adds since current is empty.
        assert {op["op"] for op in out} == {"add"}

    def test_patch_exactly_at_cap_is_not_truncated(self) -> None:
        # A patch that lands on exactly ``max_ops`` ops is complete, not
        # cut short — ``len(out) >= max_ops`` would have flagged it as a
        # false-positive truncation. The generator collects one beyond
        # the cap to tell "exactly full" from "overflowed".
        stored = {f"k{i}": i for i in range(3)}
        current: dict[str, Any] = {}
        out: list[dict[str, Any]] = []
        truncated = _compute_json_patch(stored, current, 3, out)
        assert truncated is False
        assert len(out) == 3
        assert {op["op"] for op in out} == {"add"}

    def test_nested_list_of_dicts_grows_emits_append(self) -> None:
        # A real trigger add: the stored automation has two triggers, the
        # live one only has the first — recovering ``stored`` means
        # appending the second trigger.
        stored = {
            "trigger": [
                {"platform": "state", "entity_id": "binary_sensor.a"},
                {"platform": "time", "at": "12:00:00"},
            ]
        }
        current = {"trigger": [{"platform": "state", "entity_id": "binary_sensor.a"}]}
        out: list[dict[str, Any]] = []
        _compute_json_patch(stored, current, 50, out)
        assert out == [
            {
                "op": "add",
                "path": "/trigger/-",
                "value": {"platform": "time", "at": "12:00:00"},
            }
        ]

    def test_nested_list_of_dicts_shrinks_emits_reverse_remove(self) -> None:
        # A real action removal: the stored script has one action, the
        # live one grew a second — recovering ``stored`` means removing
        # the extra tail entry.
        stored = {"action": [{"service": "light.turn_on"}]}
        current = {
            "action": [
                {"service": "light.turn_on"},
                {"service": "light.turn_off"},
            ]
        }
        out: list[dict[str, Any]] = []
        _compute_json_patch(stored, current, 50, out)
        assert out == [{"op": "remove", "path": "/action/1"}]

    def test_tilde_in_key_escapes_end_to_end(self) -> None:
        # ``~`` in a dict key must be escaped to ``~0`` in the pointer
        # path — exercised through ``_compute_json_patch`` (not just
        # ``_pointer_segment``) to cover the full diff path.
        out: list[dict[str, Any]] = []
        _compute_json_patch({"a~b": 2}, {"a~b": 1}, 50, out)
        assert out == [{"op": "replace", "path": "/a~0b", "value": 2}]


class TestSummarizePatchCounts:
    def test_counts_each_op_class_and_total(self) -> None:
        patch = [
            {"op": "add", "path": "/x", "value": 1},
            {"op": "remove", "path": "/y"},
            {"op": "replace", "path": "/z", "value": 2},
            {"op": "add", "path": "/q", "value": 3},
        ]
        assert _summarize_patch_counts(patch) == {
            "add": 2,
            "remove": 1,
            "replace": 1,
            "total": 4,
        }

    def test_empty_patch_zero_counts(self) -> None:
        assert _summarize_patch_counts([]) == {
            "add": 0,
            "remove": 0,
            "replace": 0,
            "total": 0,
        }

    def test_unrecognized_op_warns_and_undercounts_classes(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # ``_diff_node`` only emits add/remove/replace today; if a future
        # op type (move/copy/test) ever appears, the class counts sum to
        # less than ``total``. The warning makes that drift visible.
        patch = [
            {"op": "add", "path": "/x", "value": 1},
            {"op": "move", "from": "/a", "path": "/b"},
        ]
        with caplog.at_level("WARNING"):
            counts = _summarize_patch_counts(patch)
        assert counts == {"add": 1, "remove": 0, "replace": 0, "total": 2}
        assert counts["add"] + counts["remove"] + counts["replace"] < counts["total"]
        assert any(
            "unrecognized JSON-Patch op" in r.message and "move" in r.message
            for r in caplog.records
        )


class TestBuildTextDiffResponse:
    def test_changed_emits_unified_diff(self) -> None:
        r = _build_text_diff_response(
            "snap", "file", "www/x.css", "t0", "a\nb\nc\n", "a\nB\nc\n"
        )
        assert r["kind"] == "text"
        assert r["entity_missing"] is False
        assert r["unchanged"] is False
        assert r["truncated"] is False
        assert "-B" in r["diff"] and "+b" in r["diff"]

    def test_unchanged_when_identical(self) -> None:
        r = _build_text_diff_response("snap", "file", "p", "t0", "same\n", "same\n")
        assert r["unchanged"] is True
        assert r["diff"] == ""

    def test_missing_target(self) -> None:
        r = _build_text_diff_response("snap", "file", "p", "t0", "x\n", None)
        assert r["entity_missing"] is True
        assert r["unchanged"] is False
        assert r["diff"] == ""

    def test_truncates_at_cap(self) -> None:
        stored = "".join(f"s{i}\n" for i in range(_MAX_DIFF_LINES + 50))
        r = _build_text_diff_response("snap", "file", "p", "t0", stored, "current\n")
        assert r["truncated"] is True
        assert len(r["diff"].splitlines()) == _MAX_DIFF_LINES
