"""The order of a registry row's set-valued lists in a snapshot diff."""

from __future__ import annotations

from ha_mcp.backup_registry_items import in_snapshot_order


def test_a_list_holding_the_same_items_takes_the_snapshots_order() -> None:
    stored = {"area_id": "a1", "aliases": ["galley", "cooking"], "labels": ["x", "y"]}
    current = {"area_id": "a1", "aliases": ["cooking", "galley"], "labels": ["y", "x"]}

    assert in_snapshot_order(current, stored) == stored


def test_a_list_that_differs_keeps_the_live_items() -> None:
    stored = {"aliases": ["galley"], "labels": ["x"]}
    current = {"aliases": ["galley", "cooking"], "labels": []}

    assert in_snapshot_order(current, stored) == current


def test_a_key_the_snapshot_lacks_is_left_alone() -> None:
    current = {"name": "Kitchen", "aliases": ["a", "b"]}

    assert in_snapshot_order(current, {"name": "Kitchen"}) == current
