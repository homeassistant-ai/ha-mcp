"""Recorder metadata and output units, using the running Core's display rules."""

import logging
from typing import Any

from .response_helpers import build_pagination_metadata

logger = logging.getLogger(__name__)


async def fetch_statistics_metadata(
    client: Any, statistic_ids: list[str]
) -> tuple[dict[str, dict[str, Any]], str | None]:
    """Read native metadata without making the values query depend on its availability."""
    if not statistic_ids:
        return {}, None
    try:
        response = await client.send_websocket_message(
            {"type": "recorder/get_statistics_metadata", "statistic_ids": statistic_ids}
        )
        if not response.get("success"):
            return {}, "statistics_metadata_unavailable"
        records = response.get("result")
        if not isinstance(records, list):
            return {}, "statistics_metadata_unavailable"
        return {
            record["statistic_id"]: record
            for record in records
            if isinstance(record, dict) and isinstance(record.get("statistic_id"), str)
        }, None
    except Exception:
        # Metadata is supplementary: preserve usable rows and explicitly leave
        # their units unresolved. Never silently substitute current state units.
        logger.warning("Recorder statistics metadata lookup failed", exc_info=True)
        return {}, "statistics_metadata_unavailable"


def statistics_unit(
    metadata: dict[str, Any] | None, failure: str | None = None
) -> dict[str, Any]:
    """Label default statistics output with Core's resolved display unit.

    Core's statistics_during_period converts stored values to display units
    when no explicit units are requested. get_statistics_metadata calls Core's
    get_display_unit, including its handling of missing entities and invalid
    state units. Stored units alone cannot establish the output unit.
    """
    unknown = {"unit_of_measurement": None, "unit_source": "unknown"}
    if failure or metadata is None:
        return {**unknown, "unit_reason": failure or "statistics_metadata_missing"}
    if "display_unit_of_measurement" not in metadata:
        return {**unknown, "unit_reason": "display_unit_not_reported"}
    unit = metadata["display_unit_of_measurement"]
    if unit is not None and not isinstance(unit, str):
        return {**unknown, "unit_reason": "invalid_display_unit_metadata"}
    result = {"unit_of_measurement": unit, "unit_source": "recorder_metadata"}
    if unit is None:
        if metadata.get("statistics_unit_of_measurement") is not None:
            return {**unknown, "unit_reason": "display_unit_not_reported"}
        result["unit_reason"] = "statistics_are_unitless"
    return result


def format_entity_statistics(
    result_data: dict[str, Any],
    entity_ids: list[str],
    period: str,
    offset: int,
    limit: int,
    metadata: dict[str, dict[str, Any]],
    metadata_failure: str | None,
) -> list[dict[str, Any]]:
    """Preserve Core's row fields and attach units independently of pagination."""
    entities = []
    for entity_id in entity_ids:
        rows = result_data.get(entity_id, [])
        page = rows[offset : offset + limit]
        record = metadata.get(entity_id)
        entities.append(
            {
                "entity_id": entity_id,
                "period": period,
                "statistics": page,
                **statistics_unit(record, metadata_failure),
                "statistics_metadata": record,
                **build_pagination_metadata(
                    total_count=len(rows), offset=offset, limit=limit, count=len(page)
                ),
            }
        )
    return entities


def statistics_warnings(entities: list[dict[str, Any]]) -> list[str]:
    """Make unresolved units visible to callers without discarding numeric data."""
    return [
        f"{entity['entity_id']}: statistics output unit is unknown "
        f"({entity['unit_reason']}); do not infer it from current state attributes."
        for entity in entities
        if entity["unit_source"] == "unknown"
    ]


async def resolve_requested_units(
    client: Any, entities: list[dict[str, Any]], units: dict[str, str]
) -> None:
    """Resolve Core's actual converter or label explicit-unit output as unknown."""
    from ..client.websocket_client import get_websocket_client
    from .component_api import component_supports, get_component_caps, invalidate_caps, is_unknown_command

    records: dict[str, dict[str, Any]] = {}
    try:
        caps = await get_component_caps(client)
        if component_supports(caps, "core_contract"):
            ws = await get_websocket_client(url=client.base_url, token=client.token, verify_ssl=getattr(client, "verify_ssl", None))
            response = await ws.send_command("ha_mcp_tools/statistics_units", statistic_ids=[e["entity_id"] for e in entities], units=units)
            records = {r["statistic_id"]: r for r in response["result"]["records"]}
    except Exception as exc:
        if is_unknown_command(exc):
            invalidate_caps(client)
        logger.warning("Explicit statistics unit resolution failed", exc_info=True)
    for entity in entities:
        record = records.get(entity["entity_id"])
        if record is not None and "output_unit_of_measurement" in record:
            entity["unit_of_measurement"] = record["output_unit_of_measurement"]
            entity["unit_source"] = "core_converter"
            entity.pop("unit_reason", None)
        else:
            entity.update(unit_of_measurement=None, unit_source="unknown", unit_reason="requested_unit_resolution_unavailable")
