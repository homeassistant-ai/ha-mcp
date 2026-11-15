"""Pagination of the three collection-listing tools (issue #1869).

``ha_config_list_groups``, ``ha_config_list_dashboard_resources`` and
``ha_config_list_helpers`` take ``limit``/``offset`` and emit the shared
pagination envelope: ``count`` is the page size, ``total_count`` the size of the
whole collection, and the summaries that describe the whole collection
(``inline_count``, ``by_type``) do not shrink to the page.

Every helper route builds its own envelope (the ``{type}/list`` body, the
component's single-type answer for storage and flow types, and its all-types
listing merged with tags), and the slice is applied once after them, so each
route is paged here. The collections are Core's own: states, Lovelace
resources, storage helpers created through Core's commands and config entries.
"""

from __future__ import annotations

import logging

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_groups import GroupTools
from ha_mcp.tools.tools_resources import ResourceTools

from ._client_spies import sent_commands
from ._routing_core import (
    list_helpers,
    pooled_socket,
    sent_types,
    setup_every_simple_type,
    setup_storage_booleans,
)

_PAGINATION_KEYS = {
    "total_count",
    "offset",
    "limit",
    "count",
    "has_more",
    "next_offset",
}


def _groups(hass: HomeAssistant, count: int) -> None:
    for i in range(count):
        hass.states.async_set(
            f"group.g{i:03d}",
            "on",
            {"friendly_name": f"Group {i:03d}", "entity_id": []},
        )


async def _booleans(client: HomeAssistantClient, count: int) -> list[str]:
    """``count`` storage input_booleans, in creation order; returns their ids."""
    ids = []
    for i in range(count):
        created = await client.send_websocket_message(
            {"type": "input_boolean/create", "name": f"Bool {i:03d}"}
        )
        ids.append(created["result"]["id"])
    return ids


def _template_helpers(hass: HomeAssistant, count: int) -> None:
    registry = er.async_get(hass)
    for i in range(count):
        entry = MockConfigEntry(
            domain="template",
            title=f"Templated {i:03d}",
            options={"state": "{{ 1 + 1 }}"},
        )
        entry.add_to_hass(hass)
        registry.async_get_or_create(
            "sensor", "template", f"templated_{i:03d}", config_entry=entry
        )


