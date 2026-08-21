"""Normalize explicit Home Assistant entity membership attributes."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any


def normalize_member_entity_ids(
    attributes: Mapping[str, Any] | None,
) -> list[str] | None:
    """Return deterministic member IDs exposed by Home Assistant, if valid.

    ``group_entities`` is Home Assistant's current group capability.  The
    historical group implementations expose the same information through
    ``entity_id``.  Strings are deliberately rejected: several non-group
    entities use a scalar ``entity_id`` attribute for an unrelated reference.
    """
    if not isinstance(attributes, Mapping):
        return None

    for key in ("group_entities", "entity_id"):
        raw = attributes.get(key)
        if isinstance(raw, (str, bytes, bytearray, Mapping)):
            continue
        if not isinstance(raw, Collection):
            continue

        members: set[str] = set()
        valid = True
        for value in raw:
            if not _is_entity_id(value):
                valid = False
                break
            members.add(value)
        if valid:
            return sorted(members)
    return None


def _is_entity_id(value: Any) -> bool:
    """Apply Home Assistant's stable ``domain.object_id`` shape conservatively."""
    if not isinstance(value, str) or value.count(".") != 1:
        return False
    domain, object_id = value.split(".", 1)
    return bool(
        domain
        and object_id
        and value == value.lower()
        and all(
            char in "abcdefghijklmnopqrstuvwxyz0123456789_"
            for char in domain + object_id
        )
    )
