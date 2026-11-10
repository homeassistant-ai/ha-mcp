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

import pytest
import voluptuous as vol
from homeassistant.components.automation.trace import AutomationTrace
from homeassistant.components.script.trace import ScriptTrace
from homeassistant.components.trace import async_store_trace
from homeassistant.components.trace.const import DATA_TRACE
from homeassistant.components.trace.models import TraceBuckets
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers.trace import TraceElement
from homeassistant.setup import async_setup_component
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
async def test_context_lookup_finds_trace_by_its_own_context(
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
async def test_context_lookup_prefers_real_run_over_newer_diagnostic(
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
    assert result["run_id"] == "newer"
    assert result["scanned"] == 3
    assert result["other_matches"] == [
        {"domain": "script", "item_id": "uid_1", "run_id": "older", "match": "parent"},
        {"domain": "script", "item_id": "uid_1", "run_id": "diag", "match": "parent"},
    ]


@pytest.mark.asyncio
async def test_context_lookup_treats_missing_start_as_oldest(
    hass: HomeAssistant,
) -> None:
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
async def test_context_lookup_honors_domain_and_item_filters(
    hass: HomeAssistant,
) -> None:
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
async def test_context_lookup_reports_no_match_with_scan_count(
    hass: HomeAssistant,
) -> None:
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
async def test_context_lookup_reports_unavailable_when_store_is_not_set_up(
    hass: HomeAssistant,
) -> None:
    result = trace_context._trace_context_payload(
        hass, {"id": 1, "context_id": "ctx-1"}, available=False
    )

    assert result["match"] is None
    assert result["available"] is False
    assert result["scanned"] == 0


@pytest.mark.asyncio
async def test_context_lookup_encodes_trace_with_cores_extended_json_encoder(
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
async def test_context_lookup_reports_unavailable_when_trace_storage_is_off(
    hass: HomeAssistant,
) -> None:
    result = await trace_context._trace_context_prep(hass, {})

    assert result == {"available": False}


@pytest.mark.asyncio
async def test_context_lookup_restores_saved_traces(hass: HomeAssistant) -> None:
    await async_setup_component(hass, "trace", {})
    saved = _trace(run_id="r1", context_id="ctx-1")
    async_store_trace(hass, saved, stored_traces=5)

    result = await trace_context._trace_context_prep(hass, {})

    assert result == {"available": True}
    payload = trace_context._trace_context_payload(
        hass, {"id": 1, "context_id": "ctx-1"}, available=True
    )
    assert payload["match"] == "trace"
    assert payload["run_id"] == "r1"


def test_schema_rejects_an_unknown_trace_domain() -> None:
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
async def test_context_lookup_selects_newest_match_and_lists_others(
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
async def test_context_lookup_prefers_newest_across_own_and_parent_matches(
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
async def test_context_lookup_omits_other_matches_for_a_single_match(
    hass: HomeAssistant,
) -> None:
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
