"""Unit tests for the component-side ``ha_mcp_tools/trace_context`` command.

The command reads Core's live trace store in process, matches a run's own
context or a triggering parent context, and returns Core's extended trace dict.
These tests use the real Home Assistant fixture and Core's own trace objects so
selection, ordering and JSON encoding match what the command serves live.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant.components.automation.trace import AutomationTrace
from homeassistant.components.script.trace import ScriptTrace
from homeassistant.components.trace.const import DATA_TRACE, DATA_TRACE_STORE
from homeassistant.components.trace.models import TraceBuckets
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers.trace import TraceElement
from homeassistant.util.limited_size_dict import LimitedSizeDict

from custom_components.ha_mcp_tools.websocket_api import trace_context
from custom_components.ha_mcp_tools.websocket_api.schemas import (
    _trace_context_schema,
)

_START = datetime(2026, 11, 8, 16, 0, tzinfo=UTC)


def _trace(
    *,
    run_id: str,
    context_id: str,
    parent_id: str | None = None,
    not_triggered: bool = False,
    start: datetime = _START,
    trigger_variables: dict[str, Any] | None = None,
    cls: type[AutomationTrace] = AutomationTrace,
) -> AutomationTrace:
    trace = cls(
        "uid", {}, None, Context(id=context_id, parent_id=parent_id)
    )
    trace.run_id = run_id
    trace.not_triggered = not_triggered
    trace._timestamp_start = start
    if trigger_variables is not None:
        trace.set_trace({"trigger/0": [TraceElement(trigger_variables, "trigger/0")]})
    trace.finished()
    return trace


def _store(hass: HomeAssistant, key: str, *traces: AutomationTrace) -> None:
    bucket = TraceBuckets(runs=LimitedSizeDict(), not_triggered=LimitedSizeDict())
    for trace in traces:
        target = bucket.not_triggered if trace.not_triggered else bucket.runs
        target[trace.run_id] = trace
    hass.data.setdefault(DATA_TRACE, {})[key] = bucket


@pytest.mark.asyncio
async def test_own_context_match_returns_core_extended_trace(
    hass: HomeAssistant,
) -> None:
    _store(hass, "automation.uid_1", _trace(run_id="r1", context_id="ctx-1"))

    result = trace_context._trace_context_payload(
        hass, {"id": 1, "context_id": "ctx-1"}, available=True
    )

    assert result["match"] == "trace"
    assert result["run_id"] == "r1"
    assert result["domain"] == "automation"
    assert result["item_id"] == "uid_1"
    assert result["trace"]["run_id"] == "r1"
    assert result["scanned"] == 1


@pytest.mark.asyncio
async def test_parent_match_selects_newest_across_real_and_diagnostic(
    hass: HomeAssistant,
) -> None:
    older = _trace(
        run_id="older",
        context_id="own-older",
        parent_id="parent-1",
        start=_START,
    )
    newer = _trace(
        run_id="newer",
        context_id="own-newer",
        parent_id="parent-1",
        start=_START + timedelta(minutes=1),
    )
    diagnostic = _trace(
        run_id="diag",
        context_id="own-diag",
        parent_id="parent-1",
        not_triggered=True,
        start=_START + timedelta(minutes=2),
    )
    _store(hass, "script.uid_1", older, newer, diagnostic)

    result = trace_context._trace_context_payload(
        hass, {"id": 1, "context_id": "parent-1"}, available=True
    )

    assert result["match"] == "parent"
    assert result["run_id"] == "diag"
    assert result["scanned"] == 3
    assert result["other_matches"] == [
        {"domain": "script", "item_id": "uid_1", "run_id": "older", "match": "parent"},
        {"domain": "script", "item_id": "uid_1", "run_id": "newer", "match": "parent"},
    ]


@pytest.mark.asyncio
async def test_missing_start_time_does_not_sort_as_newest(hass: HomeAssistant) -> None:
    missing = _trace(run_id="missing", context_id="own-missing", parent_id="parent-1")
    missing._timestamp_start = None
    valid = _trace(
        run_id="valid",
        context_id="own-valid",
        parent_id="parent-1",
        start=_START,
    )
    _store(hass, "automation.uid_1", missing, valid)

    result = trace_context._trace_context_payload(
        hass, {"id": 1, "context_id": "parent-1"}, available=True
    )

    assert result["run_id"] == "valid"

@pytest.mark.asyncio
async def test_domain_and_item_filters_limit_the_scan(hass: HomeAssistant) -> None:
    automation = _trace(run_id="auto", context_id="own-auto", parent_id="parent-1")
    script = _trace(run_id="script", context_id="own-script", parent_id="parent-1")
    _store(hass, "automation.uid_1", automation)
    _store(hass, "script.uid_2", script)

    result = trace_context._trace_context_payload(
        hass,
        {
            "id": 1,
            "context_id": "parent-1",
            "domain": "script",
            "item_id": "uid_2",
        },
        available=True,
    )

    assert result["run_id"] == "script"
    assert result["domain"] == "script"
    assert result["item_id"] == "uid_2"
    assert result["scanned"] == 1


@pytest.mark.asyncio
async def test_no_match_reports_the_scan_count(hass: HomeAssistant) -> None:
    _store(
        hass,
        "automation.uid_1",
        _trace(run_id="r1", context_id="own-1", parent_id="parent-x"),
    )

    result = trace_context._trace_context_payload(
        hass, {"id": 1, "context_id": "missing"}, available=True
    )

    assert result["match"] is None
    assert result["trace"] is None
    assert result["scanned"] == 1


@pytest.mark.asyncio
async def test_unavailable_store_reports_unavailable(hass: HomeAssistant) -> None:
    result = trace_context._trace_context_payload(
        hass, {"id": 1, "context_id": "ctx-1"}, available=False
    )

    assert result["match"] is None
    assert result["available"] is False
    assert result["scanned"] == 0


@pytest.mark.asyncio
async def test_encoding_uses_cores_extended_json_encoder(
    hass: HomeAssistant,
) -> None:
    """A timedelta in a trigger's ``for`` survives; Core encodes it in trace/get."""
    _store(
        hass,
        "automation.uid_1",
        _trace(
            run_id="r1",
            context_id="ctx-1",
            trigger_variables={"trigger": {"for": timedelta(minutes=5)}},
        ),
    )

    result = trace_context._do_trace_context(
        hass, {"id": 7, "context_id": "ctx-1"}, available=True
    )
    message = json.loads(result.payload)

    assert message["id"] == 7
    trigger = message["result"]["trace"]["trace"]["trigger/0"][0]
    assert trigger["changed_variables"]["trigger"]["for"]["total_seconds"] == 300.0


