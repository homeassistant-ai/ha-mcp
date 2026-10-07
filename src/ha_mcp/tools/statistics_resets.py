"""Keep recorder reset timestamps independent of numeric unit conversion."""

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _reset_groups(
    rows_by_id: dict[str, Any],
    metadata: dict[str, dict[str, Any]],
    query: dict[str, Any],
) -> tuple[dict[tuple[str, str | None], list[str]], set[str]]:
    groups: dict[tuple[str, str | None], list[str]] = {}
    unresolved: set[str] = set()
    for statistic_id, rows in rows_by_id.items():
        if not any(row.get("last_reset") is not None for row in rows):
            continue
        record = metadata.get(statistic_id, {})
        stored = record.get("statistics_unit_of_measurement")
        unit_class = record.get("unit_class")
        if (
            not query.get("units")
            and "display_unit_of_measurement" in record
            and "statistics_unit_of_measurement" in record
            and record["display_unit_of_measurement"] == stored
        ):
            continue
        if isinstance(unit_class, str) and "statistics_unit_of_measurement" in record:
            groups.setdefault((unit_class, stored), []).append(statistic_id)
        else:
            unresolved.add(statistic_id)
    return groups, unresolved


def _replace_resets(
    rows_by_id: dict[str, Any], result: dict[str, Any], ids: list[str]
) -> set[str]:
    unresolved = set()
    for statistic_id in ids:
        resets = {
            row["start"]: row["last_reset"]
            for row in result.get(statistic_id, [])
            if "last_reset" in row
        }
        for row in rows_by_id[statistic_id]:
            if "last_reset" not in row:
                continue
            if row["start"] not in resets:
                unresolved.add(statistic_id)
                break
            row["last_reset"] = resets[row["start"]]
    return unresolved


async def restore_reset_timestamps(
    client: Any,
    rows_by_id: dict[str, Any],
    metadata: dict[str, dict[str, Any]],
    query: dict[str, Any],
) -> list[str]:
    """Read resets in their stored unit to avoid Core converting timestamps.

    Core 2026.9's generic row converter also converts last_reset. A native
    same-unit query avoids that bug without inverse arithmetic or a unit table,
    and remains correct when Core fixes it. Group by native class/stored unit
    because one query's unit option applies to every statistic in that class.
    """
    groups, unresolved = _reset_groups(rows_by_id, metadata, query)
    for (unit_class, stored), ids in groups.items():
        try:
            response = await client.send_websocket_message(
                {
                    **query,
                    "type": "recorder/statistics_during_period",
                    "statistic_ids": ids,
                    "types": ["last_reset"],
                    "units": {unit_class: stored},
                }
            )
            result = response.get("result") if response.get("success") else None
            if not isinstance(result, dict):
                unresolved.update(ids)
                continue
            unresolved.update(_replace_resets(rows_by_id, result, ids))
        except Exception:
            logger.warning("Unconverted reset timestamp lookup failed", exc_info=True)
            unresolved.update(ids)

    for statistic_id in unresolved:
        for row in rows_by_id[statistic_id]:
            row.pop("last_reset", None)
    return [
        f"{statistic_id}: last_reset omitted because its unconverted timestamp "
        "could not be read; Core may apply value-unit conversion to this field."
        for statistic_id in sorted(unresolved)
    ]
