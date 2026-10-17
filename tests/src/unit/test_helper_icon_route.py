"""A storage helper's icon follows the running Core's own field lists."""

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from ha_mcp.tools.config_helpers.icon_route import collection_icon

_FETCH_SCHEMAS = "ha_mcp.tools.config_helpers.icon_route.fetch_helper_schemas"


async def test_each_action_follows_its_own_schema() -> None:
    """A type needs no list of its own, and create and update can differ."""
    schemas = {"new_type": {"create": [], "update": [{"name": "icon"}]}}
    with patch(_FETCH_SCHEMAS, AsyncMock(return_value=schemas)):
        assert await collection_icon(object(), "new_type", "create", "mdi:star") is None
        assert (
            await collection_icon(object(), "new_type", "update", "mdi:star")
            == "mdi:star"
        )


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
        assert await collection_icon(object(), "new_type", "create", "mdi:star") is None


async def test_a_cleared_icon_also_clears_the_stored_item_without_discovery() -> None:
    fetch = AsyncMock(return_value=None)
    with patch(_FETCH_SCHEMAS, fetch):
        assert await collection_icon(object(), "new_type", "update", "") == ""
    fetch.assert_not_awaited()


async def test_a_zone_icon_is_left_to_the_zone_rule() -> None:
    """merged_update decides whether a zone's icon may go to the stored item."""
    fetch = AsyncMock(return_value={"zone": {"update": []}})
    with patch(_FETCH_SCHEMAS, fetch):
        assert (
            await collection_icon(object(), "zone", "update", "mdi:home") == "mdi:home"
        )
    fetch.assert_not_awaited()