@pytest.mark.asyncio
async def test_prep_reports_unavailable_without_a_trace_store(
    hass: HomeAssistant,
) -> None:
    result = await trace_context._trace_context_prep(hass, {})

    assert result == {"available": False}


@pytest.mark.asyncio
async def test_prep_restores_saved_traces(hass: HomeAssistant) -> None:
    hass.data[DATA_TRACE] = {}
    hass.data[DATA_TRACE_STORE] = object()
    restore = AsyncMock()

    with patch.object(trace_context, "async_restore_traces", restore):
        result = await trace_context._trace_context_prep(hass, {})

    assert result == {"available": True}
    restore.assert_awaited_once_with(hass)


def test_schema_accepts_the_documented_selectors() -> None:
    schema = vol.Schema(_trace_context_schema())

    assert schema(
        {
            "type": "ha_mcp_tools/trace_context",
            "context_id": "ctx-1",
            "domain": "script",
            "item_id": "uid_1",
        }
    )
    with pytest.raises(vol.Invalid):
        schema(
            {
                "type": "ha_mcp_tools/trace_context",
                "context_id": "ctx-1",
                "domain": "scene",
            }
        )


@pytest.mark.asyncio
async def test_multiple_matches_selects_newest_and_lists_others(
    hass: HomeAssistant,
) -> None:
    """One context can match several runs; the newest is selected and rest listed."""
    shared = "shared-context"
    older_auto = _trace(
        run_id="older-auto",
        context_id="own-older-auto",
        parent_id=shared,
        start=_START,
    )
    newer_script = _trace(
        run_id="newer-script",
        context_id="own-newer-script",
        parent_id=shared,
        start=_START + timedelta(minutes=1),
        cls=ScriptTrace,
    )
    _store(hass, "automation.uid_a", older_auto)
    _store(hass, "script.uid_b", newer_script)

    result = trace_context._trace_context_payload(
        hass, {"id": 1, "context_id": shared}, available=True
    )

    assert result["match"] == "parent"
    assert result["run_id"] == "newer-script"
    assert result["domain"] == "script"
    assert result["item_id"] == "uid_b"
    assert result["scanned"] == 2
    assert result["other_matches"] == [
        {
            "domain": "automation",
            "item_id": "uid_a",
            "run_id": "older-auto",
            "match": "parent",
        }
    ]


