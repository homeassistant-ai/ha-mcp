"""Regression tests for deterministic structural bulk selection."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.tools import bulk_selector
from ha_mcp.tools.bulk_selector import (
    BulkSelectorValidationError,
    resolve_bulk_selector,
)


class SelectorClient:
    """Minimal HA client exposing one consistent topology snapshot."""

    def __init__(
        self,
        *,
        states: list[dict[str, Any]],
        entities: list[dict[str, Any]],
        devices: list[dict[str, Any]] | None = None,
        areas: list[dict[str, Any]] | None = None,
        floors: list[dict[str, Any]] | None = None,
    ) -> None:
        self.states = states
        self.registries = {
            "config/entity_registry/list": entities,
            "config/device_registry/list": devices or [],
            "config/area_registry/list": areas
            or [{"area_id": "salon", "name": "Salon", "floor_id": None}],
            "config/floor_registry/list": floors or [],
        }

    async def get_states(self) -> list[dict[str, Any]]:
        return self.states

    async def send_websocket_message(self, message: dict[str, Any]) -> dict[str, Any]:
        return {"success": True, "result": self.registries[message["type"]]}


def _state(entity_id: str, members: list[str] | None = None) -> dict[str, Any]:
    attributes: dict[str, Any] = {"friendly_name": entity_id}
    if members is not None:
        attributes["entity_id"] = members
    return {"entity_id": entity_id, "state": "on", "attributes": attributes}


@pytest.fixture(autouse=True)
def _visible_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        bulk_selector, "load_hidden_set", AsyncMock(return_value=(set(), []))
    )


@pytest.mark.asyncio
async def test_excluded_leaf_inside_nested_aggregate_is_never_an_operation() -> None:
    """Exclusion subtraction happens after recursive membership expansion."""
    client = SelectorClient(
        states=[
            _state("light.salon_group", ["light.nested", "light.table"]),
            _state("light.nested", ["light.vitrine", "light.sofa"]),
            _state("light.vitrine"),
            _state("light.sofa"),
            _state("light.table"),
        ],
        entities=[
            {"entity_id": "light.salon_group", "area_id": "salon"},
            {"entity_id": "light.nested", "area_id": None},
            {"entity_id": "light.vitrine", "area_id": None},
            {"entity_id": "light.sofa", "area_id": None},
            {"entity_id": "light.table", "area_id": None},
        ],
    )

    result = await resolve_bulk_selector(
        client,
        {
            "domain": "light",
            "area_ids": ["salon"],
            "exclude_entity_ids": ["light.vitrine"],
        },
        action="off",
        parameters=None,
        timeout_seconds=None,
        validate_first=True,
    )

    assert result.resolved_entity_ids == ["light.sofa", "light.table"]
    assert result.excluded_entity_ids == ["light.vitrine"]
    assert {row["entity_id"] for row in result.operations} == {
        "light.sofa",
        "light.table",
    }


@pytest.mark.asyncio
async def test_floor_and_device_area_inheritance_resolve_exactly() -> None:
    """Floor expansion includes entities inheriting an area from their device."""
    client = SelectorClient(
        states=[_state("light.inherited"), _state("light.other")],
        entities=[
            {"entity_id": "light.inherited", "area_id": None, "device_id": "dev1"},
            {"entity_id": "light.other", "area_id": "other", "device_id": None},
        ],
        devices=[{"id": "dev1", "area_id": "salon"}],
        areas=[
            {"area_id": "salon", "floor_id": "ground"},
            {"area_id": "other", "floor_id": "upper"},
        ],
        floors=[{"floor_id": "ground"}, {"floor_id": "upper"}],
    )

    result = await resolve_bulk_selector(
        client,
        {"domain": "light", "floor_ids": ["ground"]},
        action="off",
        parameters=None,
        timeout_seconds=5,
        validate_first=True,
    )

    assert result.selected_area_ids == ["salon"]
    assert result.operations == [
        {
            "entity_id": "light.inherited",
            "action": "off",
            "validate_first": True,
            "timeout_seconds": 5,
        }
    ]


@pytest.mark.asyncio
async def test_membership_cycle_fails_before_returning_operations() -> None:
    """A cyclic aggregate graph is an all-or-nothing preflight failure."""
    client = SelectorClient(
        states=[
            _state("light.one", ["light.two"]),
            _state("light.two", ["light.one"]),
        ],
        entities=[
            {"entity_id": "light.one", "area_id": "salon"},
            {"entity_id": "light.two", "area_id": None},
        ],
    )

    with pytest.raises(BulkSelectorValidationError, match="cycle"):
        await resolve_bulk_selector(
            client,
            {"domain": "light", "area_ids": ["salon"]},
            action="off",
            parameters=None,
            timeout_seconds=None,
            validate_first=True,
        )


@pytest.mark.asyncio
async def test_visibility_filters_positive_leaf_but_keeps_exclusion_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hidden leaves affect safety without leaking their IDs in the report."""
    monkeypatch.setattr(
        bulk_selector,
        "load_hidden_set",
        AsyncMock(return_value=({"light.hidden", "light.secret"}, [])),
    )
    client = SelectorClient(
        states=[
            _state("light.group", ["light.visible", "light.hidden", "light.secret"]),
            _state("light.visible"),
            _state("light.hidden"),
            _state("light.secret"),
        ],
        entities=[
            {"entity_id": "light.group", "area_id": "salon"},
            {"entity_id": "light.visible", "area_id": None},
            {"entity_id": "light.hidden", "area_id": None},
            {"entity_id": "light.secret", "area_id": None},
        ],
    )

    result = await resolve_bulk_selector(
        client,
        {
            "domain": "light",
            "area_ids": ["salon"],
            "exclude_entity_ids": ["light.secret"],
        },
        action="off",
        parameters=None,
        timeout_seconds=None,
        validate_first=True,
    )

    assert result.resolved_entity_ids == ["light.visible"]
    assert result.excluded_entity_ids == []
    assert result.hidden_entity_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("selector", "message"),
    [
        ({"domain": "light", "area_ids": ["unknown"]}, "unknown area_ids"),
        ({"domain": "light", "floor_ids": ["unknown"]}, "unknown floor_ids"),
        ({"domain": "light"}, "at least one"),
    ],
)
async def test_invalid_structural_ids_fail_closed(
    selector: dict[str, Any], message: str
) -> None:
    """Invalid exact topology never degrades to a broader match."""
    client = SelectorClient(states=[_state("light.one")], entities=[])

    with pytest.raises(BulkSelectorValidationError, match=message):
        await resolve_bulk_selector(
            client,
            selector,
            action="off",
            parameters=None,
            timeout_seconds=None,
            validate_first=True,
        )
