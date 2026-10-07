"""Card fields and card checks come from Home Assistant's own frontend (#2632).

The component reads each card editor's form and config struct from the
frontend files Home Assistant serves. Without the component there is no source
for either: describe reports that, and writes carry no card warnings.
"""

from uuid import uuid4

import pytest

from ...utilities.assertions import MCPAssertions, safe_call_tool
from ...utilities.topology import component_surface_available
from ...utilities.wait_helpers import wait_for_tool_result


@pytest.mark.asyncio
async def test_card_fields_come_from_the_card_editor(mcp_client):
    mcp = MCPAssertions(mcp_client)
    if not component_surface_available():
        failure = await mcp.call_tool_failure(
            "ha_config_get_dashboard",
            {"card_type": "tile", "describe": True},
        )
        assert failure["error"]["code"] == "COMPONENT_NOT_INSTALLED"
        return

    # Lazy installation/indexing can outlast one bounded describe request on
    # a cold HAOS runner. Retry the production path, without preinstalling it.
    tile = await wait_for_tool_result(
        mcp_client,
        "ha_config_get_dashboard",
        {"card_type": "tile", "describe": True},
        lambda data: bool(data.get("fields")),
        timeout=90,
        description="built-in card production first-use initialization",
    )
    fields = {f["name"]: f for f in tile["fields"]}
    assert fields["entity"]["type"] == "entity"
    assert fields["color"]["description"], fields["color"]
    assert tile["description"]

    listed = await mcp.call_tool_success("ha_config_get_dashboard", {"describe": True})
    assert {"tile", "grid", "heading"} <= {c["type"] for c in listed["card_types"]}
    blank = await mcp.call_tool_success(
        "ha_config_get_dashboard", {"describe": True, "card_type": ""}
    )
    assert "tile" in {c["type"] for c in blank["card_types"]}

    failure = await mcp.call_tool_failure(
        "ha_config_get_dashboard",
        {"card_type": "no-such-card", "describe": True},
        expected_error="no-such-card",
    )
    assert failure["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "tile" in failure["error"]["suggestion"]


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
        {
            "type": "custom:config-template-card",
            "card": {
                "type": "tile",
                "entity": '${"light.bed_light"}',
                "vertical": "${true}",
            },
        },
    ]
    try:
        result = await mcp.call_tool_success(
            "ha_config_set_dashboard",
            {
                "url_path": path,
                "config": {
                    "views": [
                        {"title": "Checks", "cards": cards},
                        {
                            "title": "Nested",
                            "type": "sections",
                            "header": {"card": {"type": "no-such-header"}},
                            "sections": [
                                {
                                    "type": "grid",
                                    "cards": [
                                        {
                                            "type": "custom:button-card",
                                            "custom_fields": {
                                                "nested": {
                                                    "card": {
                                                        "type": "no-such-field-card"
                                                    }
                                                },
                                                "text": "Not a card",
                                            },
                                        },
                                        {
                                            "type": "custom:state-switch",
                                            "states": {
                                                "on": {"type": "no-such-state-card"},
                                            },
                                        },
                                    ],
                                }
                            ],
                        },
                    ]
                },
                "MandatoryBPS": False,
            },
        )
        warnings = "\n".join(result.get("warnings", []))
        if not component_surface_available():
            assert "card option" not in warnings, warnings
            assert "editor schema advisory" not in warnings, warnings
            return
        assert "views[0].cards[0]" not in warnings, warnings
        assert "'colour' is not listed in the tile editor schema" in warnings
        assert "views[0].cards[2]: unknown card type 'no-such-card'" in warnings
        assert any(
            "views[0].cards[3].cards[0] (button):" in warning and "'entitty'" in warning
            for warning in result["warnings"]
        )
        assert "views[1].header.card: unknown card type 'no-such-header'" in warnings
        assert "no-such-field-card" in warnings
        assert "no-such-state-card" in warnings
        assert "no card type configured" not in warnings  # Field wrappers are metadata.
        assert "cards[4].card" not in warnings  # A template is not the final config.
    finally:
        await safe_call_tool(
            mcp_client, "ha_config_delete_dashboard", {"url_path": path}
        )


