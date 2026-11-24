"""Bound a whole-instance Logbook read before Core loads or encodes its rows."""

from __future__ import annotations

from datetime import datetime
from typing import Any

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
from homeassistant.components.recorder.models import extract_event_type_ids
from homeassistant.helpers.json import json_bytes
from homeassistant.helpers.recorder import session_scope
from sqlalchemy import case, func, literal, select, tuple_
from sqlalchemy.engine import Row
from sqlalchemy.orm import Session
from sqlalchemy.sql.selectable import Subquery

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


def _prime_contexts(
    session: Session,
    source: Subquery,
    processor: EventProcessor,
    rows: list[Row[Any]],
) -> None:
    """Give Core the first earlier row for each context its page can reference."""
    ids = {
        row[pos]
        for row in rows
        for pos in (CONTEXT_ID_BIN_POS, CONTEXT_PARENT_ID_BIN_POS)
        if row[pos] is not None
    }
    if not ids:
        return
    cols = _columns(source)
    rank = func.row_number().over(partition_by=source.c.context_id_bin, order_by=cols)
    origins = (
        select(*cols, rank.label("origin_rank"))
        .where(source.c.context_id_bin.in_(ids), tuple_(*cols) < _key(rows[0]))
        .subquery()
    )
    origin_keys = select(*list(origins.c)[:3]).where(origins.c.origin_rank == 1)
    for origin in session.connection().execute(
        select(source).where(tuple_(*cols).in_(origin_keys))
    ):
        processor.logbook_run.context_lookup[origin[CONTEXT_ID_BIN_POS]] = origin


def _read_rows(
    session: Session, source: Subquery, page: dict[str, Any]
) -> tuple[list[Row[Any]], bool]:
    """Choose a bounded set by indexed identity, before hydrating wide records."""
    cols = _columns(source)
    order = tuple(c.desc() for c in cols) if page["order"] == "newest" else cols
    keys = tuple_(*cols)
    # Probe sizes without bringing the event JSON/attributes into Python. Use
    # a conservative UTF-8 allowance for databases where LENGTH counts chars.
    size = sum(
        (
            func.coalesce(func.length(col), 0)
            for col in (source.c.event_data, source.c.attributes, source.c.state)
        ),
        start=literal(0),
    )
    probe = select(*cols).order_by(*order).limit(RAW_PAGE_ROWS + 1)
    if after := page.get("after"):
        anchor = tuple(after)
        probe = probe.where(
            keys < anchor if page["order"] == "newest" else keys > anchor
        )
    candidates = session.connection().execute(probe).all()
    if not candidates:
        return [], False
    sizes = {
        (stamp, kind, row_id): chars
        for stamp, kind, row_id, chars in session.connection().execute(
            select(*cols, size).where(keys.in_(candidates[:RAW_PAGE_ROWS]))
        )
    }
    chosen: list[RowKey] = []
    used = 0
    for stamp, kind, row_id in candidates[:RAW_PAGE_ROWS]:
        chars = sizes[(stamp, kind, row_id)]
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


def read_logbook_page(
    processor: EventProcessor, start: datetime, end: datetime, page: dict[str, Any]
) -> dict[str, Any]:
    """Use Core's query and renderer, keeping just one bounded database page."""
    with session_scope(hass=processor.hass, read_only=True) as session:
        manager = get_instance(processor.hass).event_type_manager
        types = tuple(
            extract_event_type_ids(manager.get_many(processor.event_types, session))
        )
        source = (
            statement_for_request(start, end, types, filters=processor.filters)
            .order_by(None)
            .subquery()
        )
        rows, more = _read_rows(session, source, page)
        if rows and (page.get("after") or (more and page["order"] == "newest")):
            _prime_contexts(session, source, processor, rows)
        rendered = [(row, processor.humanify([row])) for row in rows]
    if page["order"] == "newest":
        rendered.reverse()
    return _select_page(rendered, page, more)


def _select_page(
    rendered: list[tuple[Row[Any], list[dict[str, Any]]]],
    page: dict[str, Any],
    more: bool,
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    search = page.get("search", "").lower()

    def matches(entry: dict[str, Any]) -> bool:
        return not search or any(
            search in str(entry.get(k, "")).lower()
            for k in ("name", "message", "entity_id")
        )

    candidates = [
        (row, events[0] if events and matches(events[0]) else None)
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
