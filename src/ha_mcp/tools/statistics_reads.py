"""The recorder's statistics as Core serves them: rows per period, or one total per statistic."""

from datetime import datetime
from typing import Any

from .core_contract import merge_core_options
from .history_coverage import (
    empty_statistics_warnings,
    statistics_coverage_warnings,
)
from .recorder_errors import raise_recorder_refusal
from .statistics_helpers import (
    _parse_statistic_types,
    converter_records,
    fetch_statistics_metadata,
    format_entity_statistics,
    label_requested_units,
    statistics_unit,
    statistics_warnings,
)

_DEFAULT_LIMIT = 100


async def _statistics_metadata(
    client: Any,
    ids: list[str],
    units: dict[str, Any],
    warnings: list[str],
    *,
    needed: bool,
) -> tuple[dict[str, dict[str, Any]], str | None, dict[str, dict[str, Any]]]:
    converted = await converter_records(client, ids, units, warnings, needed=needed)
    if converted is not None:
        metadata = {
            key: {
                k: v
                for k, v in row.items()
                if k not in {"output_unit_of_measurement", "conversion_unit_class"}
            }
            for key, row in converted.items()
        }
        return metadata, None, converted
    metadata, failure = await fetch_statistics_metadata(client, ids)
    return metadata, failure, {}


async def fetch_statistics(
    client: Any,
    entity_id_list: list[str],
    start_dt: datetime,
    end_dt: datetime,
    period: str,
    statistic_types: str | list[str] | None,
    limit: int | None,
    offset: int | None,
    core_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute the recorder/statistics_during_period WebSocket call.

    Returns the unwrapped statistics dict; the caller is responsible for projection
    and wrapping with ``add_timezone_metadata``.
    """
    effective_limit = limit if limit is not None else _DEFAULT_LIMIT
    effective_offset = offset if offset is not None else 0

    stat_types_list = _parse_statistic_types(statistic_types)

    command_params: dict[str, Any] = {
        "start_time": start_dt.isoformat(),
        "end_time": end_dt.isoformat(),
        "statistic_ids": entity_id_list,
        "period": period,
    }
    command_params = merge_core_options(
        {**command_params, "types": stat_types_list}, core_options
    )
    if stat_types_list is None:
        command_params.pop("types")

    response = await client.send_websocket_message(
        {"type": "recorder/statistics_during_period", **command_params}
    )

    if not response.get("success"):
        raise_recorder_refusal("statistics", response, entity_id_list)

    result_data = response.get("result", {})
    units = command_params.get("units", {})
    warnings: list[str] = []
    # Rows use the converter's answer only to label units that were asked for.
    metadata, metadata_failure, converted = await _statistics_metadata(
        client, entity_id_list, units, warnings, needed=bool(units)
    )
    entities_statistics = format_entity_statistics(
        result_data,
        entity_id_list,
        period,
        effective_offset,
        effective_limit,
        metadata,
        metadata_failure,
    )
    if units:
        label_requested_units(entities_statistics, converted, units)
    all_stat_types = (
        stat_types_list
        if stat_types_list is not None
        else sorted(
            {
                key
                for rows in result_data.values()
                for row in rows
                for key in row
                if key not in {"start", "end"}
            }
        )
    )

    statistics_data: dict[str, Any] = {
        "success": True,
        "source": "statistics",
        "entities": entities_statistics,
        "period_type": period,
        "time_range": {
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
        },
        "statistic_types": all_stat_types,
        "query_params": {
            "statistic_types": stat_types_list,
            "limit": effective_limit,
            "offset": effective_offset,
        },
    }

    # An unknown unit says nothing about a statistic that returned no rows.
    warnings += statistics_warnings(
        [entity for entity in entities_statistics if entity["total_count"]]
    ) + await statistics_coverage_warnings(
        client,
        result_data,
        entity_id_list,
        metadata,
        metadata_failure,
        period,
        start_dt.timestamp(),
    )
    if warnings:
        statistics_data["warnings"] = warnings
    return statistics_data


def _display_units(
    record: dict[str, Any] | None,
    converted: dict[str, Any] | None,
    requested: dict[str, Any] | None,
) -> dict[str, Any]:
    """Core's ``units`` option: the caller's units over the display unit Core names.

    Core converts a total to the display unit of the moment. Asking for the
    unit just read keeps the value and its label together when that unit
    changes between the two reads. The unit class is Core's converter choice
    when the component gives it, and the metadata's otherwise.
    """
    units = dict(requested or {})
    if converted:
        unit_class = converted.get("conversion_unit_class")
        unit = converted.get("output_unit_of_measurement")
    else:
        unit_class = (record or {}).get("unit_class")
        unit = (record or {}).get("display_unit_of_measurement")
    if unit_class and unit:
        units.setdefault(unit_class, unit)
    return {"units": units} if units else {}


async def fetch_statistics_totals(
    client: Any,
    statistic_ids: list[str],
    start_dt: datetime,
    end_dt: datetime,
    statistic_types: str | list[str] | None,
    core_options: dict[str, Any] | None,
) -> dict[str, Any]:
    """Ask Core for each statistic's totals over its selected period."""
    time_range = {"start": start_dt.isoformat(), "end": end_dt.isoformat()}
    types = _parse_statistic_types(statistic_types)
    params = merge_core_options(
        {
            "statistic_id": None,
            "fixed_period": {
                "start_time": time_range["start"],
                "end_time": time_range["end"],
            },
            "types": types,
        },
        core_options,
    )
    native_period = {
        key: params[key] for key in ("calendar", "rolling_window") if key in params
    }
    if native_period:
        params.pop("fixed_period")
    if types is None:
        params.pop("types")
    requested = params.get("units")
    warnings: list[str] = []
    metadata, metadata_failure, converted = await _statistics_metadata(
        client, statistic_ids, requested or {}, warnings, needed=True
    )
    entities = []
    for statistic_id in statistic_ids:
        record = metadata.get(statistic_id)
        response = await client.send_websocket_message(
            {
                **params,
                **_display_units(record, converted.get(statistic_id), requested),
                "type": "recorder/statistic_during_period",
                "statistic_id": statistic_id,
            }
        )
        if not response.get("success"):
            raise_recorder_refusal("statistics", response, [statistic_id])
        entities.append(
            {
                "entity_id": statistic_id,
                "summary": response.get("result") or {},
                **statistics_unit(record, metadata_failure),
                "statistics_metadata": record,
            }
        )
    # From the read that gave each request its unit: a later one could name another.
    label_requested_units(entities, converted, requested or {})
    empty = [
        entity["entity_id"]
        for entity in entities
        if all(value is None for value in entity["summary"].values())
    ]
    data: dict[str, Any] = {
        "success": True,
        "source": "statistics",
        "entities": entities,
        "time_range": native_period or time_range,
        "query_params": {"statistic_types": types, "summary": True},
    }
    warnings += statistics_warnings(
        [entity for entity in entities if entity["entity_id"] not in empty]
    ) + await empty_statistics_warnings(
        client,
        empty,
        metadata,
        metadata_failure,
        # A total is also null for a value the statistic does not keep.
        nothing="Core returned no values in this window for",
    )
    if warnings:
        data["warnings"] = warnings
    return data
