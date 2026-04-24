"""Reference validator for automation and script configs.

Walks a config dict and extracts every literal service and entity
reference, then cross-checks them against the live service and entity
registries. Produces soft warnings that flow into the existing response
alongside ``best_practice_warnings``.

Intentional limits (documented, not bugs):

- **Templates** (strings containing ``{{``) are counted and skipped.
  Jinja is not rendered here; template-safe validation would need
  ``POST /api/template`` round-trips and is a later follow-up.
- **Blueprint automations** (``use_blueprint`` at the root) are skipped
  wholesale. Post-substitution config is not exposed by any HA API, so
  the effective refs cannot be ground-truthed.
- **``device_id`` / ``area_id`` / ``label_id``** are NOT checked yet.
  They require separate registry fetches and are planned for a later
  pass; see #940.

Background: #940 (hallucinated ``notify.mobile_app_andrew_phone`` that
``ha_config_set_automation`` accepted silently).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, TypedDict

logger = logging.getLogger(__name__)

# Keys whose value (literal string) names a Home Assistant service in
# the form ``<domain>.<service_name>``. Both legacy (``service:``) and
# the new-style (``action:``) keys are recognized — HA accepts either
# inside automation/script action blocks.
_SERVICE_KEYS: frozenset[str] = frozenset({"service", "action"})

# Keys whose value (string or list of strings) names an entity. These
# appear in triggers, conditions, ``target:`` blocks, and service
# ``data:`` blocks — the walker is depth-agnostic so the location
# doesn't matter.
_ENTITY_KEYS: frozenset[str] = frozenset({"entity_id"})


class ExtractedRef(TypedDict):
    """One reference pulled out of the config tree."""

    path: str
    value: str
    kind: str  # "service" | "entity"


class WalkerResult(TypedDict):
    """Return value of :func:`extract_refs`."""

    refs: list[ExtractedRef]
    unvalidated_templates: int
    blueprint_skipped: bool


class ValidationWarning(TypedDict):
    """One warning in the tool response."""

    path: str
    value: str
    kind: str
    reason: str


def extract_refs(config: Any) -> WalkerResult:
    """Pull every literal service/entity reference out of *config*.

    Pure function: no network, no mutation of the input. The caller
    decides what to do with the extracted refs.

    Blueprint configs short-circuit with an empty ref list and
    ``blueprint_skipped=True`` — the effective post-substitution config
    is not reachable from ha-mcp, so it cannot be validated.
    """
    if isinstance(config, dict) and "use_blueprint" in config:
        return {
            "refs": [],
            "unvalidated_templates": 0,
            "blueprint_skipped": True,
        }

    refs: list[ExtractedRef] = []
    # Wrapped in a single-element list so the inner closure can mutate
    # it without a ``nonlocal`` dance.
    unvalidated_templates = [0]

    def _walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                sub_path = f"{path}.{key}" if path else key

                if key in _SERVICE_KEYS and isinstance(value, str):
                    if _is_template(value):
                        unvalidated_templates[0] += 1
                    else:
                        refs.append(
                            {"path": sub_path, "value": value, "kind": "service"}
                        )
                    continue

                if key in _ENTITY_KEYS:
                    if isinstance(value, str):
                        if _is_template(value):
                            unvalidated_templates[0] += 1
                        else:
                            refs.append(
                                {"path": sub_path, "value": value, "kind": "entity"}
                            )
                        continue
                    if isinstance(value, list):
                        for i, item in enumerate(value):
                            if not isinstance(item, str):
                                continue
                            item_path = f"{sub_path}[{i}]"
                            if _is_template(item):
                                unvalidated_templates[0] += 1
                            else:
                                refs.append(
                                    {
                                        "path": item_path,
                                        "value": item,
                                        "kind": "entity",
                                    }
                                )
                        continue

                # Neither a service nor an entity key: recurse so deeply
                # nested action blocks (choose/if/parallel/repeat) still
                # get walked.
                _walk(value, sub_path)

        elif isinstance(node, list):
            for i, item in enumerate(node):
                _walk(item, f"{path}[{i}]")
        # Primitives: nothing to extract.

    _walk(config, "")
    return {
        "refs": refs,
        "unvalidated_templates": unvalidated_templates[0],
        "blueprint_skipped": False,
    }


def _is_template(value: str) -> bool:
    """Return True if *value* looks like a Jinja template."""
    return "{{" in value


def build_service_index(services_payload: Any) -> dict[str, set[str]]:
    """Turn ``/api/services`` output into a ``{domain: {services}}`` map.

    HA returns a list of ``{"domain": str, "services": {name: {...}}}``
    objects — one per domain. Any malformed entry is skipped silently.
    """
    index: dict[str, set[str]] = {}
    if not isinstance(services_payload, list):
        return index
    for entry in services_payload:
        if not isinstance(entry, dict):
            continue
        domain = entry.get("domain")
        services = entry.get("services")
        if not isinstance(domain, str) or not isinstance(services, dict):
            continue
        index[domain] = set(services.keys())
    return index


def build_entity_set(states_payload: Any) -> set[str]:
    """Turn ``/api/states`` output into a set of entity_ids."""
    entities: set[str] = set()
    if not isinstance(states_payload, list):
        return entities
    for entry in states_payload:
        if isinstance(entry, dict):
            entity_id = entry.get("entity_id")
            if isinstance(entity_id, str):
                entities.add(entity_id)
    return entities


def check_refs(
    refs: list[ExtractedRef],
    service_index: dict[str, set[str]],
    entity_set: set[str],
) -> list[ValidationWarning]:
    """Return one warning per ref that isn't in the registry."""
    warnings: list[ValidationWarning] = []
    for ref in refs:
        value = ref["value"]
        if ref["kind"] == "service":
            domain, _, service_name = value.partition(".")
            if (
                not service_name
                or domain not in service_index
                or service_name not in service_index[domain]
            ):
                warnings.append(
                    {
                        "path": ref["path"],
                        "value": value,
                        "kind": "service",
                        "reason": "not found in service registry",
                    }
                )
        elif ref["kind"] == "entity":
            if value not in entity_set:
                warnings.append(
                    {
                        "path": ref["path"],
                        "value": value,
                        "kind": "entity",
                        "reason": "not found in entity registry",
                    }
                )
    return warnings