@pytest.mark.asyncio
async def test_own_and_parent_matches_use_the_same_selection_rule(
    hass: HomeAssistant,
) -> None:
    """The selected run is the newest across own-context and parent matches."""
    shared = "shared-context"
    own_match = _trace(
        run_id="own-match",
        context_id=shared,
        start=_START,
    )
    parent_match = _trace(
        run_id="parent-match",
        context_id="own-parent",
        parent_id=shared,
        start=_START + timedelta(minutes=1),
        cls=ScriptTrace,
    )
    _store(hass, "automation.uid_a", own_match)
    _store(hass, "script.uid_b", parent_match)

    result = trace_context._trace_context_payload(
        hass, {"id": 1, "context_id": shared}, available=True
    )

    assert result["match"] == "parent"
    assert result["run_id"] == "parent-match"
    assert result["other_matches"] == [
        {
            "domain": "automation",
            "item_id": "uid_a",
            "run_id": "own-match",
            "match": "trace",
        }
    ]


@pytest.mark.asyncio
async def test_no_other_matches_omits_the_list(hass: HomeAssistant) -> None:
    """A single match has no other_matches field."""
    _store(hass, "automation.uid_1", _trace(run_id="r1", context_id="ctx-1"))

    result = trace_context._trace_context_payload(
        hass, {"id": 1, "context_id": "ctx-1"}, available=True
    )

    assert result["run_id"] == "r1"
    assert "other_matches" not in result


@pytest.mark.asyncio
async def test_tie_break_prefers_own_then_automation_then_item_id(
    hass: HomeAssistant,
) -> None:
    """The selection tie-break is own > parent, automation > script, item_id asc."""
    shared = "shared-context"
    parent_auto = _trace(
        run_id="parent-auto",
        context_id="own-parent-auto",
        parent_id=shared,
        start=_START,
    )
    own_script = _trace(
        run_id="own-script",
        context_id=shared,
        start=_START,
        cls=ScriptTrace,
    )
    own_auto_b = _trace(
        run_id="own-auto-b",
        context_id=shared,
        start=_START,
    )
    own_auto_a = _trace(
        run_id="own-auto-a",
        context_id=shared,
        start=_START,
    )
    _store(hass, "automation.uid_b", parent_auto, own_auto_b)
    _store(hass, "script.uid_1", own_script)
    _store(hass, "automation.uid_a", own_auto_a)

    result = trace_context._trace_context_payload(
        hass, {"id": 1, "context_id": shared}, available=True
    )

    assert result["match"] == "trace"
    assert result["domain"] == "automation"
    assert result["item_id"] == "uid_a"
    assert result["run_id"] == "own-auto-a"
    assert result["scanned"] == 4
    assert len(result["other_matches"]) == 3
