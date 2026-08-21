"""Tests for generic entity-membership normalization."""

from ha_mcp.tools.smart_search._entities import _redact_hidden_memberships
from ha_mcp.tools.tools_search import _add_membership_fields
from ha_mcp.utils.entity_membership import normalize_member_entity_ids


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


def test_self_reference_is_not_recursively_expanded() -> None:
    """Normalize explicit members without attempting recursive expansion."""
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


def test_visibility_denied_member_withholds_ids_but_keeps_group_signal() -> None:
    """Withhold the full member list if any member is not visible."""
    attributes = {"group_entities": ["light.visible", "light.denied"]}
    entities = [{"entity_id": "light.group", "attributes": attributes}]

    redacted = _redact_hidden_memberships(entities, {"light.denied"})
    record: dict = {}
    _add_membership_fields(
        record,
        redacted[0]["attributes"],
        ("is_group", "member_entity_ids"),
    )

    assert record == {"is_group": True}
    assert entities[0]["attributes"] == attributes


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
