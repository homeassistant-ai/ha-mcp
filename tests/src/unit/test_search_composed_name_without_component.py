"""ha_search says so when it cannot use the name Home Assistant shows.

Only a current ha_mcp_tools component can ask Core for an entity's composed
name. A search answered without one knows an entity by its id, legacy
friendly_name and aliases and leaves ``name`` out of every record, so an empty
result and a requested ``name`` both have to say why.
"""

import pytest

from ha_mcp.tools.search.entities import _composed_name_notes

from .test_search_fields_projection import _SearchToolFixture


class TestComposedNameWithoutTheComponent(_SearchToolFixture):
    @pytest.mark.asyncio
    async def test_a_requested_composed_name_is_not_dropped_without_a_word(
        self, search_tool
    ):
        data = await search_tool(query="light", result_fields=["entity_id", "name"])

        assert data["entities"]
        assert all("name" not in row for row in data["entities"])
        assert any("composed name" in w for w in data.get("warnings", []))

    @pytest.mark.asyncio
    async def test_an_empty_search_says_the_component_is_missing(self, search_tool):
        data = await search_tool(query="no entity is called this")

        assert data["entity_total_matches"] == 0
        assert any(
            "No ha_mcp_tools component detected" in w for w in data.get("warnings", [])
        )

    @pytest.mark.asyncio
    async def test_a_search_with_results_carries_no_component_hint(self, search_tool):
        data = await search_tool(query="light")

        assert data["entities"]
        assert not any(
            "No ha_mcp_tools component detected" in w for w in data.get("warnings", [])
        )


def test_a_component_that_predates_composed_names_is_told_apart_from_none() -> None:
    """An outdated component is installed: the fix is an update, not an install."""
    notes = _composed_name_notes(
        name_served=False,
        component="outdated",
        name_requested=True,
        empty_query_search=True,
    )

    assert len(notes) == 2
    assert all("update it" in note for note in notes)
    assert not any("No ha_mcp_tools component detected" in note for note in notes)


def test_an_empty_search_with_a_current_component_carries_no_hint() -> None:
    """The legacy path answering beside a current component is not a missing one."""
    assert (
        _composed_name_notes(
            name_served=False,
            component="current",
            name_requested=False,
            empty_query_search=True,
        )
        == []
    )
