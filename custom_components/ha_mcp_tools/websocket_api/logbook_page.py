"""Bound a Logbook read before Core loads or encodes its rows."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from homeassistant.components.logbook.models import (
    CONTEXT_ID_BIN_POS,
    CONTEXT_PARENT_ID_BIN_POS,
    EVENT_TYPE_POS,
    ROW_ID_POS,
    TIME_FIRED_TS_POS,
)
from homeassistant.components.logbook.processor import EventProcessor
from homeassistant.components.logbook.queries import statement_for_request
from homeassistant.components.logbook.queries.action_calls import action_calls_stmt
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.db_schema import Events, States
from homeassistant.components.recorder.models import (
    extract_event_type_ids,
    extract_metadata_ids,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.json import json_bytes
from homeassistant.helpers.recorder import session_scope
from sqlalchemy import case, func, literal, select, tuple_
from sqlalchemy.engine import Row
from sqlalchemy.orm import Session
from sqlalchemy.sql.selectable import CompoundSelect, Select, Subquery

# Bound both work per call (including non-display rows) and the wire response.
RAW_PAGE_ROWS = 64
PAGE_BYTES = 512 * 1024
type RowKey = tuple[float, int, int]


def _key(row: Row[Any]) -> RowKey:
    # Core orders by time only. HA-MCP breaks ties by kind and primary key.
    return (row[TIME_FIRED_TS_POS], int(row[EVENT_TYPE_POS] is None), row[ROW_ID_POS])


def _columns(source: Subquery) -> tuple[Any, Any, Any]:
    c = source.c
    return c.time_fired_ts, case((c.event_type.is_(None), 1), else_=0), c.row_id


def _branches(source: Subquery) -> Sequence[Select[Any]]:
    """Validate Core's visible branches and optional context-only suffix.

    Two branches are Core's whole-instance and context statements, three its
    device and action-call ones, four its entity and entity-plus-device ones.
    """
    union = source.element
    if isinstance(union, CompoundSelect):
        branches = union.selects
        shape = []
        for branch in branches:
            if not isinstance(branch, Select):
                break
            cols = branch.selected_columns
            pk = cols.get("row_id")
            flag = cols.get("context_only")
            if pk is None or flag is None:
                break
            state = pk.shares_lineage(States.__table__.c.state_id)
            if not state and not pk.shares_lineage(Events.__table__.c.event_id):
                break
            shape.append(
                (state, getattr(getattr(flag, "element", None), "value", "unsupported"))
            )
        if len(shape) == len(branches) and shape in (
            [(False, None), (True, None)],
            [(False, None), (False, "1"), (True, "1")],
            [(False, None), (True, None), (False, "1"), (True, "1")],
        ):
            return cast("Sequence[Select[Any]]", branches)
    raise HomeAssistantError("Unsupported Core logbook statement shape for paging")


def _context_origins(
    session: Session,
    source: Subquery,
    rows: list[Row[Any]],
    before: RowKey,
) -> dict[bytes, Row[Any]]:
    """Give Core the first earlier row for each context its page can reference."""
    ids = {
        row[pos]
        for row in rows
        for pos in (CONTEXT_ID_BIN_POS, CONTEXT_PARENT_ID_BIN_POS)
        if row[pos] is not None
    }
    if not ids:
        return {}
    # Rank only the requested context IDs in each native event/state branch.
    # Ranking the combined UNION makes SQLite scan unrelated Recorder events.
    origins: dict[bytes, Row[Any]] = {}
    for branch in _branches(source):
        c = branch.selected_columns
        kind = int(c.row_id.shares_lineage(States.__table__.c.state_id))
        table = States if kind else Events
        lookup = table.__table__.alias()
        pk = lookup.c.state_id if kind else lookup.c.event_id
        stamp = lookup.c.last_updated_ts if kind else lookup.c.time_fired_ts
        # Drive the lookup from Recorder's context index. Check eligibility
        # with Core's query by primary key, including all installation filters.
        eligible = (
            branch.with_only_columns(literal(1))
            .where(
                c.row_id == pk,
                tuple_(c.time_fired_ts, literal(kind), c.row_id) < before,
            )
            .correlate(lookup)
            .exists()
        )
        rank = func.row_number().over(
            partition_by=lookup.c.context_id_bin, order_by=(stamp, pk)
        )
        ranked = (
            select(pk.label("id"), rank.label("rank"))
            .where(lookup.c.context_id_bin.in_(ids), eligible)
            .subquery()
        )
        origin_ids = select(ranked.c.id).where(ranked.c.rank == 1)
        query = (
            select(*branch.selected_columns)
            .select_from(*branch.get_final_froms())
            .where(c.row_id.in_(origin_ids))
        )
        for row in session.connection().execute(query):
            context = row[CONTEXT_ID_BIN_POS]
            if context not in origins or _key(row) < _key(origins[context]):
                origins[context] = row
    return origins


def _read_rows(
    session: Session,
    source: Subquery,
    page: dict[str, Any],
    event_types: tuple[int, ...],
) -> tuple[list[Row[Any]], bool]:
    """Choose a bounded set by indexed identity, before hydrating wide records."""
    newest = page["order"] == "newest"
    candidates: list[RowKey] = []
    for branch in _branches(source):
        c = branch.selected_columns
        kind = int(c.row_id.shares_lineage(States.__table__.c.state_id))
        cols = (c.time_fired_ts, c.row_id)
        if c.context_only.element.value is not None:
            continue
        query = branch.with_only_columns(*cols)
        if after := page.get("after"):
            stamp, anchor_kind, row_id = after
            if kind == anchor_kind:
                pair = tuple_(*cols)
                query = query.where(
                    pair < (stamp, row_id) if newest else pair > (stamp, row_id)
                )
            elif kind < anchor_kind if newest else kind > anchor_kind:
                query = query.where(
                    c.time_fired_ts <= stamp if newest else c.time_fired_ts >= stamp
                )
            else:
                query = query.where(
                    c.time_fired_ts < stamp if newest else c.time_fired_ts > stamp
                )
        order = tuple(col.desc() for col in cols) if newest else cols
        probe = query.order_by(*order).limit(RAW_PAGE_ROWS + 1)
        # Recorder indexes events by (event_type_id, time_fired_ts). A single
        # IN across types makes SQLite sort the entire window before LIMIT.
        probes = (
            [probe]
            if kind
            else [probe.where(Events.event_type_id == t) for t in event_types]
        )
        for statement in probes:
            for stamp, row_id in session.connection().execute(statement):
                candidates.append((stamp, kind, row_id))
    candidates.sort(reverse=newest)
    return _hydrate_rows(session, source, candidates)


def _hydrate_rows(
    session: Session, source: Subquery, candidates: list[RowKey]
) -> tuple[list[Row[Any]], bool]:
    cols = _columns(source)
    keys = tuple_(*cols)
    if not candidates:
        return [], False
    # Probe sizes without bringing the event JSON/attributes into Python.
    # LENGTH may count characters, so pages are budgeted at four bytes each.
    size = sum(
        (
            func.coalesce(func.length(col), 0)
            for col in (source.c.event_data, source.c.attributes, source.c.state)
        ),
        start=literal(0),
    )
    sizes = {
        (stamp, kind, row_id): chars
        for stamp, kind, row_id, chars in session.connection().execute(
            select(*cols, size).where(
                source.c.context_only.is_(None), keys.in_(candidates[:RAW_PAGE_ROWS])
            )
        )
    }
    chosen: list[RowKey] = []
    used = 0
    for stamp, kind, row_id in candidates[:RAW_PAGE_ROWS]:
        # A purge can remove a probed row before its size is read.
        if (chars := sizes.get((stamp, kind, row_id))) is None:
            continue
        if chars > PAGE_BYTES:
            if chosen:
                break
            raise HomeAssistantError(
                "A single raw Logbook row exceeds the HA-MCP page size budget"
            )
        if chosen and used + chars * 4 > PAGE_BYTES:
            break
        chosen.append((stamp, kind, row_id))
        used += chars * 4
    # Core always humanifies in chronological order, including newest-first pages.
    rows = list(
        session.connection().execute(
            select(source)
            .where(source.c.context_only.is_(None), keys.in_(chosen))
            .order_by(*cols)
        )
    )
    return rows, len(chosen) < len(candidates)


def _source(
    processor: EventProcessor,
    period: tuple[datetime, datetime],
    types: tuple[int, ...],
    page: dict[str, Any] | None = None,
    metadata_ids: list[int] | None = None,
) -> Subquery:
    start, end = period
    if page and (after := page.get("after")):
        anchor = datetime.fromtimestamp(after[0], UTC)
        # Core uses open bounds. Keep every row at the cursor timestamp so the
        # row kind and primary key can break ties inside each native branch.
        if page["order"] == "newest":
            end = min(end, anchor + timedelta(microseconds=1))
        else:
            start = max(start, anchor - timedelta(microseconds=1))
    statement = (
        action_calls_stmt(start.timestamp(), end.timestamp(), types)
        if processor.action_calls
        else statement_for_request(
            start,
            end,
            types,
            processor.entity_ids,
            metadata_ids,
            processor.device_ids,
            processor.filters,
            processor.context_id,
        )
    )
    # These three/four-branch statements contain a CTE. Nested for paging,
    # Core's cached lambda would run with an earlier request's bind values.
    with_context = (
        processor.entity_ids or processor.device_ids or processor.action_calls
    )
    bound = statement.spoil() if with_context else statement
    source: Subquery = bound.order_by(None).subquery()
    return source


def read_logbook_page(
    processor: EventProcessor, start: datetime, end: datetime, page: dict[str, Any]
) -> dict[str, Any]:
    """Use Core's query and renderer, keeping just one bounded database page."""
    with session_scope(hass=processor.hass, read_only=True) as session:
        instance = get_instance(processor.hass)
        manager = instance.event_type_manager
        types = tuple(
            extract_event_type_ids(manager.get_many(processor.event_types, session))
        )
        period = (start, end)
        metadata = (
            extract_metadata_ids(
                instance.states_meta_manager.get_many(
                    processor.entity_ids, session, False
                )
            )
            if processor.entity_ids
            else None
        )
        source = _source(processor, period, types, metadata_ids=metadata)
        probe = (
            _source(processor, period, types, page, metadata)
            if page.get("after")
            else source
        )
        rows, more = _read_rows(session, probe, page, types)
        origins = {}
        # A page holds no context-only rows, so what caused its rows is looked up.
        if rows and (
            page.get("after")
            or more
            or processor.entity_ids
            or processor.device_ids
            or processor.action_calls
        ):
            origins = _context_origins(session, source, rows, _key(rows[-1]))
        # Core's get_events does the same: a filtered query drops the parent
        # call_service rows that name the user.
        parent_users = (
            processor._fetch_parent_user_ids(session, rows, instance.max_bind_vars)
            if processor.entity_ids or processor.device_ids
            else None
        )
        rendered = []
        for row in rows:
            for pos in (CONTEXT_ID_BIN_POS, CONTEXT_PARENT_ID_BIN_POS):
                if (origin := origins.get(row[pos])) is not None and _key(
                    origin
                ) < _key(row):
                    processor.logbook_run.context_lookup.setdefault(row[pos], origin)
            rendered.append((row, processor.humanify([row], parent_users)))
    if page["order"] == "newest":
        rendered.reverse()
    return _select_page(rendered, page, more)


