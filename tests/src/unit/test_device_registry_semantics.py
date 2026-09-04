"""Tests for Core 2026.9 child-device registry semantics."""

from __future__ import annotations

from typing import Any

import pytest

from ha_mcp.utils.device_registry_semantics import (
    EFFECTIVE_AREA_MARKER,
    annotate_device_rows_with_effective_area,
    build_device_registry_snapshot,
    effective_device_area_id,
)


def _main(device_id: str, *, area_id: Any = None, **extra: Any) -> dict[str, Any]:
    return {"id": device_id, "area_id": area_id, **extra}


def _child(
    device_id: str,
    parent_device_id: Any,
    *,
    area_id: Any = None,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "id": device_id,
        "parent_device_id": parent_device_id,
        "area_id": area_id,
        **extra,
    }


def test_core_2026_8_ordinary_rows_remain_unchanged() -> None:
    rows = [_main("device-b", area_id=None), _main("device-a", area_id="office")]

    snapshot = build_device_registry_snapshot(rows)

    assert snapshot.rows == tuple(rows)
    assert snapshot.parent_by_id == {"device-b": None, "device-a": None}
    assert snapshot.effective_area_by_id == {
        "device-b": None,
        "device-a": "office",
    }


def test_child_inherits_parent_area_and_direct_area_takes_precedence() -> None:
    rows = [
        _main("parent", area_id="office"),
        _child("inherited", "parent"),
        _child("direct", "parent", area_id="garage"),
    ]

    snapshot = build_device_registry_snapshot(rows)

    assert snapshot.parent_by_id == {
        "parent": None,
        "inherited": "parent",
        "direct": "parent",
    }
    assert snapshot.effective_area_by_id == {
        "parent": "office",
        "inherited": "office",
        "direct": "garage",
    }


@pytest.mark.parametrize(
    "rows",
    [
        [_child("child", "missing")],
        [_main("parent", area_id=7), _child("child", "parent")],
        [_main("parent", area_id="office"), _child("child", 7)],
        [_child("parent", "child"), _child("child", "parent")],
        [
            _main("root", area_id="office"),
            _child("parent", "root"),
            _child("child", "parent"),
        ],
    ],
    ids=[
        "missing-parent",
        "malformed-parent-area",
        "malformed-parent-id",
        "cycle",
        "excessive-depth",
    ],
)
def test_invalid_parent_relationships_do_not_invent_an_area(
    rows: list[dict[str, Any]],
) -> None:
    snapshot = build_device_registry_snapshot(rows)

    assert snapshot.effective_area_by_id["child"] is None


def test_identical_duplicates_collapse_without_reordering() -> None:
    first = _main("first", area_id="office", labels=["alpha"])
    duplicate = dict(first)
    last = _child("last", "first")

    snapshot = build_device_registry_snapshot([first, duplicate, last])

    assert [row["id"] for row in snapshot.rows] == ["first", "last"]
    assert snapshot.effective_area_by_id["last"] == "office"


def test_conflicting_identity_is_removed_with_dependent_enrichment() -> None:
    snapshot = build_device_registry_snapshot(
        [
            _main("parent", area_id="office"),
            _main("parent", area_id="garage"),
            _child("child", "parent"),
            _main("ordinary", area_id="kitchen"),
        ]
    )

    assert [row["id"] for row in snapshot.rows] == ["child", "ordinary"]
    assert snapshot.effective_area_by_id == {
        "child": None,
        "ordinary": "kitchen",
    }


def test_malformed_rows_and_identities_are_ignored() -> None:
    snapshot = build_device_registry_snapshot(
        [None, "bad", {}, {"id": 5}, {"id": ""}, _main("valid", area_id="lab")]
    )

    assert snapshot.rows == (_main("valid", area_id="lab"),)
    assert snapshot.effective_area_by_id == {"valid": "lab"}


def test_annotation_is_private_and_does_not_mutate_input() -> None:
    rows = [_main("parent", area_id="office"), _child("child", "parent")]

    annotated = annotate_device_rows_with_effective_area(rows)

    assert EFFECTIVE_AREA_MARKER not in rows[0]
    assert EFFECTIVE_AREA_MARKER not in rows[1]
    assert annotated[0][EFFECTIVE_AREA_MARKER] == "office"
    assert annotated[1][EFFECTIVE_AREA_MARKER] == "office"


def test_effective_area_helper_never_recurses_through_a_child_parent() -> None:
    root = _main("root", area_id="office")
    parent = _child("parent", "root")
    child = _child("child", "parent")
    devices = {row["id"]: row for row in (root, parent, child)}

    assert effective_device_area_id(parent, devices) == "office"
    assert effective_device_area_id(child, devices) is None
