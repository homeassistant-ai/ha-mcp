"""Snapshot diffs for the backup manager: a JSON patch for config snapshots
and a unified diff for text (file/YAML) snapshots.

Split out of ``backup_manager``, which imports what it uses from here.
"""

from __future__ import annotations

import difflib
import logging
from typing import Any, Literal, TypedDict

logger = logging.getLogger(__name__)

# Discriminator values for the snapshot ``kind`` marker and the
# DiffResponse/DiffResponseText union. Typed as ``Literal`` so they satisfy
# the discriminated-union fields without widening to ``str``.
_TEXT_KIND: Literal["text"] = "text"
_DICT_KIND: Literal["dict"] = "dict"

# Output cap for the text (file/YAML) diff. Bounded like the manager's ``_MAX_PATCH_OPS``
# so a pathological whole-file rewrite stays token-friendly; the response
# sets ``truncated`` and points the caller at the full snapshot via view.
_MAX_DIFF_LINES = 400


# --------------------------- diff helpers -----------------------------------


class DiffCounts(TypedDict):
    """Per-op-class tallies for a diff patch. ``total`` is the op count;
    ``add + remove + replace`` equals it today (see ``_summarize_patch_counts``)."""

    add: int
    remove: int
    replace: int
    total: int


class DiffResponse(TypedDict):
    """Return shape of ``BackupManager.diff_snapshot``. Both the
    entity-present and ``entity_missing`` branches build this through
    ``_build_diff_response`` so the key set can't drift between them."""

    kind: Literal["dict"]
    backup_name: str
    domain: str
    entity_id: str
    captured_at: str | None
    entity_missing: bool
    patch: list[dict[str, Any]]
    counts: DiffCounts
    unchanged: bool
    truncated: bool


def _build_diff_response(
    name: str,
    domain: str,
    entity_id: str,
    captured_at: str | None,
    *,
    entity_missing: bool,
    patch: list[dict[str, Any]],
    counts: DiffCounts,
    truncated: bool,
) -> DiffResponse:
    """Assemble the diff return payload for either branch.

    ``unchanged`` means "live config matches the snapshot" — only true
    when the entity exists and the patch is empty. Under
    ``entity_missing`` it is forced ``False``: the empty patch is an
    artefact of the absent target, not evidence of a match.
    """
    return {
        "kind": _DICT_KIND,
        "backup_name": name,
        "domain": domain,
        "entity_id": entity_id,
        "captured_at": captured_at,
        "entity_missing": entity_missing,
        "patch": patch,
        "counts": counts,
        "unchanged": not entity_missing and counts["total"] == 0,
        "truncated": truncated,
    }


class DiffResponseText(TypedDict):
    """Return shape of ``diff_snapshot`` for text (file/YAML) snapshots.

    Mirrors ``DiffResponse``'s control keys (``entity_missing`` /
    ``unchanged`` / ``truncated``) so ``ha_manage_backup`` handles both
    kinds uniformly, but carries a unified text ``diff`` instead of a
    JSON-Patch."""

    kind: Literal["text"]
    backup_name: str
    domain: str
    entity_id: str
    captured_at: str | None
    entity_missing: bool
    diff: str
    unchanged: bool
    truncated: bool


def _build_text_diff_response(
    name: str,
    domain: str,
    entity_id: str,
    captured_at: str | None,
    stored: str,
    current: Any,
) -> DiffResponseText:
    """Assemble the unified-text-diff payload for a file/YAML snapshot.

    ``current is None`` means the target (file or YAML key) is gone, so
    there is no live text to diff against — ``entity_missing`` is set and
    the diff is empty (mirrors the dict branch's missing-target handling).
    The diff recovers ``stored`` *from* ``current`` (snapshot is the target
    state), consistent with the JSON-Patch direction. Output is bounded by
    ``_MAX_DIFF_LINES``; overflow sets ``truncated``.
    """
    if current is None:
        return {
            "kind": _TEXT_KIND,
            "backup_name": name,
            "domain": domain,
            "entity_id": entity_id,
            "captured_at": captured_at,
            "entity_missing": True,
            "diff": "",
            "unchanged": False,
            "truncated": False,
        }
    lines = list(
        difflib.unified_diff(
            str(current).splitlines(),
            stored.splitlines(),
            fromfile="current",
            tofile="snapshot",
            lineterm="",
        )
    )
    truncated = len(lines) > _MAX_DIFF_LINES
    if truncated:
        lines = lines[:_MAX_DIFF_LINES]
    return {
        "kind": _TEXT_KIND,
        "backup_name": name,
        "domain": domain,
        "entity_id": entity_id,
        "captured_at": captured_at,
        "entity_missing": False,
        "diff": "\n".join(lines),
        "unchanged": len(lines) == 0,
        "truncated": truncated,
    }


