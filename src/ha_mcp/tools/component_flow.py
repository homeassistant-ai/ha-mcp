"""What Core knows about a flow that its flow endpoints do not report, read
through the ``ha_mcp_tools`` component.

``flow_record`` is Core's record of a flow in progress (what it was started
for). ``flow_outline`` is the steps Core declares for a schema-based flow,
which is how a form after the first is known without answering the ones
before it. Both return ``None`` when no component answers, and the caller
then works from the step Core returns alone.
"""

from __future__ import annotations

from typing import Any

from ..client.rest_client import HomeAssistantCommandNotSent
from .component_api import component_supports, get_component_caps, invalidate_caps

_PREFIX = "ha_mcp_tools/"


async def _ask(client: Any, command: str, **params: Any) -> dict[str, Any] | None:
    """The component's reply to ``command``, whose capability has its name."""
    if not component_supports(await get_component_caps(client), command):
        return None
    try:
        reply = await client.send_websocket_message(
            {"type": _PREFIX + command, **params}
        )
    except HomeAssistantCommandNotSent:
        return None
    if not reply.get("success"):
        if reply.get("error_code") == "unknown_command":
            invalidate_caps(client)
        return None
    result = reply.get("result")
    return result if isinstance(result, dict) else None


async def flow_record(client: Any, manager: str, flow_id: str) -> dict[str, Any] | None:
    """Core's record of ``flow_id`` in its ``config``, ``options`` or
    ``subentries`` flow manager: ``found``, and when found the flow's
    ``handler``, ``source``, ``entry_id`` and ``subentry_id``."""
    return await _ask(client, "flow_record", manager=manager, flow_id=flow_id)


async def flow_outline(
    client: Any, domain: str, flow: str
) -> list[dict[str, Any]] | None:
    """The steps Core declares for ``domain``'s ``config`` or ``options``
    flow, in Core's order; ``None`` for a flow that is not declared as a
    mapping of steps."""
    result = await _ask(client, "flow_outline", domain=domain, flow=flow)
    steps = result.get("steps") if result and result.get("supported") else None
    if not isinstance(steps, list):
        return None
    return [step for step in steps if isinstance(step, dict)]


def steps_after(
    steps: list[dict[str, Any]], step_id: str
) -> list[tuple[dict[str, Any], bool]] | None:
    """The steps Core may show after ``step_id``, each with whether it always
    follows. Where Core picks the next step from the answers, every step not
    yet passed is listed as a possible one. A step Core passes without
    showing a form is followed, not listed. ``None`` when the outline does not
    hold ``step_id`` (a reconfigure step is not part of it).
    """
    by_id = {step.get("step_id"): step for step in steps}
    current = by_id.get(step_id)
    if current is None:
        return None
    later: list[tuple[dict[str, Any], bool]] = []
    seen = {step_id}
    while True:
        if current.get("next_from_answers"):
            later.extend(
                (step, False)
                for name, step in by_id.items()
                if name not in seen and _shown(step)
            )
            break
        current = by_id.get(current.get("next_step"))
        if current is None or current["step_id"] in seen:
            break
        seen.add(current["step_id"])
        if _shown(current):
            later.append((current, True))
    return later


def _shown(step: dict[str, Any]) -> bool:
    """Whether Core shows something at ``step``: a form step declared without
    a schema is passed straight through."""
    return (
        step.get("type") != "form"
        or step.get("data_schema") is not None
        or bool(step.get("schema_from_answers"))
    )
