"""Card fields and card checks come from Home Assistant's own frontend (#2632).

The component reads each card editor's form and config struct from the
frontend files Home Assistant serves. Without the component there is no source
for either: describe reports that, and writes carry no card warnings.
"""

from uuid import uuid4

import pytest

from ...utilities.assertions import MCPAssertions, safe_call_tool
from ...utilities.topology import component_surface_available


@pytest.mark.asyncio
async def test_card_fields_come_from_the_card_editor(mcp_client):
    mcp = MCPAssertions(mcp_client)
    if not component_surface_available():
        await mcp.call_tool_failure(
            "ha_config_get_dashboard",
            {"card_type": "tile", "describe": True},
            expected_error="COMPONENT_NOT_INSTALLED",
        )
        return

    tile = await mcp.call_tool_success(
        "ha_config_get_dashboard", {"card_type": "tile", "describe": True}
    )
    fields = {f["name"]: f for f in tile["fields"]}
    assert fields["entity"]["type"] == "entity"
    content = {f["name"]: f for f in fields["content"]["fields"]}
    assert content["color"]["description"], content["color"]
    assert tile["description"]

    listed = await mcp.call_tool_success("ha_config_get_dashboard", {"describe": True})
    assert {"tile", "grid", "heading"} <= {c["type"] for c in listed["card_types"]}

    await mcp.call_tool_failure(
        "ha_config_get_dashboard",
        {"card_type": "no-such-card", "describe": True},
        expected_error="tile",
    )


@pytest.mark.asyncio
async def test_saved_card_problems_come_back_as_warnings(mcp_client):
    """A write still succeeds; the warnings name each card the frontend rejects."""
    mcp = MCPAssertions(mcp_client)
    path = "card-checks-" + uuid4().hex[:8]
    cards = [
        {"type": "tile", "entity": "light.bed_light"},
        {"type": "tile", "entity": "light.bed_light", "colour": "red"},
        {"type": "no-such-card"},
        {"type": "vertical-stack", "cards": [{"type": "button", "entitty": "x"}]},
    ]
    try:
        result = await mcp.call_tool_success(
            "ha_config_set_dashboard",
            {
                "url_path": path,
                "config": {"views": [{"title": "Checks", "cards": cards}]},
                "MandatoryBPS": False,
            },
        )
        warnings = "\n".join(result.get("warnings", []))
        if not component_surface_available():
            assert "card option" not in warnings, warnings
            return
        assert "views[0].cards[0]" not in warnings, warnings
        assert "'colour' is not a tile card option; did you mean 'color'?" in warnings
        assert "views[0].cards[2]: unknown card type 'no-such-card'" in warnings
        assert "views[0].cards[3].cards[0] (button): 'entitty'" in warnings
    finally:
        await safe_call_tool(
            mcp_client, "ha_config_delete_dashboard", {"url_path": path}
        )


@pytest.mark.asyncio
async def test_custom_cards_are_checked_and_described_from_their_resource(mcp_client):
    """A card from a dashboard resource answers for itself, like a HACS card."""
    mcp = MCPAssertions(mcp_client)
    if not component_surface_available():
        await mcp.call_tool_failure(
            "ha_config_get_dashboard",
            {"card_type": "custom:e2e-custom-card", "describe": True},
            expected_error="COMPONENT_NOT_INSTALLED",
        )
        return
    path = "custom-card-checks-" + uuid4().hex[:8]
    resource = await mcp.call_tool_success(
        "ha_config_set_dashboard_resource",
        {"url": "/local/e2e-custom-card.js", "resource_type": "module"},
    )
    try:
        listed = await mcp.call_tool_success(
            "ha_config_get_dashboard", {"describe": True}
        )
        assert "custom:e2e-custom-card" in {c["type"] for c in listed["card_types"]}
        card = await mcp.call_tool_success(
            "ha_config_get_dashboard",
            {"card_type": "custom:e2e-custom-card", "describe": True},
        )
        assert card["fields"] == [
            {"name": "entity", "required": True, "type": "entity"}
        ]

        cards = [
            {"type": "custom:e2e-custom-card"},
            {"type": "custom:e2e-custom-card", "entity": "light.bed_light"},
        ]
        result = await mcp.call_tool_success(
            "ha_config_set_dashboard",
            {
                "url_path": path,
                "config": {"views": [{"title": "Custom", "cards": cards}]},
                "MandatoryBPS": False,
            },
        )
        warnings = "\n".join(result.get("warnings", []))
        assert (
            "views[0].cards[0] (custom:e2e-custom-card): "
            "e2e-custom-card needs an entity" in warnings
        ), warnings
        assert "views[0].cards[1]" not in warnings, warnings
    finally:
        await safe_call_tool(
            mcp_client, "ha_config_delete_dashboard", {"url_path": path}
        )
        await safe_call_tool(
            mcp_client,
            "ha_config_delete_dashboard_resource",
            {"resource_id": resource["resource_id"]},
        )