def _compute_json_patch(
    stored: Any, current: Any, max_ops: int, out: list[dict[str, Any]]
) -> bool:
    """Generate an RFC 6902 JSON-Patch from ``current`` to ``stored``.

    The patch is the op sequence a client would apply to ``current`` to
    recover ``stored`` (the captured snapshot is the target state).
    Appends ops to ``out`` in place (capped at ``max_ops`` entries).

    Returns True only when the diff genuinely exceeded ``max_ops``. The
    generator collects one op beyond the cap so an exactly-full patch
    (``len == max_ops``) isn't mistaken for a truncated one; the
    overflow op is trimmed before returning.
    """
    _diff_node(stored, current, "", out, max_ops + 1)
    truncated = len(out) > max_ops
    if truncated:
        del out[max_ops:]
    return truncated


def _diff_node(
    stored: Any,
    current: Any,
    path: str,
    out: list[dict[str, Any]],
    max_ops: int,
) -> None:
    if len(out) >= max_ops:
        return
    # ``type(s) is type(c)`` keeps ``True``/``1`` apart (both compare
    # equal but represent different states for HA toggles); YAML loaders
    # only emit plain dict/list/scalar containers, so subclass surprises
    # aren't in scope.
    if type(stored) is type(current):
        if isinstance(stored, dict):
            assert isinstance(current, dict)
            _diff_dict_node(stored, current, path, out, max_ops)
            return
        if isinstance(stored, list):
            assert isinstance(current, list)
            _diff_list_node(stored, current, path, out, max_ops)
            return
        if stored != current:
            out.append({"op": "replace", "path": path or "", "value": stored})
        return
    # ``True == 1`` / ``False == 0`` in Python, so equality alone would
    # let a bool/int type swap pass silently even though it represents
    # a different state for HA toggles. The different-type branch
    # forces a replace unconditionally. No post-append length guard here
    # (unlike the loop sites in the dict/list walkers below): this append is terminal, and
    # ``_compute_json_patch`` budgets ``max_ops + 1`` precisely to absorb
    # one final overflow op before trimming.
    out.append({"op": "replace", "path": path or "", "value": stored})


def _diff_dict_node(
    stored: dict[Any, Any],
    current: dict[Any, Any],
    path: str,
    out: list[dict[str, Any]],
    max_ops: int,
) -> None:
    """Diff two dicts into JSON-Patch ops (add/remove/recurse per key)."""
    for key, stored_value in stored.items():
        seg = _pointer_segment(str(key))
        sub_path = f"{path}/{seg}"
        if key not in current:
            out.append({"op": "add", "path": sub_path, "value": stored_value})
            if len(out) >= max_ops:
                return
        else:
            _diff_node(stored_value, current[key], sub_path, out, max_ops)
            if len(out) >= max_ops:
                return
    for key in current:
        if key not in stored:
            seg = _pointer_segment(str(key))
            out.append({"op": "remove", "path": f"{path}/{seg}"})
            if len(out) >= max_ops:
                return


def _diff_list_node(
    stored: list[Any],
    current: list[Any],
    path: str,
    out: list[dict[str, Any]],
    max_ops: int,
) -> None:
    """Diff two lists into JSON-Patch ops (positional recurse, then tail add/remove)."""
    min_len = min(len(stored), len(current))
    for i in range(min_len):
        _diff_node(stored[i], current[i], f"{path}/{i}", out, max_ops)
        if len(out) >= max_ops:
            return
    if len(stored) > len(current):
        for value in stored[len(current) :]:
            out.append({"op": "add", "path": f"{path}/-", "value": value})
            if len(out) >= max_ops:
                return
    elif len(current) > len(stored):
        # Remove tail entries from highest to lowest index so
        # successive removes stay valid (RFC 6902 reindexes
        # after each op).
        for i in range(len(current) - 1, len(stored) - 1, -1):
            out.append({"op": "remove", "path": f"{path}/{i}"})
            if len(out) >= max_ops:
                return


def _pointer_segment(key: str) -> str:
    """Escape one JSON-Pointer reference token per RFC 6901 §3.

    Order matters: ``~`` → ``~0`` must run before ``/`` → ``~1``. The
    reverse order would first turn a literal ``/`` into ``~1``, and the
    following ``~`` pass would then corrupt that fresh ``~1`` into
    ``~01``.
    """
    return key.replace("~", "~0").replace("/", "~1")


def _summarize_patch_counts(patch: list[dict[str, Any]]) -> DiffCounts:
    """Tally op classes. ``add + remove + replace == total`` holds today
    because ``_diff_node`` only emits those three ops; if a future change
    starts emitting ``move``/``copy``/``test``, the class counts would sum
    to less than ``total``. Warn on any unrecognized op so that drift is
    visible instead of silently undercounting.
    """
    classes: dict[str, int] = {"add": 0, "remove": 0, "replace": 0}
    for op in patch:
        op_type = op.get("op")
        if isinstance(op_type, str) and op_type in classes:
            classes[op_type] += 1
        else:
            logger.warning(
                "diff: unrecognized JSON-Patch op %r — not reflected in "
                "per-class counts (add/remove/replace)",
                op_type,
            )
    return {
        "add": classes["add"],
        "remove": classes["remove"],
        "replace": classes["replace"],
        "total": len(patch),
    }