@pytest.mark.asyncio
async def test_custom_cards_are_checked_and_described_from_their_resource(mcp_client):
    """A card from a dashboard resource answers for itself, like a HACS card."""
    mcp = MCPAssertions(mcp_client)
    if not component_surface_available():
        failure = await mcp.call_tool_failure(
            "ha_config_get_dashboard",
            {"card_type": "custom:e2e-custom-card", "describe": True},
        )
        assert failure["error"]["code"] == "COMPONENT_NOT_INSTALLED"
        return
    path = "custom-card-checks-" + uuid4().hex[:8]
    resource = await mcp.call_tool_success(
        "ha_config_set_dashboard_resource",
        {"url": "/local/e2e-custom-card.js", "resource_type": "module"},
    )
    try:
        # No preseeded DOM cache: wait for the user's first-use download,
        # integrity verification and resource loading to complete.
        listed = await wait_for_tool_result(
            mcp_client,
            "ha_config_get_dashboard",
            {"describe": True},
            lambda data: (
                "custom:e2e-custom-card"
                in {c["type"] for c in data.get("card_types", [])}
            ),
            timeout=90,
            description="custom card production first-use loading",
        )
        assert "custom:e2e-custom-card" in {c["type"] for c in listed["card_types"]}
        card = await mcp.call_tool_success(
            "ha_config_get_dashboard",
            {"card_type": "custom:e2e-custom-card", "describe": True},
        )
        assert card["fields"] == [
            {"name": "entity", "required": True, "type": "entity"}
        ]
        assert card["field_coverage"] == "partial"
        assert "editor-only" in card["note"]

        cards = [
            {"type": "custom:e2e-custom-card"},
            {"type": "custom:e2e-custom-card", "entity": "light.bed_light"},
            {
                "type": "custom:e2e-custom-card",
                "entity": "light.bed_light",
                "disabled": True,
            },
            {"type": "custom:e2e-type-error-card"},
            {"type": "custom:e2e-type-error-card", "mode": "browser"},
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
        assert (
            "views[0].cards[2] (custom:e2e-custom-card): editor schema advisory"
            in warnings
        )
        assert (
            "'disabled' is not listed in the e2e-custom-card editor schema" in warnings
        )
        for index in (3, 4):
            assert any(
                f"views[0].cards[{index}]" in warning
                and "check inconclusive (TypeError:" in warning
                and "does not establish invalid configuration" in warning
                for warning in result.get("warnings", [])
            ), result
        assert "entity must be a string" in warnings
        saved = await mcp.call_tool_success(
            "ha_config_get_dashboard", {"url_path": path}
        )
        assert saved["config"]["views"][0]["cards"] == cards
        # Each fault must leave time for the following card. Combining every
        # deliberate timeout in one save tests the aggregate cutoff instead.
        for fault in ("slow-verdict", "slow-editor", "broken-editor"):
            result = await mcp.call_tool_success(
                "ha_config_set_dashboard",
                {
                    "url_path": path,
                    "config": {
                        "views": [
                            {
                                "cards": [
                                    {"type": f"custom:e2e-{fault}-card"},
                                    {"type": "custom:e2e-custom-card"},
                                ]
                            }
                        ]
                    },
                    "MandatoryBPS": False,
                },
            )
            assert any(
                "views[0].cards[1]" in w and "needs an entity" in w
                for w in result.get("warnings", [])
            ), (fault, result)
            if fault == "broken-editor":
                assert any(
                    "views[0].cards[0]" in w and "needs an entity" in w
                    for w in result.get("warnings", [])
                ), result
        no_form = await mcp.call_tool_success(
            "ha_config_get_dashboard",
            {"describe": True, "card_type": "custom:e2e-broken-editor-card"},
        )
        assert no_form["fields"] is None and "field_coverage" not in no_form
    finally:
        await safe_call_tool(
            mcp_client, "ha_config_delete_dashboard", {"url_path": path}
        )
        await safe_call_tool(
            mcp_client,
            "ha_config_delete_dashboard_resource",
            {"resource_id": resource["resource_id"]},
        )


@pytest.mark.asyncio
async def test_componentless_writes_keep_static_card_guidance(mcp_client):
    if component_surface_available():
        return  # The no-component lane exercises the fallback contract.
    mcp = MCPAssertions(mcp_client)
    path = "card-guide-" + uuid4().hex[:8]
    try:
        result = await mcp.call_tool_success(
            "ha_config_set_dashboard",
            {
                "url_path": path,
                "config": {"views": [{"title": "Guide", "path": "guide", "cards": []}]},
                "MandatoryBPS": True,
            },
        )
        assert "references/dashboard-cards.md" in result.get("skill_content", {})
    finally:
        await safe_call_tool(
            mcp_client, "ha_config_delete_dashboard", {"url_path": path}
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "card_type,options",
    [
        ("tile", {"entity": "light.bed_light"}),
        ("button", {"entity": "light.bed_light"}),
        ("sensor", {"entity": "sensor.test"}),
        ("light", {"entity": "light.bed_light"}),
        ("thermostat", {"entity": "climate.test"}),
        ("history-graph", {"entities": ["sensor.test"]}),
        ("picture-entity", {"entity": "light.bed_light"}),
        ("markdown", {"content": "Coverage"}),
        ("gauge", {"entity": "sensor.test"}),
        ("entity", {"entity": "light.bed_light"}),
        ("logbook", {"entities": ["light.bed_light"]}),
    ],
)
async def test_common_card_coverage_tracks_the_installed_frontend(
    mcp_client, card_type, options
):
    """Stable and nightly beta lanes catch per-editor parser regressions."""
    if not component_surface_available():
        return
    mcp = MCPAssertions(mcp_client)
    described = await mcp.call_tool_success(
        "ha_config_get_dashboard",
        {
            "describe": True,
            "card_type": card_type,
        },
    )
    assert described["fields"], f"{card_type} editor form coverage regressed"
    path = "card-coverage-" + uuid4().hex[:8]
    try:
        saved = await mcp.call_tool_success(
            "ha_config_set_dashboard",
            {
                "url_path": path,
                "config": {
                    "views": [
                        {
                            "title": "Coverage",
                            "path": "coverage",
                            "cards": [
                                {
                                    "type": card_type,
                                    **options,
                                    "e2e_unknown_option": True,
                                },
                            ],
                        }
                    ]
                },
                "MandatoryBPS": False,
            },
        )
        assert any(
            f"'e2e_unknown_option' is not listed in the {card_type} editor schema"
            in warning
            for warning in saved.get("warnings", [])
        ), saved
    finally:
        await safe_call_tool(
            mcp_client, "ha_config_delete_dashboard", {"url_path": path}
        )


@pytest.mark.asyncio
async def test_described_fields_match_stored_card_configuration(mcp_client):
    """Forms must describe stored config, including editor-translated fields."""
    if not component_surface_available():
        return
    mcp = MCPAssertions(mcp_client)
    descriptions = {}
    for card_type in (
        "area",
        "statistics-graph",
        "tile",
        "shortcut",
        "sensor",
        "markdown",
        "gauge",
        "entity",
        "alarm-panel",
    ):
        result = await mcp.call_tool_success(
            "ha_config_get_dashboard", {"describe": True, "card_type": card_type}
        )
        assert result["fields"], card_type
        descriptions[card_type] = {field["name"]: field for field in result["fields"]}
    for card_type in ("tile", "shortcut"):
        assert (
            not {"content", "interactions", "content_layout"}
            & descriptions[card_type].keys()
        )
        assert "tap_action" in descriptions[card_type]
    assert "text_only" in descriptions["markdown"]
    assert not {"style", "actions_warning"} & descriptions["markdown"].keys()
    assert "show_severity" not in descriptions["gauge"]
    assert {field["name"] for field in descriptions["gauge"]["severity"]["fields"]} == {
        "green",
        "yellow",
        "red",
    }
    assert "entity" in descriptions["entity"]
    alarm = descriptions["alarm-panel"]
    assert alarm["entity"]["required"] and alarm["entity"]["type"] == "entity"
    assert alarm["entity"]["domain"] == "alarm_control_panel"
    assert alarm["states"]["type"] == "select" and alarm["states"]["options"]
    sensor = descriptions["sensor"]
    assert sensor["detail"]["type"] == "number"
    assert "min" not in sensor and "max" not in sensor
    assert {field["name"] for field in sensor["limits"]["fields"]} == {"min", "max"}
    path = "card-fields-" + uuid4().hex[:8]
    try:
        result = await mcp.call_tool_success(
            "ha_config_set_dashboard",
            {
                "url_path": path,
                "config": {
                    "views": [
                        {
                            "title": "Fields",
                            "path": "fields",
                            "cards": [
                                {
                                    "type": "tile",
                                    "entity": "light.bed_light",
                                    "tap_action": {"action": "none"},
                                },
                                {
                                    "type": "sensor",
                                    "entity": "sensor.test",
                                    "detail": 2,
                                    "limits": {"min": 0, "max": 100},
                                },
                            ],
                        }
                    ]
                },
                "MandatoryBPS": False,
            },
        )
        assert not result.get("warnings"), result
    finally:
        await safe_call_tool(
            mcp_client, "ha_config_delete_dashboard", {"url_path": path}
        )


@pytest.mark.asyncio
async def test_editor_metadata_includes_subeditors_and_declares_partial_coverage(
    mcp_client,
):
    if not component_surface_available():
        return
    mcp = MCPAssertions(mcp_client)
    expected = {
        "history-graph": {"entities"},
        "statistics-graph": {"entities"},
        "picture-glance": {"entities"},
        "map": {"entities"},
        "calendar": {"entities"},
        "distribution": {"entities"},
        "tile": {"features", "features_position", "vertical"},
        "picture-elements": {"elements"},
        "weather-forecast": {
            "forecast_type",
            "forecast_slots",
            "show_current",
            "show_forecast",
            "tap_action",
        },
    }
    for card_type, names in expected.items():
        result = await mcp.call_tool_success(
            "ha_config_get_dashboard", {"describe": True, "card_type": card_type}
        )
        assert names <= {field["name"] for field in result["fields"]}, result
        assert result["field_coverage"] == "partial"
        assert "additional options" in result["note"]
    for card_type in (
        "dialog-edit",
        "dialog-create",
        "dialog-delete",
        "dialog-suggest",
        "suggestion",
    ):
        await mcp.call_tool_failure(
            "ha_config_get_dashboard", {"describe": True, "card_type": card_type}
        )
    for card_type in ("horizontal-stack", "vertical-stack", "shopping-list"):
        await mcp.call_tool_success(
            "ha_config_get_dashboard", {"describe": True, "card_type": card_type}
        )


@pytest.mark.asyncio
async def test_editor_advice_does_not_claim_valid_runtime_options_are_invalid(
    mcp_client,
):
    if not component_surface_available():
        return
    mcp = MCPAssertions(mcp_client)
    path = "editor-advice-" + uuid4().hex[:8]
    config = {
        "views": [
            {
                "title": "Advice",
                "path": "advice",
                "cards": [
                    {
                        "type": "iframe",
                        "url": "https://example.com",
                        "allow": "fullscreen",
                        "disable_sandbox": True,
                    },
                    {
                        "type": "button",
                        "entity": "light.bed_light",
                        "tap_action": {"action": "fire-dom-event"},
                    },
                    {
                        "type": "button",
                        "entity": "light.bed_light",
                        "tap_action": {
                            "action": "toggle",
                            "confirmation": {"exemptions": [{"user": "test"}]},
                        },
                    },
                ],
            }
        ]
    }
    try:
        saved = await mcp.call_tool_success(
            "ha_config_set_dashboard",
            {"url_path": path, "config": config, "MandatoryBPS": False},
        )
        assert all(
            "editor schema advisory; runtime support may differ" in warning
            for warning in saved.get("warnings", [])
        ), saved
        readback = await mcp.call_tool_success(
            "ha_config_get_dashboard", {"url_path": path}
        )
        assert readback["config"] == config
    finally:
        await safe_call_tool(
            mcp_client, "ha_config_delete_dashboard", {"url_path": path}
        )
