"""E2E tests for the card search of ha_config_get_dashboard.

One search covers one dashboard (url_path) or every storage dashboard, and
reaches cards that custom cards nest under keys of their own (issue #2694).
"""

import logging

from ...utilities.assertions import MCPAssertions, safe_call_tool

logger = logging.getLogger(__name__)

URL_PATH = "test-card-search"
ENTITY = "input_number.e2e_card_search_limit"

# A self-made page card's groups[].cards[] (wrapped and bare cards), a
# button-card custom_fields stack, a tabs[].card tab and a plain card.
CONFIG = {
    "views": [
        {
            "title": "Search",
            "cards": [
                {
                    "type": "custom:astra-board-page",
                    "groups": [
                        {
                            "title": "Example",
                            "cards": [
                                {
                                    "card": {"type": "tile", "entity": ENTITY},
                                    "width": 12,
                                },
                                {"type": "tile", "entity": ENTITY},
                            ],
                        }
                    ],
                },
                {
                    "type": "custom:button-card",
                    "custom_fields": {
                        "content": {
                            "card": {
                                "type": "vertical-stack",
                                "cards": [{"type": "tile", "entity": ENTITY}],
                            }
                        }
                    },
                },
                {
                    "type": "custom:tabbed-card",
                    "tabs": [{"card": {"type": "tile", "entity": ENTITY}}],
                },
                {"type": "tile", "entity": ENTITY},
            ],
        }
    ]
}
EXPECTED_PATHS = [
    ".views[0].cards[0].groups[0].cards[0].card",
    ".views[0].cards[0].groups[0].cards[1]",
    ".views[0].cards[1].custom_fields.content.card.cards[0]",
    ".views[0].cards[2].tabs[0].card",
    ".views[0].cards[3]",
]


class TestCardSearch:
    async def test_search_reaches_cards_nested_in_custom_cards(self, mcp_client):
        mcp = MCPAssertions(mcp_client)
        await mcp.call_tool_success(
            "ha_config_set_dashboard",
            {"url_path": URL_PATH, "title": "Card Search Test", "config": CONFIG},
        )
        try:
            everywhere = await mcp.call_tool_success(
                "ha_config_get_dashboard", {"query": ENTITY}
            )
            ours = [m for m in everywhere["matches"] if m["url_path"] == URL_PATH]
            assert [m["jq_path"] for m in ours] == EXPECTED_PATHS
            assert all(
                m["matched"] == [{"field": "entity", "value": ENTITY}] for m in ours
            )

            scoped = await mcp.call_tool_success(
                "ha_config_get_dashboard",
                {"url_path": URL_PATH, "entity_id": ENTITY, "card_type": "tile"},
            )
            assert [m["jq_path"] for m in scoped["matches"]] == EXPECTED_PATHS
            assert scoped["config_hash"] == ours[0]["config_hash"]
            assert scoped["matches"][0]["config_hash"] == ours[0]["config_hash"]

            # A match from the search across dashboards is directly editable.
            nested = ours[0]
            await mcp.call_tool_success(
                "ha_config_set_dashboard",
                {
                    "url_path": URL_PATH,
                    "config_hash": nested["config_hash"],
                    "python_transform": (
                        f"config{nested['python_path']}['name'] = 'Edited'"
                    ),
                },
            )
            edited = await mcp.call_tool_success(
                "ha_config_get_dashboard", {"url_path": URL_PATH, "query": "edited"}
            )
            assert [m["jq_path"] for m in edited["matches"]] == [EXPECTED_PATHS[0]]
        finally:
            await safe_call_tool(
                mcp_client, "ha_config_delete_dashboard", {"url_path": URL_PATH}
            )
