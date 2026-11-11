"""Real trace runs for the trace unit tests.

``automation_run`` and ``script_run`` build Core's own trace objects, and
``store_runs`` saves them through Core's ``async_store_trace``, so a test's
list rows and details come from Home Assistant itself rather than from a
hand-written reply. A shared helper that builds no stand-in lives in a
``_*.py`` module (tests/AGENTS.md, "Unit Tests Run on Real Home Assistant
Core").
"""

from __future__ import annotations

from homeassistant.components.automation.trace import AutomationTrace
from homeassistant.components.script.trace import ScriptTrace
from homeassistant.components.trace import async_store_trace
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers.trace import TraceElement

STORED_TRACES = 10


def _finish(trace: AutomationTrace | ScriptTrace) -> None:
    """Give the run one action step, then Core's own stopped state."""
    trace.set_trace({"action/0": [TraceElement({}, "action/0")]})
    trace.finished()


def automation_run(
    run_id: str, *, context_id: str, parent_id: str | None = None
) -> AutomationTrace:
    """One automation run Core stores for ``automation.uid_1``."""
    trace = AutomationTrace(
        "uid_1",
        {"alias": "Trace capture"},
        None,
        Context(id=context_id, parent_id=parent_id),
    )
    trace.run_id = run_id
    trace.set_trigger_description("state change")
    _finish(trace)
    return trace


def script_run(
    run_id: str, *, context_id: str, parent_id: str | None = None
) -> ScriptTrace:
    """One script run Core stores for ``script.uid_2``."""
    trace = ScriptTrace(
        "uid_2",
        {"alias": "Trace capture"},
        None,
        Context(id=context_id, parent_id=parent_id),
    )
    trace.run_id = run_id
    _finish(trace)
    return trace


async def store_runs(
    hass: HomeAssistant, *traces: AutomationTrace | ScriptTrace
) -> None:
    """Save ``traces`` through Core's own trace helper."""
    for trace in traces:
        async_store_trace(hass, trace, stored_traces=STORED_TRACES)
