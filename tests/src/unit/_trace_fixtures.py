"""Real trace runs for the trace unit tests.

``automation_run`` and ``script_run`` build Core's own trace objects, and
``store_runs`` saves them through Core's ``async_store_trace``, so a test's
list rows and details come from Home Assistant itself rather than from a
hand-written reply. A shared helper that builds no stand-in lives in a
``_*.py`` module (tests/AGENTS.md, "Unit Tests Run on Real Home Assistant
Core").
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.automation.trace import AutomationTrace
from homeassistant.components.script.trace import ScriptTrace
from homeassistant.components.trace import async_store_trace
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers.trace import TraceElement
from homeassistant.helpers.trigger.models import NotTriggeredInfo

STORED_TRACES = 10

# The diagnostics Core stores on a trigger that evaluated a change but did not
# fire; see ``AutomationEntity._handle_not_triggered``.
_NOT_TRIGGERED = NotTriggeredInfo(
    reason="threshold_entity_not_found", data={"entity_id": "sensor.missing"}
)


def _finish(
    trace: AutomationTrace | ScriptTrace,
    changed_variables: dict[str, Any] | None = None,
) -> None:
    """Give the run the one step Core records for its kind, then its stopped state."""
    if trace.not_triggered:
        # A trigger that did not fire records only its trigger step, carrying
        # the trigger's diagnostics in the step's result.
        element = TraceElement(changed_variables or {}, "trigger/0")
        element.set_result(**_NOT_TRIGGERED.as_dict())
        trace.set_trace({"trigger/0": [element]})
    else:
        # A script runs its sequence; an automation's steps are actions.
        path = "sequence/0" if isinstance(trace, ScriptTrace) else "action/0"
        trace.set_trace({path: [TraceElement(changed_variables or {}, path)]})
    trace.finished()


def automation_run(
    run_id: str,
    *,
    context_id: str,
    parent_id: str | None = None,
    item_id: str = "uid_1",
    not_triggered: bool = False,
    changed_variables: dict[str, Any] | None = None,
) -> AutomationTrace:
    """One automation run Core stores for ``automation.<item_id>``."""
    trace = AutomationTrace(
        item_id,
        {"alias": "Trace capture"},
        None,
        Context(id=context_id, parent_id=parent_id),
        not_triggered=not_triggered,
    )
    trace.run_id = run_id
    trace.set_trigger_description("state change")
    _finish(trace, changed_variables)
    return trace


def script_run(
    run_id: str,
    *,
    context_id: str,
    parent_id: str | None = None,
    item_id: str = "uid_2",
    changed_variables: dict[str, Any] | None = None,
) -> ScriptTrace:
    """One script run Core stores for ``script.<item_id>``."""
    trace = ScriptTrace(
        item_id,
        {"alias": "Trace capture"},
        None,
        Context(id=context_id, parent_id=parent_id),
    )
    trace.run_id = run_id
    _finish(trace, changed_variables)
    return trace


async def store_runs(
    hass: HomeAssistant, *traces: AutomationTrace | ScriptTrace
) -> None:
    """Save ``traces`` through Core's own trace helper."""
    for trace in traces:
        async_store_trace(hass, trace, stored_traces=STORED_TRACES)
