"""What Core knows about a flow that its flow endpoints do not report, read
through the ``ha_mcp_tools`` component.

``flow_record`` is Core's record of a flow in progress (what it was started
for). ``flow_outline`` is the steps Core declares for a schema-based flow,
which is how a form after the first is known without answering the ones
before it. ``core_flow_step`` is a step as Home Assistant 2026.12's own flow
formatter presents it, texts included. Each returns ``None`` when no component
answers, and the caller then works from the step Core returns alone.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .component_api import ask_component, component_serves

# A flow's place in strings.json -> the flow manager holding it.
_MANAGER_OF_PATH = {
    "config": "config",
    "options": "options",
    "config_subentries": "subentries",
    "issues": "repairs",
}
# What the formatter reads of a step that has ended or is waiting; a form or
# menu it reads from the flow itself.
_STEP_KEYS = (
    "flow_id",
    "handler",
    "type",
    "step_id",
    "reason",
    "title",
    "description",
    "description_placeholders",
    "progress_action",
    "translation_domain",
)

# A component that reads Repairs fix flows advertises this beside flow_record.
_REPAIRS_FLOW_RECORD = "flow_record_repairs"


async def flow_record(client: Any, manager: str, flow_id: str) -> dict[str, Any] | None:
    """Core's record of ``flow_id`` in its ``config``, ``options``,
    ``subentries`` or ``repairs`` flow manager: ``found``, and when found the
    flow's ``handler``, ``source``, ``entry_id``, ``subentry_id`` and
    ``issue_id``."""
    if manager == "repairs" and not await component_serves(
        client, _REPAIRS_FLOW_RECORD
    ):
        return None
    return await ask_component(client, "flow_record", manager=manager, flow_id=flow_id)


async def flow_outline(
    client: Any, domain: str, flow: str
) -> list[dict[str, Any]] | None:
    """The steps Core declares for ``domain``'s ``config`` or ``options``
    flow, in Core's order; ``None`` for a flow that is not declared as a
    mapping of steps."""
    result = await ask_component(client, "flow_outline", domain=domain, flow=flow)
    steps = result.get("steps") if result and result.get("supported") else None
    if not isinstance(steps, list):
        return None
    return [step for step in steps if isinstance(step, dict)]


def steps_after(
    steps: list[dict[str, Any]], step_id: str
) -> list[tuple[dict[str, Any], bool]] | None:
    """The steps Core may show after ``step_id``, each with whether it always
    follows. Where Core picks the next step from the answers, every step not
    yet passed is listed as a candidate: the declaration does not say which
    of them the callback can return. A step Core passes without
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


async def core_flow_step(
    client: Any,
    path: str,
    step: Mapping[str, Any],
    placeholders: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """``step`` as Home Assistant's flow formatter presents it: ``title``,
    ``description``, ``fields`` with each one's ``label`` and help text,
    ``errors``, ``menu_options`` labels, and the progress, create-entry and
    abort text as ``description``. ``path`` is the flow's place in
    ``strings.json`` (``config``, ``options``, ``config_subentries.<type>`` or
    ``issues.<key>.fix_flow``) and ``placeholders`` a fix flow's issue
    placeholders. ``None`` before Home Assistant 2026.12, without the
    component, or once the flow has moved past a form or menu."""
    head, _, rest = path.partition(".")
    manager = _MANAGER_OF_PATH.get(head)
    if manager is None or not isinstance(step.get("flow_id"), str):
        return None
    extra: dict[str, Any] = {}
    if manager == "repairs":
        extra = {
            "translation_key": rest.removesuffix(".fix_flow"),
            "placeholders": dict(placeholders) if placeholders else None,
        }
    result = await ask_component(
        client,
        "flow_format",
        manager=manager,
        step={key: step[key] for key in _STEP_KEYS if key in step},
        **extra,
    )
    formatted = result.get("step") if result and result.get("found") else None
    return formatted if isinstance(formatted, dict) else None


def core_field_text(fields: list[Any], prefix: str = "") -> dict[str, dict[str, str]]:
    """Core's ``label`` and help ``description`` of each formatted field, by
    name (``<section>.<field>`` inside a section)."""
    texts: dict[str, dict[str, str]] = {}
    for field in fields:
        if not isinstance(field, dict) or not isinstance(field.get("name"), str):
            continue
        name = f"{prefix}{field['name']}"
        if isinstance(field.get("schema"), list):
            texts.update(core_field_text(field["schema"], f"{name}."))
        found = {
            key: field[key]
            for key in ("label", "description")
            if isinstance(field.get(key), str)
        }
        if found:
            texts[name] = found
    return texts
