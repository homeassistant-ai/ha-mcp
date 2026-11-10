"""Unit tests for the component-side ``ha_mcp_tools/trace_context`` command.

The command reads Core's live trace store in process, matches a run's own
context or a triggering parent context, and returns Core's extended trace dict.
These tests use trace-shaped objects so the selection and preference rules are
pinned without a running Home Assistant.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import voluptuous as vol
from homeassistant.components.trace.const import DATA_TRACE

from custom_components.ha_mcp_tools.websocket_api import trace_context
from custom_components.ha_mcp_tools.websocket_api.schemas import (
    _trace_context_schema,
)


def _trace(
    *,
    run_id: str,
    context_id: str,
    parent_id: str | None = None,
    not_triggered: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        run_id=run_id,
        context=SimpleNamespace(id=context_id, parent_id=parent_id),
        not_triggered=not_triggered,
        as_extended_dict=lambda: {
            "run_id": run_id,
            "context": {"id": context_id, "parent_id": parent_id},
        },
    )


def _bucket(*traces: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(all_traces=lambda: iter(traces))


def _hass(buckets: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(data={DATA_TRACE: buckets})


def test_own_context_match_returns_core_extended_trace() -> None:
    trace = _trace(run_id="r1", context_id="ctx-1")
    hass = _hass({"automation.uid_1": _bucket(trace)})

    result = trace_context._do_trace_context(
        hass, {"context_id": "ctx-1"}, available=True
    )

    assert result["match"] == "trace"
    assert result["run_id"] == "r1"
    assert result["domain"] == "automation"
    assert result["item_id"] == "uid_1"
    assert result["trace"]["run_id"] == "r1"
    assert result["scanned"] == 1


def test_parent_match_prefers_a_real_run_over_not_triggered() -> None:
    diagnostic = _trace(
        run_id="diag",
        context_id="own-diag",
        parent_id="parent-1",
        not_triggered=True,
    )
    run = _trace(run_id="run", context_id="own-run", parent_id="parent-1")
    hass = _hass({"script.uid_1": _bucket(diagnostic, run)})

    result = trace_context._do_trace_context(
        hass, {"context_id": "parent-1"}, available=True
    )

    assert result["match"] == "parent"
    assert result["run_id"] == "run"


def test_domain_and_item_filters_limit_the_scan() -> None:
    automation = _trace(run_id="auto", context_id="own-auto", parent_id="parent-1")
    script = _trace(run_id="script", context_id="own-script", parent_id="parent-1")
    hass = _hass(
        {
            "automation.uid_1": _bucket(automation),
            "script.uid_2": _bucket(script),
        }
    )

    result = trace_context._do_trace_context(
        hass,
        {"context_id": "parent-1", "domain": "script", "item_id": "uid_2"},
        available=True,
    )

    assert result["run_id"] == "script"
    assert result["domain"] == "script"
    assert result["item_id"] == "uid_2"
    assert result["scanned"] == 1


def test_no_match_reports_the_scan_count() -> None:
    tracer = _trace(run_id="r1", context_id="own-1", parent_id="parent-x")
    hass = _hass({"automation.uid_1": _bucket(tracer)})

    result = trace_context._do_trace_context(
        hass, {"context_id": "missing"}, available=True
    )

    assert result["match"] is None
    assert result["trace"] is None
    assert result["scanned"] == 1


def test_unavailable_store_does_not_touch_hass_data() -> None:
    hass = SimpleNamespace(data={})

    result = trace_context._do_trace_context(
        hass, {"context_id": "ctx-1"}, available=False
    )

    assert result["match"] is None
    assert result["available"] is False
    assert result["scanned"] == 0


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
