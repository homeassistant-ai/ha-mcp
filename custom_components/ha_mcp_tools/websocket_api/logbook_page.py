"""Bound a whole-instance Logbook read before Core loads or encodes its rows."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from heapq import nlargest, nsmallest
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
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.db_schema import Events, States
from homeassistant.components.recorder.models import extract_event_type_ids
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
    # Events and states have separate primary-key sequences. Preserve Core's
    # event-then-state union order at a tie and disambiguate with the primary key.
    return (row[TIME_FIRED_TS_POS], int(row[EVENT_TYPE_POS] is None), row[ROW_ID_POS])


def _columns(source: Subquery) -> tuple[Any, Any, Any]:
    c = source.c
    return c.time_fired_ts, case((c.event_type.is_(None), 1), else_=0), c.row_id


def _branches(source: Subquery) -> Sequence[Select[Any]]:
    """Core's whole-instance Logbook statement is the UNION of events and states."""
    union = cast("CompoundSelect[Any]", source.element)
    return cast("Sequence[Select[Any]]", union.selects)


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
    # Probe sizes without bringing the event JSON/attributes into Python. Use
    # a conservative UTF-8 allowance for databases where LENGTH counts chars.
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
            select(*cols, size).where(keys.in_(candidates[:RAW_PAGE_ROWS]))
        )
    }
    chosen: list[RowKey] = []
    used = 0
    for stamp, kind, row_id in candidates[:RAW_PAGE_ROWS]:
        # A purge can remove a probed row before its size is read.
        if (chars := sizes.get((stamp, kind, row_id))) is None:
            continue
        if chosen and used + chars * 4 > PAGE_BYTES:
            break
        chosen.append((stamp, kind, row_id))
        used += chars * 4
    # Core always humanifies in chronological order, including newest-first pages.
    rows = list(
        session.connection().execute(
            select(source).where(keys.in_(chosen)).order_by(*cols)
        )
    )
    return rows, len(chosen) < len(candidates)


