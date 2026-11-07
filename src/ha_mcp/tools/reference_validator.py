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
RefKind = Literal["service", "entity", "device", "area", "floor", "label"]


class ValidationWarning(TypedDict):
    """One warning in the tool response.

    A missing reference has no ``path``: Core's helpers collect references as
    sets. A target Core does not offer carries its ``path``, the item it is a
    target of (``target_of``) and what Core does offer for it (``offered``).
    """

    kind: RefKind
    value: str
    reason: str
    path: NotRequired[str]
    target_of: NotRequired[str]
    offered: NotRequired[list[str]]


class ValidationMeta(TypedDict):
    """Core's warnings, or why the references went unchecked."""

    warnings: list[ValidationWarning]
    references_unchecked: NotRequired[str]


def _warnings(answer: dict[str, Any]) -> list[ValidationWarning]:
    warnings: list[ValidationWarning] = [
        {
            "kind": missing["kind"],
            "value": missing["value"],
            "reason": f"Home Assistant has no {missing['kind']} {missing['value']}",
        }
        for missing in answer["missing"]
    ]
    warnings.extend(
        {
            "kind": "entity",
            "value": mismatch["entity_id"],
            "path": mismatch["path"],
            "target_of": mismatch["name"],
            "offered": mismatch["offered"],
            "reason": (
                f"Home Assistant does not offer {mismatch['entity_id']} as a "
                f"target of {mismatch['name']}; see offered for what it does offer"
            ),
        }
        for mismatch in answer["mismatches"]
    )
    return warnings


async def validate_config_references(
    client: Any, config: dict[str, Any], kind: ConfigKind
) -> ValidationMeta:
    """Core's verdict on *config*'s references, as validation metadata.

    ``references_unchecked`` names why nothing was checked: the component is
    absent or outdated, the command or transport failed, Core's validator
    refused the config (Core then refuses the write itself), or the component's
    check failed.
    """
    answer = await fetch_config_references_via_component(client, kind, config)
    if answer["status"] != "ok":
        return {
            "warnings": [],
            "references_unchecked": answer.get("error")
            or f"references were not checked: the check answered {answer['status']}",
        }
    return {"warnings": _warnings(answer)}
