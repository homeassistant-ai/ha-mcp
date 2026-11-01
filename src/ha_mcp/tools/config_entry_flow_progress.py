"""Waiting out a config-entry flow's ``progress`` step.

A progress step waits on work Home Assistant runs itself (a scan, a download).
When that work finishes, Core moves the flow to ``progress_done``; a GET on the
flow runs ``async_configure`` again, which steps past ``progress_done`` and
answers the step the flow is at now. That is the call the frontend makes when
Core reports the flow progressed, so polling it drives the flow the same way.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from ..errors import ErrorCode, create_error_response
from .config_entry_flow_errors import (
    FlowRef,
    _flow_failure_context,
    progress_action_text,
)
from .helpers import raise_tool_error

PROGRESS_STEP_TYPES = frozenset({"progress", "progress_done"})
PROGRESS_BUDGET_S = 120.0
POLL_INTERVAL_S = 1.0
_POLL_INTERVAL_MAX_S = 5.0

# The REST flow resource of each flow manager, keyed by the strings category
# FlowRef.path starts with.
_FLOW_RESOURCES = {
    "config": "flow",
    "options": "options/flow",
    "config_subentries": "subentries/flow",
}


async def wait_for_progress(
    client: Any,
    flow: FlowRef,
    flow_id: str,
    step: dict[str, Any],
    *,
    is_reconfigure: bool = False,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> dict[str, Any]:
    """Poll the flow until it leaves its progress step; return the step it reaches.

    Raises ``TIMEOUT_OPERATION`` with the step's ``progress_action`` and Core's
    text for it when the step is still in progress after
    ``PROGRESS_BUDGET_S``. The flow is left open; the walkers' callers abort it.
    """
    resource = _FLOW_RESOURCES[flow.path.split(".")[0]]
    url = f"/config/config_entries/{resource}/{flow_id}"
    deadline = clock() + PROGRESS_BUDGET_S
    interval = POLL_INTERVAL_S
    while step.get("type") in PROGRESS_STEP_TYPES:
        remaining = deadline - clock()
        if remaining <= 0:
            raise_tool_error(
                create_error_response(
                    ErrorCode.TIMEOUT_OPERATION,
                    f"Flow step {step.get('step_id')!r} was still in progress "
                    f"after {PROGRESS_BUDGET_S:.0f}s",
                    suggestions=[
                        "Retry once the task Home Assistant reports has "
                        "finished, or complete the flow in the Home Assistant UI."
                    ],
                    context={
                        **_flow_failure_context(
                            flow_id, step, is_reconfigure=is_reconfigure
                        ),
                        "progress_action": step.get("progress_action"),
                        **await progress_action_text(client, flow, step),
                    },
                )
            )
        await sleep(min(interval, remaining))
        interval = min(interval * 1.5, _POLL_INTERVAL_MAX_S)
        step = await client._request("GET", url)
    return step