def _source(
    processor: EventProcessor,
    period: tuple[datetime, datetime],
    types: tuple[int, ...],
    page: dict[str, Any] | None = None,
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
    source: Subquery = (
        statement_for_request(start, end, types, filters=processor.filters)
        .order_by(None)
        .subquery()
    )
    return source


def read_logbook_page(
    processor: EventProcessor, start: datetime, end: datetime, page: dict[str, Any]
) -> dict[str, Any]:
    """Use Core's query and renderer, keeping just one bounded database page."""
    with session_scope(hass=processor.hass, read_only=True) as session:
        manager = get_instance(processor.hass).event_type_manager
        types = tuple(
            extract_event_type_ids(manager.get_many(processor.event_types, session))
        )
        period = (start, end)
        source = _source(processor, period, types)
        if bounds := page.get("count_range"):
            return _count_matches(
                session, processor, source, bounds, page, types, period
            )
        if "keys" in page:
            return _render_keys(session, processor, source, page)
        probe = _source(processor, period, types, page) if page.get("after") else source
        rows, more = _read_rows(session, probe, page, types)
        if rows and (page.get("after") or (more and page["order"] == "newest")):
            for context, origin in _context_origins(
                session, source, rows, _key(rows[0])
            ).items():
                processor.logbook_run.context_lookup[context] = origin
        rendered = [(row, processor.humanify([row])) for row in rows]
    if page["order"] == "newest":
        rendered.reverse()
    return _select_page(rendered, page, more)


def _matches(entry: dict[str, Any], search: str) -> bool:
    if not search:
        return True
    name = entry.get("name") or ""
    message = entry.get("message") or ""
    return (
        search in f"{name} {message}".strip().lower()
        or search in str(entry.get("entity_id", "")).lower()
    )


def _count_matches(
    session: Session,
    processor: EventProcessor,
    source: Subquery,
    bounds: list[float],
    page: dict[str, Any],
    event_types: tuple[int, ...],
    period: tuple[datetime, datetime],
) -> dict[str, Any]:
    """Keep at most one public page of identities while computing its exact count."""
    stamp = source.c.time_fired_ts
    stmt = select(source).where(stamp >= bounds[0], stamp < bounds[1])
    newest = page["order"] == "newest"
    if after := page.get("after"):
        keys = tuple_(*_columns(source))
        stmt = stmt.where(keys < tuple(after) if newest else keys > tuple(after))
    needle = page.get("search", "").lower()
    total = 0
    take = page.get("collect_keys", 0)
    skip = page["skip"]
    with session.connection().execute(stmt.execution_options(yield_per=4)) as result:
        if not take:
            for rows in result.partitions(4):
                processor.switch_to_live()
                total += sum(
                    _matches(entry, needle) for entry in processor.humanify(rows)
                )
            return {"entries": [], "total": total, "keys": []}

        def matches() -> Iterator[RowKey]:
            nonlocal total
            for rows in result.partitions(4):
                processor.switch_to_live()
                for row in rows:
                    events = processor.humanify([row])
                    if events and _matches(events[0], needle):
                        total += 1
                        yield _key(row)

        # A deep numeric offset skips whole hours by count. Resolve only
        # its boundary hour separately, without an offset-sized heap.
        select_keys = nlargest if newest else nsmallest
        selected = select_keys(min(skip + take, 500), matches())
    if skip < total and min(skip + take, total) > len(selected):
        selected = _deep_keys(session, processor, bounds, page, event_types, period)
    else:
        selected = selected[skip : skip + take]
    return {"entries": [], "total": total, "keys": selected}


def _deep_keys(
    session: Session,
    processor: EventProcessor,
    bounds: list[float],
    page: dict[str, Any],
    event_types: tuple[int, ...],
    period: tuple[datetime, datetime],
) -> list[RowKey]:
    """Only the hour containing a deep numeric offset needs a second pass."""
    skip = page["skip"]
    selected: list[RowKey] = []
    needle = page.get("search", "").lower()
    while True:
        probe = _source(processor, period, event_types, page)
        rows, more = _read_rows(session, probe, page, event_types)
        if not rows:
            break
        if page["order"] == "newest":
            rows.reverse()
        processor.switch_to_live()
        for row in rows:
            key = _key(row)
            if bounds[0] <= key[0] < bounds[1]:
                events = processor.humanify([row])
                if events and _matches(events[0], needle):
                    if skip:
                        skip -= 1
                    else:
                        selected.append(key)
                        if len(selected) == page["collect_keys"]:
                            return selected
        if not more:
            break
        page = {**page, "after": _key(rows[-1])}
    return selected


def _render_keys(
    session: Session, processor: EventProcessor, source: Subquery, page: dict[str, Any]
) -> dict[str, Any]:
    """Hydrate only the requested matches, keeping the public page byte-bounded."""
    newest = page["order"] == "newest"
    pending = sorted({tuple(key) for key in page["keys"]}, reverse=newest)
    entries: list[dict[str, Any]] = []
    used = 0
    after = None
    while pending:
        rows, _ = _hydrate_rows(session, source, pending)
        if not rows:
            # Recorder can purge rows between the count and render.
            pending = pending[RAW_PAGE_ROWS:]
            continue
        processor.logbook_run.event_cache.clear()
        origins = _context_origins(session, source, rows, _key(rows[-1]))
        # Selected matches can have earlier context origins between them.
        for row in reversed(rows) if newest else rows:
            lookup = processor.logbook_run.context_lookup
            lookup.clear()
            for pos in (CONTEXT_ID_BIN_POS, CONTEXT_PARENT_ID_BIN_POS):
                if (origin := origins.get(row[pos])) is not None and _key(
                    origin
                ) < _key(row):
                    lookup[row[pos]] = origin
            for entry in processor.humanify([row]):
                size = len(json_bytes(entry))
                if entries and used + size > PAGE_BYTES:
                    return {"entries": entries, "after": after}
                entries.append(entry)
                used += size
            after = _key(row)
        last = _key(rows[0] if newest else rows[-1])
        pending = [key for key in pending if (key < last if newest else key > last)]
    return {"entries": entries, "after": after}


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
    total = None if more else sum(entry is not None for _, entry in candidates)
    skip = page["skip"]
    matched = used = 0
    after = None
    for row, entry in candidates:
        if entry is not None:
            size = len(json_bytes(entry))
            if not skip and (
                len(entries) == page["limit"] or (entries and used + size > PAGE_BYTES)
            ):
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
    }
