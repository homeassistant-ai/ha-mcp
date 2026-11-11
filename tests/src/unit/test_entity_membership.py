"""Tests for generic entity-membership normalization."""

from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.utils.entity_membership import normalize_member_entity_ids
from ha_mcp.visibility import resolver
from ha_mcp.visibility.model import VisibilityConfig
from ha_mcp.visibility.persistence import save_visibility_config

from ._search_tool import _build_ha_search


def test_prefers_modern_group_entities_and_sorts_deduplicates() -> None:
    """Prefer modern membership and normalize it deterministically."""
    assert normalize_member_entity_ids(
        {
            "group_entities": ["light.two", "light.one", "light.two"],
            "entity_id": ["light.legacy"],
        }
    ) == ["light.one", "light.two"]


def test_falls_back_to_legacy_entity_id_collection() -> None:
    """Accept the legacy entity_id member collection."""
    assert normalize_member_entity_ids({"entity_id": ("light.two", "light.one")}) == [
        "light.one",
        "light.two",
    ]


def test_empty_collection_is_explicit_membership() -> None:
    """Treat an empty member collection as explicit aggregate evidence."""
    assert normalize_member_entity_ids({"group_entities": []}) == []


def test_scalar_entity_id_is_not_membership() -> None:
    """Reject scalar entity_id attributes as membership evidence."""
    assert normalize_member_entity_ids({"entity_id": "light.one"}) is None


def test_malformed_collection_fails_closed() -> None:
    """Reject a collection when any member is not a valid entity ID."""
    assert (
        normalize_member_entity_ids({"group_entities": ["light.one", "not-an-entity"]})
        is None
    )


def test_invalid_modern_value_can_fall_back_to_valid_legacy_membership() -> None:
    """Use valid legacy membership when the modern attribute is malformed."""
    assert normalize_member_entity_ids(
        {"group_entities": "light.one", "entity_id": ["light.legacy"]}
    ) == ["light.legacy"]


def test_member_ids_are_returned_as_an_opaque_collection() -> None:
    """Return explicit member IDs without interpreting their relationship."""
    assert normalize_member_entity_ids(
        {"group_entities": ["light.group", "light.member"]}
    ) == ["light.group", "light.member"]


def test_mapping_is_not_a_member_collection() -> None:
    """Reject mappings even though they are iterable containers."""
    assert (
        normalize_member_entity_ids({"group_entities": {"light.member": True}}) is None
    )


def test_uppercase_or_invalid_characters_fail_closed() -> None:
    """Reject entity IDs outside the lowercase ID grammar."""
    assert normalize_member_entity_ids({"entity_id": ["light.Not_Valid"]}) is None
    assert normalize_member_entity_ids({"entity_id": ["light.not-valid"]}) is None


async def test_visibility_denied_member_withholds_ids_but_keeps_group_signal(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A group with a member hidden from the agent still reads as a group,
    but ha_search withholds its member list rather than leak the hidden id."""
    save_visibility_config(
        tmp_path,
        VisibilityConfig(
            enabled=True, exclude_categories=[], deny_entity_ids=["light.denied"]
        ),
    )
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)
    # ``config`` serves the registry reads of the search.
    assert await async_setup_component(hass, "config", {})
    hass.states.async_set("light.visible", "on")
    hass.states.async_set("light.denied", "on")
    hass.states.async_set(
        "light.group",
        "on",
        {
            "friendly_name": "Group",
            "group_entities": ["light.visible", "light.denied"],
        },
    )

    data = await _build_ha_search(core_client)(
        query="light.group",
        result_fields=["entity_id", "is_group", "member_entity_ids"],
    )

    row = next(row for row in data["entities"] if row["entity_id"] == "light.group")
    assert row == {"entity_id": "light.group", "is_group": True}


def test_frozenset_is_normalized_and_non_ascii_is_rejected() -> None:
    """Normalize internal sets while rejecting non-ASCII entity IDs."""
    assert normalize_member_entity_ids(
        {"group_entities": frozenset({"light.two", "light.one"})}
    ) == ["light.one", "light.two"]
    assert normalize_member_entity_ids({"entity_id": ["light.lämp"]}) is None


def test_valid_modern_membership_takes_precedence_over_legacy() -> None:
    """Keep modern membership authoritative when both forms are valid."""
    assert normalize_member_entity_ids(
        {
            "group_entities": ["light.modern"],
            "entity_id": ["light.legacy"],
        }
    ) == ["light.modern"]


def test_group_members_attribute_is_not_membership() -> None:
    """Ignore similarly named attributes outside HA's explicit conventions."""
    assert (
        normalize_member_entity_ids({"group_members": ["light.not_supported"]}) is None
    )
