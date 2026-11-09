"""A storage helper's icon follows the running Core's own field lists."""

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from ha_mcp.tools.config_helpers.icon_route import icon_targets

_FETCH_SCHEMAS = "ha_mcp.tools.config_helpers.icon_route.fetch_helper_schemas"


async def test_each_action_follows_its_own_schema() -> None:
    """A type needs no list of its own, and create and update can differ."""
    schemas = {"new_type": {"create": [], "update": [{"name": "icon"}]}}
    with patch(_FETCH_SCHEMAS, AsyncMock(return_value=schemas)):
        assert await icon_targets(object(), "new_type", "create", "mdi:star") == (
            None,
            "mdi:star",
        )
        assert await icon_targets(object(), "new_type", "update", "mdi:star") == (
            "mdi:star",
            None,
        )


@pytest.mark.parametrize("icon", ["mdi:star", ""])
async def test_an_icon_core_stores_never_becomes_a_registry_override(
    icon: str,
) -> None:
    """The helper and zone dialogs write the icon to the stored item only; an
    override would hide every later change made in those dialogs."""
    schemas = {"new_type": {"update": [{"name": "icon"}]}}
    with patch(_FETCH_SCHEMAS, AsyncMock(return_value=schemas)):
        assert await icon_targets(object(), "new_type", "update", icon) == (icon, None)


@pytest.mark.parametrize(
    "schemas",
    [
        None,
        {},
        {"new_type": {}},
        {"new_type": {"create": [{"name": "name"}, {"name": "picture"}]}},
        {"new_type": {"create": ["icon", {"label": "icon"}]}},
    ],
    ids=["unreadable", "no types", "no action", "other fields", "malformed"],
)
async def test_unknown_fields_send_the_icon_to_the_registry(schemas: Any) -> None:
    with patch(_FETCH_SCHEMAS, AsyncMock(return_value=schemas)):
        assert await icon_targets(object(), "new_type", "create", "mdi:star") == (
            None,
            "mdi:star",
        )


async def test_a_cleared_icon_of_unknown_fields_clears_both() -> None:
    """Clearing only the registry would bring an older stored icon back."""
    with patch(_FETCH_SCHEMAS, AsyncMock(return_value=None)):
        assert await icon_targets(object(), "new_type", "update", "") == ("", "")


async def test_a_zone_icon_stays_in_the_zone_without_the_component() -> None:
    """Without the component no schema can be read, and a registry override
    would leave the Zones editor showing no icon (#3001)."""
    with patch(_FETCH_SCHEMAS, AsyncMock(return_value=None)):
        assert await icon_targets(object(), "zone", "create", "mdi:home") == (
            "mdi:home",
            None,
        )


async def test_no_icon_writes_nothing() -> None:
    fetch = AsyncMock(return_value=None)
    with patch(_FETCH_SCHEMAS, fetch):
        assert await icon_targets(object(), "zone", "update", None) == (None, None)
    fetch.assert_not_awaited()
