"""Storage-helper payloads that Home Assistant validates (#2632).

Home Assistant validates each storage-helper create and update itself, so the
tool sends the caller's fields under Core's names. These tests cover what the
tool still adds: the merge with the stored item that Core's whole-item update
needs, the create defaults the tool documents, the zone icon rule (#2643), and
the three unusable configurations Core accepts.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools.config_helpers.core_payload import (
    check_core_gaps,
    merged_update,
    with_create_defaults,
)
from ha_mcp.tools.config_helpers.schemas import SIMPLE_HELPER_SCHEMAS


def _error(te: pytest.ExceptionInfo[ToolError]) -> dict[str, Any]:
    return json.loads(str(te.value))  # type: ignore[no-any-return]


# --- merged_update: Core's update writes the whole item ---------------------


def test_update_keeps_every_stored_field_it_does_not_change() -> None:
    stored = {"id": "t", "name": "T", "min": 0.0, "max": 50.0, "mode": "box"}
    body = merged_update("input_number", stored, None, None, {"max": 80.0}, "x")
    assert body == {"name": "T", "min": 0.0, "max": 80.0, "mode": "box"}


def test_update_does_not_send_the_items_id() -> None:
    """Core's update schemas reject ``id``; the item is addressed separately."""
    body = merged_update("input_boolean", {"id": "b", "name": "B"}, "C", None, {}, "x")
    assert "id" not in body


def test_cleared_icon_is_left_out_of_the_item() -> None:
    """Core rejects an empty icon, so clearing removes the key instead."""
    stored = {"id": "b", "name": "B", "icon": "mdi:star"}
    kept = merged_update("input_boolean", stored, None, None, {}, "input_boolean.b")
    cleared = merged_update("input_boolean", stored, None, "", {}, "input_boolean.b")
    assert kept["icon"] == "mdi:star"
    assert "icon" not in cleared


# --- zone icon (#2643) -------------------------------------------------------

_ZONE = {"id": "z", "name": "Z", "latitude": 1.0, "longitude": 2.0, "radius": 100}


def test_new_zone_icon_reaches_an_item_that_stores_one() -> None:
    """The zone shows the item's icon under the registry's; a changed registry
    icon alone would be hidden again once the registry icon is cleared."""
    stored = {**_ZONE, "icon": "mdi:home"}
    body = merged_update("zone", stored, None, "mdi:work", {}, "zone.z")
    assert body["icon"] == "mdi:work"


def test_clearing_a_stored_zone_icon_is_refused() -> None:
    """Core's zone update merges and rejects an empty icon, so the stored icon
    could never be removed; refusing beats reporting a cleared icon that
    shows again."""
    stored = {**_ZONE, "icon": "mdi:home"}
    with pytest.raises(ToolError) as exc_info:
        merged_update("zone", stored, None, "", {}, "zone.z")
    body = _error(exc_info)
    assert "mdi:home" in body["error"]["message"]
    assert body["helper_type"] == "zone"


def test_zone_without_a_stored_icon_keeps_it_registry_only() -> None:
    """With no item icon the registry carries the icon, and stays clearable."""
    for icon in ("mdi:work", ""):
        body = merged_update("zone", _ZONE, None, icon, {}, "zone.z")
        assert "icon" not in body


def test_zone_update_without_icon_keeps_the_stored_icon() -> None:
    stored = {**_ZONE, "icon": "mdi:home"}
    body = merged_update("zone", stored, "Office", None, {}, "zone.z")
    assert body["icon"] == "mdi:home"


# --- create defaults ---------------------------------------------------------


def test_input_datetime_without_components_gets_date_and_time() -> None:
    """Core rejects an input_datetime with neither; the tool defaults to both."""
    assert with_create_defaults("input_datetime", {}) == {
        "has_date": True,
        "has_time": True,
    }
    only_date = with_create_defaults("input_datetime", {"has_date": True})
    assert only_date == {"has_date": True}


def test_tag_create_gets_a_tag_id_unless_given() -> None:
    """Core's tag create requires tag_id; the tool documents it as optional."""
    generated = with_create_defaults("tag", {})["tag_id"]
    assert generated and generated != with_create_defaults("tag", {})["tag_id"]
    assert with_create_defaults("tag", {"tag_id": "abc"})["tag_id"] == "abc"


# --- check_core_gaps: configurations Core stores but cannot use --------------


@pytest.mark.parametrize(
    ("helper_type", "body"),
    [
        ("counter", {"minimum": 5, "maximum": 5}),
        ("counter", {"step": 0}),
        ("counter", {"minimum": 0, "maximum": 3, "step": 5}),
        ("input_number", {"min": 0, "max": 1, "step": 5}),
    ],
    ids=[
        "counter-empty-range",
        "counter-zero-step",
        "counter-step-over-range",
        "number-step-over-range",
    ],
)
def test_unusable_range_is_rejected_with_the_helpers_schema(
    helper_type: str, body: dict[str, Any]
) -> None:
    with pytest.raises(ToolError) as exc_info:
        check_core_gaps(helper_type, body)
    error = _error(exc_info)
    assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert error["data_schema"] == SIMPLE_HELPER_SCHEMAS[helper_type]


@pytest.mark.parametrize(
    ("helper_type", "body"),
    [
        ("counter", {"minimum": 0, "maximum": 10, "step": 1}),
        ("counter", {"minimum": None, "maximum": None, "step": 3}),
        ("input_number", {"min": 0, "max": 1, "step": 0.1}),
        # Core enforces min < max for input_number itself.
        ("input_number", {"min": 5, "max": 5, "step": 1}),
        ("input_text", {"min": 4, "max": 4}),
    ],
)
def test_usable_or_core_checked_ranges_pass(
    helper_type: str, body: dict[str, Any]
) -> None:
    check_core_gaps(helper_type, body)