def _ours(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """The warnings this server logged (Core's own setup may log too)."""
    return [
        r
        for r in caplog.records
        if r.name.startswith("ha_mcp") and r.levelno >= logging.WARNING
    ]


@pytest.fixture
async def helpers_core(hass: HomeAssistant, core_client: HomeAssistantClient) -> None:
    """Core's registry API and storage input_booleans."""
    await setup_storage_booleans(hass)


# --- groups ------------------------------------------------------------------------


@pytest.fixture
def groups(core_client: HomeAssistantClient) -> GroupTools:
    return GroupTools(core_client)


async def test_groups_below_the_limit_are_listed_whole_with_metadata(
    hass: HomeAssistant, groups: GroupTools
) -> None:
    _groups(hass, 3)

    result = await groups.ha_config_list_groups()

    assert [g["entity_id"] for g in result["groups"]] == [
        "group.g000",
        "group.g001",
        "group.g002",
    ]
    assert (result["count"], result["total_count"]) == (3, 3)
    assert result["has_more"] is False
    assert result["next_offset"] is None


async def test_the_first_page_of_groups_reports_more(
    hass: HomeAssistant, groups: GroupTools
) -> None:
    _groups(hass, 250)

    result = await groups.ha_config_list_groups()

    assert len(result["groups"]) == result["count"] == 100
    assert result["total_count"] == 250
    assert (result["limit"], result["offset"]) == (100, 0)
    assert result["has_more"] is True
    assert result["next_offset"] == 100


async def test_an_offset_selects_the_next_window_of_groups(
    hass: HomeAssistant, groups: GroupTools
) -> None:
    _groups(hass, 250)

    result = await groups.ha_config_list_groups(limit=100, offset=100)

    assert result["groups"][0]["entity_id"] == "group.g100"
    assert result["count"] == 100
    assert result["next_offset"] == 200


async def test_the_last_page_of_groups_is_short_and_final(
    hass: HomeAssistant, groups: GroupTools
) -> None:
    _groups(hass, 250)

    result = await groups.ha_config_list_groups(limit=100, offset=200)

    assert result["count"] == 50
    assert result["has_more"] is False
    assert result["next_offset"] is None


async def test_an_offset_past_the_end_is_an_empty_final_page(
    hass: HomeAssistant, groups: GroupTools
) -> None:
    _groups(hass, 5)

    result = await groups.ha_config_list_groups(offset=500)

    assert result["groups"] == []
    assert (result["count"], result["total_count"]) == (0, 5)
    assert result["has_more"] is False
    assert result["next_offset"] is None


async def test_the_groups_message_counts_the_whole_collection(
    hass: HomeAssistant, groups: GroupTools
) -> None:
    _groups(hass, 250)

    result = await groups.ha_config_list_groups()

    assert result["message"] == "Found 250 group(s)"


# --- dashboard resources ---------------------------------------------------------


@pytest.fixture
async def resources(hass: HomeAssistant, core_client: HomeAssistantClient) -> list[str]:
    """120 module resources in Core's Lovelace storage; returns their ids."""
    assert await async_setup_component(hass, "lovelace", {})
    for i in range(120):
        await core_client.send_websocket_message(
            {
                "type": "lovelace/resources/create",
                "res_type": "module",
                "url": f"/local/card{i:03d}.js",
            }
        )
    listed = await core_client.send_websocket_message({"type": "lovelace/resources"})
    return [r["id"] for r in listed["result"]]


async def test_the_first_page_of_resources_reports_more(
    resources: list[str], core_client: HomeAssistantClient
) -> None:
    result = await ResourceTools(core_client).ha_config_list_dashboard_resources()

    assert len(result["resources"]) == result["count"] == 100
    assert result["total_count"] == 120
    assert result["has_more"] is True
    assert result["next_offset"] == 100


async def test_an_offset_selects_the_next_window_of_resources(
    resources: list[str], core_client: HomeAssistantClient
) -> None:
    result = await ResourceTools(core_client).ha_config_list_dashboard_resources(
        limit=10, offset=100
    )

    assert [r["id"] for r in result["resources"]] == resources[100:110]
    assert result["count"] == 10


async def test_resource_summaries_describe_the_whole_collection(
    resources: list[str], core_client: HomeAssistantClient
) -> None:
    """``by_type``/``inline_count`` must not shrink to the returned page."""
    result = await ResourceTools(core_client).ha_config_list_dashboard_resources(
        limit=10
    )

    assert len(result["resources"]) == 10
    assert result["by_type"]["module"] == 120
    assert result["inline_count"] == 0


# --- helpers: the legacy {type}/list body -----------------------------------------


async def test_the_legacy_helper_list_is_paged(
    helpers_core: None, core_client: HomeAssistantClient
) -> None:
    ids = await _booleans(core_client, 120)

    result = await list_helpers(
        core_client, helper_type="input_boolean", limit=50, offset=50
    )

    assert [h["id"] for h in result["helpers"]] == ids[50:100]
    assert (result["count"], result["total_count"]) == (50, 120)
    assert result["has_more"] is True
    assert result["next_offset"] == 100


async def test_a_short_legacy_helper_list_is_unchanged_plus_metadata(
    helpers_core: None, core_client: HomeAssistantClient
) -> None:
    ids = await _booleans(core_client, 2)

    result = await list_helpers(core_client, helper_type="input_boolean")

    assert [h["id"] for h in result["helpers"]] == ids
    assert (result["count"], result["total_count"]) == (2, 2)
    assert result["has_more"] is False
    assert set(result) >= _PAGINATION_KEYS


async def test_persons_from_storage_and_yaml_are_both_listed_unflagged(
    hass: HomeAssistant,
    helpers_core: None,
    core_client: HomeAssistantClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``person/list`` answers ``{"storage": [...], "config": [...]}``: both
    halves are listed, and a shape Core gives is not logged as malformed."""
    assert await async_setup_component(
        hass, "person", {"person": [{"id": "bob", "name": "Bob"}]}
    )
    created = await core_client.send_websocket_message(
        {"type": "person/create", "name": "Alice"}
    )

    with caplog.at_level(logging.WARNING, logger="ha_mcp"):
        result = await list_helpers(core_client, helper_type="person")

    assert sorted(h["id"] for h in result["helpers"]) == sorted(
        [created["result"]["id"], "bob"]
    )
    assert result["total_count"] == 2
    assert not _ours(caplog), [r.getMessage() for r in _ours(caplog)]


async def test_an_empty_person_list_is_not_flagged(
    hass: HomeAssistant,
    helpers_core: None,
    core_client: HomeAssistantClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No persons at all is legitimately empty, not a malformed answer."""
    assert await async_setup_component(hass, "person", {})

    with caplog.at_level(logging.WARNING, logger="ha_mcp"):
        result = await list_helpers(core_client, helper_type="person")

    assert result["helpers"] == []
    assert result["total_count"] == 0
    assert "warnings" not in result
    assert not _ours(caplog), [r.getMessage() for r in _ours(caplog)]


# --- helpers: the component's answers ---------------------------------------------


async def test_the_component_helper_list_is_paged(
    helpers_core: None, component_core_client: HomeAssistantClient
) -> None:
    """The component's single-type answer has its own envelope builder; the
    legacy list is never read."""
    ids = await _booleans(component_core_client, 120)
    socket = await pooled_socket(component_core_client)

    with sent_commands(socket) as sent:
        result = await list_helpers(
            component_core_client, helper_type="input_boolean", limit=50, offset=50
        )

    assert "input_boolean/list" not in sent_types(sent)
    assert [h["id"] for h in result["helpers"]] == ids[50:100]
    assert (result["count"], result["total_count"]) == (50, 120)
    assert result["has_more"] is True
    assert result["next_offset"] == 100


async def test_a_flow_helper_list_is_paged(
    hass: HomeAssistant, helpers_core: None, component_core_client: HomeAssistantClient
) -> None:
    """Flow types are served only by the component; a large set is still paged."""
    _template_helpers(hass, 120)

    result = await list_helpers(
        component_core_client, helper_type="template", limit=50, offset=100
    )

    assert len(result["helpers"]) == result["count"] == 20
    assert result["total_count"] == 120
    assert result["has_more"] is False
    assert result["next_offset"] is None


@pytest.fixture
async def all_types(
    hass: HomeAssistant, helpers_core: None, component_core_client: HomeAssistantClient
) -> HomeAssistantClient:
    """Every storage helper type set up, 120 input_booleans and one tag."""
    await setup_every_simple_type(hass)
    await _booleans(component_core_client, 120)
    await component_core_client.send_websocket_message(
        {"type": "tag/create", "tag_id": "tag-42"}
    )
    return component_core_client


async def test_all_types_pages_the_listing_merged_with_tags(
    all_types: HomeAssistantClient,
) -> None:
    """Tags are merged in after the component's listing, so the slice runs
    after the merge: 120 helpers + 1 tag, paged."""
    result = await list_helpers(all_types, helper_type="all", limit=100)

    assert result["total_count"] == 121
    assert len(result["helpers"]) == result["count"] == 100
    assert result["has_more"] is True
    assert result["next_offset"] == 100


async def test_the_last_all_types_page_holds_the_merged_tag(
    all_types: HomeAssistantClient,
) -> None:
    result = await list_helpers(all_types, helper_type="all", limit=100, offset=100)

    assert result["count"] == 21
    assert result["helpers"][-1]["id"] == "tag-42"
    assert result["has_more"] is False
    assert result["next_offset"] is None
