"""References in an automation, script or scene config, checked by Core.

The component's ``ha_mcp_tools/config_references`` bridge runs Core's own
validator on the proposed config, collects its references with Core's helpers
and checks them against Core's registries, and puts each entity target to
Core's target lookups (the editor's target pickers). This module only turns
that answer into soft warnings beside ``best_practice_warnings``. There is no
server-side copy of Core's rules to fall back on: without the component the
response says the references went unchecked.

Background: #940 (a hallucinated ``notify.mobile_app_andrew_phone`` that
``ha_config_set_automation`` accepted silently) and #2989 (``select.select_option``
aimed at an ``input_select``, which Core saves and the action then skips).
"""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

from .component_config_reads import fetch_config_references_via_component

ConfigKind = Literal["automation", "script", "scene"]

_UNCHECKED_WITHOUT_COMPONENT = (
    "references were not checked: Home Assistant checks them through the "
    "ha_mcp_tools component, which is not available"
)


class ValidationWarning(TypedDict):
    """One warning in the tool response."""

    kind: str
    value: str
    reason: str
    path: NotRequired[str]


def _warnings(answer: dict[str, Any]) -> list[ValidationWarning]:
    warnings: list[ValidationWarning] = [
        {
            "kind": missing["kind"],
            "value": missing["value"],
            "reason": f"Home Assistant has no {missing['kind']} {missing['value']}",
        }
        for missing in answer.get("missing", [])
    ]
    warnings.extend(
        {
            "path": mismatch["path"],
            "kind": "entity",
            "value": mismatch["entity_id"],
            "reason": (
                f"Home Assistant does not offer {mismatch['entity_id']} as a "
                f"target of {mismatch['name']}; for that entity it offers: "
                + (", ".join(mismatch["offered"]) or "nothing")
            ),
        }
        for mismatch in answer.get("mismatches", [])
    )
    return warnings


async def validate_config_references(
    client: Any, config: dict[str, Any], kind: ConfigKind
) -> dict[str, Any]:
    """Core's verdict on *config*'s references, as validation metadata.

    Returns ``{"warnings": [...]}``, plus ``references_unchecked`` naming why
    when the component could not answer: it is not installed, Core's
    validator refused the config (Core refuses the write itself), or a part of
    Core it asks is missing.
    """
    answer = await fetch_config_references_via_component(client, kind, config)
    if answer is None:
        return {"warnings": [], "references_unchecked": _UNCHECKED_WITHOUT_COMPONENT}
    if answer.get("status") != "ok":
        return {"warnings": [], "references_unchecked": answer.get("error")}
    return {"warnings": _warnings(answer)}