def _matches(entry: dict[str, Any], search: str) -> bool:
    if not search:
        return True
    name = entry.get("name") or ""
    message = entry.get("message") or ""
    # An action-call row has neither; its action is domain.service.
    action = f"{entry.get('domain') or ''}.{entry.get('service') or ''}"
    return (
        search in f"{name} {message}".strip().lower()
        or search in str(entry.get("entity_id") or "").lower()
        or ("service" in entry and search in action.lower())
    )


def _select_page(
    rendered: list[tuple[Row[Any], list[dict[str, Any]]]],
    page: dict[str, Any],
    more: bool,
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    search = page.get("search", "").lower()

    candidates = [
        (row, events[0] if events and _matches(events[0], search) else None)
        for row, events in rendered
    ]
    batch_count = sum(entry is not None for _, entry in candidates)
    total = None if more else batch_count
    if page.get("count_only"):
        return {
            "entries": [],
            "matched": batch_count,
            "total": total,
            "skip": 0,
            "after": _key(candidates[-1][0]) if more and candidates else None,
        }
    skip = page["skip"]
    matched = 0
    used = page.get("used_bytes", 0)
    byte_limited = False
    after = None
    for row, entry in candidates:
        if entry is not None:
            size = len(json_bytes(entry))
            if not skip and size > PAGE_BYTES:
                raise HomeAssistantError(
                    "A single Logbook row exceeds the HA-MCP page byte budget"
                )
            byte_limited = bool(not skip and used and used + size > PAGE_BYTES)
            if not skip and (len(entries) == page["limit"] or byte_limited):
                after = after or page.get("after")
                more = True
                break
            matched += 1
            if skip:
                skip -= 1
            else:
                entries.append(entry)
                used += size
        after = _key(row)
    return {
        "entries": entries,
        "matched": matched,
        "total": total,
        "skip": skip,
        "after": after if more else None,
        "used_bytes": used,
        "byte_limited": byte_limited,
    }
