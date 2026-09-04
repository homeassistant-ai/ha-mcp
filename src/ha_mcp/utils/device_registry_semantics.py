"""Shared read-side semantics for Home Assistant device-registry rows.

Home Assistant Core 2026.9 added reduced ``ChildDeviceEntry`` rows to
``config/device_registry/list``. A child keeps its own ``area_id`` when set and
otherwise inherits the direct parent device's area. The parent is always a main
device in Core's supported model; this module deliberately does not invent a
recursive hierarchy for malformed external payloads.

The helpers are pure over bounded registry responses. They preserve the first
appearance order, collapse equivalent duplicate mappings, and remove conflicting
identities from the semantic snapshot so no consumer chooses an arbitrary parent
or placement.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# Private marker used only between registry acquisition and the existing public
# device shaper. It never appears in a public response.
EFFECTIVE_AREA_MARKER = "_ha_mcp_effective_area_id"


@dataclass(frozen=True, slots=True)
class DeviceRegistrySnapshot:
    """Validated unique registry rows and their bounded placement relationships."""

    rows: tuple[dict[str, Any], ...]
    by_id: dict[str, dict[str, Any]]
    parent_by_id: dict[str, str | None]
    effective_area_by_id: dict[str, str | None]


def effective_device_area_id(
    device: Mapping[str, Any], devices_by_id: Mapping[str, Mapping[str, Any]]
) -> str | None:
    """Return Core's effective area for one main or child device row.

    A present direct area wins. A child with no direct area inherits exactly one
    level from a valid main parent. Missing parents, malformed ids/areas, a child
    used as a parent, and cycles all resolve to ``None`` rather than guessing.
    """
    direct_area = device.get("area_id")
    if direct_area is not None:
        return direct_area if isinstance(direct_area, str) else None

    parent_id = device.get("parent_device_id")
    if not isinstance(parent_id, str) or not parent_id:
        return None
    parent = devices_by_id.get(parent_id)
    if parent is None:
        return None
    # Core requires a child parent to be a main device. Refuse malformed chains
    # (including cycles) instead of recursively resolving an invented hierarchy.
    if parent.get("parent_device_id") is not None:
        return None
    parent_area = parent.get("area_id")
    return parent_area if isinstance(parent_area, str) else None


def build_device_registry_snapshot(rows: list[Any]) -> DeviceRegistrySnapshot:
    """Build one deterministic semantic snapshot from a registry list response.

    Invalid rows and ids are ignored under the existing partial-read convention.
    Identical duplicate mappings collapse. If the same id names different rows,
    every copy of that identity is excluded so enrichment never picks first/last.
    """
    order: list[str] = []
    by_id: dict[str, dict[str, Any]] = {}
    conflicts: set[str] = set()

    for item in rows:
        if not isinstance(item, dict):
            continue
        device_id = item.get("id")
        if not isinstance(device_id, str) or not device_id:
            continue
        if device_id in conflicts:
            continue
        if device_id not in by_id:
            order.append(device_id)
            by_id[device_id] = dict(item)
            continue
        if by_id[device_id] != item:
            conflicts.add(device_id)
            del by_id[device_id]

    ordered_rows = tuple(by_id[device_id] for device_id in order if device_id in by_id)
    parent_by_id: dict[str, str | None] = {}
    effective_area_by_id: dict[str, str | None] = {}
    for device_id, device in by_id.items():
        parent_id = device.get("parent_device_id")
        parent_by_id[device_id] = (
            parent_id if isinstance(parent_id, str) and parent_id else None
        )
        effective_area_by_id[device_id] = effective_device_area_id(device, by_id)

    return DeviceRegistrySnapshot(
        rows=ordered_rows,
        by_id=by_id,
        parent_by_id=parent_by_id,
        effective_area_by_id=effective_area_by_id,
    )


def annotate_device_rows_with_effective_area(rows: list[Any]) -> list[dict[str, Any]]:
    """Return unique row copies carrying the private effective-area marker."""
    snapshot = build_device_registry_snapshot(rows)
    annotated: list[dict[str, Any]] = []
    for row in snapshot.rows:
        copy = dict(row)
        device_id = copy["id"]
        copy[EFFECTIVE_AREA_MARKER] = snapshot.effective_area_by_id.get(device_id)
        annotated.append(copy)
    return annotated
