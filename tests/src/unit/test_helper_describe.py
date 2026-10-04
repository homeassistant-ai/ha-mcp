"""ha_config_list_helpers(describe=True): helper fields read from HA (#2632)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.tools.config_helpers import describe as mod

_SCHEDULE_CORE = [
    {"type": "string", "lengthMin": 1, "name": "name", "required": True},
    {"name": "monday", "required": False, "optional": True},  # opaque validator
]


def _fields(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {f["name"]: f for f in result["fields"]}


@pytest.fixture
def client() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def no_component(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "fetch_helper_schemas", AsyncMock(return_value=None))
    monkeypatch.setattr(mod, "read_helper_item", AsyncMock(return_value=None))


@pytest.mark.asyncio
async def test_storage_helper_fields_come_from_core_when_component_present(
    client, monkeypatch
):
    """With the component, the field list is Core's, typed where Core is opaque."""
    monkeypatch.setattr(
        mod,
        "fetch_helper_schemas",
        AsyncMock(return_value={"schedule": {"create": _SCHEDULE_CORE}}),
    )

    result = await mod.describe_helper(client, "schedule")

    assert result["source"] == "core_schema"
    fields = _fields(result)
    assert fields["name"]["required"] is True
    # Core's schedule-day validator does not serialize; the static table types it.
    assert "type" in fields["monday"] and fields["monday"].get("description")


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_component")
async def test_storage_helper_without_component_falls_back_to_static_table(client):
    """A no-component install still gets a usable field list."""
    result = await mod.describe_helper(client, "input_number")

    assert result["source"] == "static_fallback"
    assert "name" in _fields(result) and len(_fields(result)) > 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_component")
async def test_existing_storage_helper_reports_current_values_without_component(
    client,
):
    """Editing an existing helper starts from its stored values."""
    client.send_websocket_message.return_value = {
        "result": [{"id": "pool", "name": "Pool", "min": 0, "max": 40}]
    }

    result = await mod.describe_helper(client, "input_number", helper_id="pool")

    # The stored item says "max"; the fallback field may use the tool's alias.
    currents = {f.get("current") for f in result["fields"] if "max" in f["name"]}
    assert currents == {40}


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_component")
async def test_unknown_storage_helper_says_current_values_are_missing(client):
    client.send_websocket_message.return_value = {"result": []}

    result = await mod.describe_helper(client, "input_number", helper_id="gone")

    assert result["current_unavailable"] is True


@pytest.mark.asyncio
async def test_flow_helper_menu_lists_sub_types_until_one_is_chosen(
    client, monkeypatch
):
    """A menu-rooted helper names its sub-types instead of failing."""
    monkeypatch.setattr(
        mod,
        "fetch_helper_flow_info",
        AsyncMock(return_value={"menu_options": ["sensor", "binary_sensor"]}),
    )

    result = await mod.describe_helper(client, "template")

    assert result["menu_options"] == ["sensor", "binary_sensor"]
    assert "fields" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("last_step", "more_forms"),
    [(False, "yes"), (None, "depends_on_answers"), (True, None)],
)
async def test_flow_helper_says_when_more_forms_follow(
    client, monkeypatch, last_step, more_forms
):
    """A statistics helper asks for its characteristic on a second form; the
    agent learns that instead of taking the first form for all of it."""
    monkeypatch.setattr(
        mod,
        "fetch_helper_flow_info",
        AsyncMock(
            return_value={
                "step_id": "user",
                "schema": [{"name": "entity_id"}],
                "last_step": last_step,
            }
        ),
    )

    client.send_websocket_message.return_value = {}

    result = await mod.describe_helper(client, "statistics")

    assert result.get("more_forms") == more_forms
    assert ("note" in result) is (more_forms is not None)


@pytest.mark.asyncio
async def test_existing_flow_helper_with_a_later_options_form_says_so(client):
    client.start_options_flow.return_value = {
        "type": "form",
        "flow_id": "f1",
        "step_id": "init",
        "data_schema": [{"name": "away_temp"}],
        "last_step": False,
    }
    client.send_websocket_message.return_value = {}

    result = await mod.describe_helper(client, "generic_thermostat", helper_id="entry1")

    assert result["more_forms"] == "yes"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("menu", "expected"),
    [(["a", 1, None], ["a"]), (None, None), ([2], None)],
)
async def test_options_flow_menu_lists_only_string_choices(client, menu, expected):
    """A malformed options-flow menu reports nothing rather than a None list."""
    client.start_options_flow.return_value = {
        "type": "menu",
        "flow_id": "f1",
        "menu_options": menu,
    }

    result = await mod.describe_helper(client, "group", helper_id="entry1")

    assert result.get("menu_options") == expected
    assert result["note"]
    client.abort_options_flow.assert_awaited_once_with("f1")