async def validate_config_references(
    client: Any, config: dict[str, Any]
) -> dict[str, Any]:
    """Walk *config*, fetch registries, return validation metadata.

    Errors from the two registry fetches are logged and swallowed so
    validation can never break the happy path of
    ``ha_config_set_automation`` / ``ha_config_set_script``.

    Returns a dict with three keys:

    - ``warnings`` - list of :class:`ValidationWarning`, empty on success
    - ``unvalidated_templates`` - int, templated strings skipped by the
      walker
    - ``blueprint_skipped`` - bool, True iff the root config uses
      ``use_blueprint``
    """
    walker_result = extract_refs(config)

    if walker_result["blueprint_skipped"] or not walker_result["refs"]:
        return {
            "warnings": [],
            "unvalidated_templates": walker_result["unvalidated_templates"],
            "blueprint_skipped": walker_result["blueprint_skipped"],
        }

    try:
        services_payload, states_payload = await asyncio.gather(
            client.get_services(),
            client.get_states(),
        )
    except Exception:
        logger.exception(
            "Reference validator: failed to fetch service/entity registries; "
            "skipping validation for this call"
        )
        return {
            "warnings": [],
            "unvalidated_templates": walker_result["unvalidated_templates"],
            "blueprint_skipped": False,
        }

    service_index = build_service_index(services_payload)
    entity_set = build_entity_set(states_payload)
    warnings = check_refs(walker_result["refs"], service_index, entity_set)

    return {
        "warnings": warnings,
        "unvalidated_templates": walker_result["unvalidated_templates"],
        "blueprint_skipped": False,
    }