@pytest.mark.asyncio
async def test_existing_flow_helper_reads_current_values_and_aborts_options_flow(
    client,
):
    """An options flow opened only to read values is never left open."""
    client.start_options_flow.return_value = {
        "type": "form",
        "flow_id": "f1",
        "data_schema": [
            {
                "name": "state",
                "required": True,
                "selector": {"template": {}},
                "description": {"suggested_value": "{{ 1 + 1 }}"},
            }
        ],
    }

    result = await mod.describe_helper(client, "template", helper_id="entry1")

    assert _fields(result)["state"]["current"] == "{{ 1 + 1 }}"
    client.abort_options_flow.assert_awaited_once_with("f1")


def test_long_select_lists_are_truncated_but_say_how_many_remain():
    """A 175-unit dropdown must not flood a small model's context."""
    field = {
        "name": "unit_of_measurement",
        "selector": {"select": {"options": [f"u{i}" for i in range(175)]}},
    }

    options = mod.compact_field(field)["options"]

    assert len(options) <= mod.MAX_OPTIONS + 1
    assert str(175 - mod.MAX_OPTIONS) in options[-1]


def test_collapsed_sections_are_described_inline():
    """Fields hidden in an expandable section (e.g. availability) stay visible."""
    field = {
        "type": "expandable",
        "name": "additional_options",
        "schema": [{"name": "availability", "selector": {"template": {}}}],
    }

    compact = mod.compact_field(field)

    assert compact["fields"][0]["name"] == "availability"


@pytest.mark.asyncio
async def test_flow_fields_carry_ha_own_help_text_including_sections(
    client, monkeypatch
):
    """The model sees the same per-field help the HA UI shows, with no doc to maintain."""
    monkeypatch.setattr(
        mod,
        "fetch_helper_flow_info",
        AsyncMock(
            return_value={
                "step_id": "sensor",
                "schema": [
                    {"name": "state", "selector": {"template": {}}},
                    {
                        "type": "expandable",
                        "name": "additional_options",
                        "schema": [
                            {"name": "availability", "selector": {"template": {}}}
                        ],
                    },
                ],
            }
        ),
    )
    p = "component.template.config.step.sensor"
    client.send_websocket_message.return_value = {
        "resources": {
            f"{p}.data_description.state": "Template for the state.",
            f"{p}.sections.additional_options.data_description.availability": "Avail.",
            "component.template.config.step.binary_sensor.data_description.state": "x",
        }
    }

    fields = _fields(
        await mod.describe_helper(client, "template", menu_choice="sensor")
    )

    assert fields["state"]["description"] == "Template for the state."
    assert fields["additional_options"]["fields"][0]["description"] == "Avail."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    [RuntimeError("ws down"), {"result": ["not", "a", "dict"]}, {"resources": "x"}],
    ids=["call-fails", "result-not-a-dict", "resources-not-a-dict"],
)
async def test_flow_fields_still_described_when_translations_fail(
    client, monkeypatch, reply
):
    """Help text is best-effort: a failed or malformed reply leaves it out."""
    monkeypatch.setattr(
        mod,
        "fetch_helper_flow_info",
        AsyncMock(return_value={"step_id": "user", "schema": [{"name": "source"}]}),
    )
    if isinstance(reply, Exception):
        client.send_websocket_message.side_effect = reply
    else:
        client.send_websocket_message.return_value = reply

    result = await mod.describe_helper(client, "derivative")

    assert [f["name"] for f in result["fields"]] == ["source"]


@pytest.mark.asyncio
async def test_flow_field_without_help_text_gets_its_ui_label(client, monkeypatch):
    """A field HA documents only by its label still says what it is."""
    monkeypatch.setattr(
        mod,
        "fetch_helper_flow_info",
        AsyncMock(return_value={"step_id": "user", "schema": [{"name": "source"}]}),
    )
    client.send_websocket_message.return_value = {
        "resources": {
            "component.utility_meter.config.step.user.data.source": "Input sensor"
        }
    }

    result = await mod.describe_helper(client, "utility_meter")

    assert result["fields"][0]["description"] == "Input sensor"


@pytest.mark.asyncio
async def test_core_fields_follow_a_stable_order_with_hints_first(client, monkeypatch):
    """Core returns weekdays shuffled; the hinted first day must lead."""
    shuffled = [
        {"name": "sunday", "required": False},
        {"name": "name", "type": "string", "required": True},
        {"name": "monday", "required": False},
    ]
    monkeypatch.setattr(
        mod,
        "fetch_helper_schemas",
        AsyncMock(return_value={"schedule": {"create": shuffled}}),
    )

    result = await mod.describe_helper(client, "schedule")

    assert [f["name"] for f in result["fields"]] == ["name", "monday", "sunday"]
