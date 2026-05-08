"""Reactive best-practice checker for HA automation/script configs.

Stateless payload inspection — returns warnings pointing to skill reference
files. Zero overhead on clean calls (returns empty list).

Warnings include skill:// URIs so the LLM can read the relevant reference
file via the bundled SkillsDirectoryProvider. The ``skill_prefix`` kwarg
lets callers pass any URL prefix (e.g., a GitHub mirror) when skill://
isn't reachable, or ``None`` to omit references entirely.

Each warning carries the native alternative inline (a concrete example or
short explanation) before the URI suffix, so clients that don't auto-fetch
resource URIs still receive actionable guidance.

The checker covers two layers:

1. *Specific* detectors for known anti-pattern shapes — each emits a tailored
   message that names the native alternative concretely.
2. A *generic* fallback that fires when ``{{ ... }}`` or ``{% ... %}`` shows
   up in a logic position (condition / trigger / wait_template / target field)
   without matching a specific pattern. This catches new template misuse
   without waiting for a regex to be added — see issue #1011.

Templates in legitimate positions (action ``data`` fields, notification
``message``/``title``, ``event_data``, ``variables``) are *not* inspected and
therefore never flagged — those are the documented dynamic-data cases.

Anti-patterns sourced from:
  https://github.com/homeassistant-ai/skills
  skill://home-assistant-best-practices
"""

from __future__ import annotations

import re
from typing import Any

_SKILL_URI_PREFIX = "skill://home-assistant-best-practices/references"
_DEFAULT_SKILL_PREFIX = _SKILL_URI_PREFIX

# ---------------------------------------------------------------------------
# Regex patterns for template anti-patterns
# ---------------------------------------------------------------------------

# float/int comparison: | float > 25, | int(0) >= 10, float(x) < 5
_RE_NUMERIC_CMP = re.compile(
    r"\|\s*(?:float|int)\s*(?:\([^)]*\)\s*)?[><]=?"
    r"|(?:float|int)\s*\([^)]*\)\s*[><]=?"
)
# is_state() call (not is_state_attr)
_RE_IS_STATE = re.compile(r"\bis_state\s*\(")
# now().hour or now().minute
_RE_NOW_TIME = re.compile(r"\bnow\(\)\s*\.\s*(?:hour|minute)\b")
# now().weekday() / now().isoweekday() / now().strftime('%A'|'%w')
_RE_WEEKDAY = re.compile(
    r"\bnow\(\)\s*\.\s*(?:weekday|isoweekday)\s*\("
    r"|\bnow\(\)\s*\.\s*strftime\s*\(\s*['\"]%[Aaw]['\"]"
)
# Date-component checks: now().date(), now().year/month/day
# (issue #1011 — agent first reached for `now().date().isoformat() == '...'`)
_RE_NOW_DATE = re.compile(
    r"\bnow\(\)\s*\.\s*date\s*\("
    r"|\bnow\(\)\s*\.\s*(?:year|month|day)\b(?!\s*\()"
)
# sun.sun entity references
_RE_SUN = re.compile(r"(?:is_state|state_attr|states)\s*\(\s*['\"]sun\.sun['\"]")
# states('x') in [...] or states('x') in (...)
_RE_STATE_IN = re.compile(r"states\s*\([^)]+\)\s+in\s+[\[(]")
# Unsafe direct state access: states.sensor.x.state
_RE_DIRECT_STATE = re.compile(r"\bstates\.\w+\.\w+\.state\b")
# Motion entity pattern
_RE_MOTION = re.compile(r"binary_sensor\.\w*motion", re.IGNORECASE)
# Any Jinja template marker — catch-all and target-field scan.
_RE_ANY_TEMPLATE = re.compile(r"\{\{|\{%")
# `this.X` self-reference (e.g. `{{ this.entity_id }}`)
_RE_THIS_REFERENCE = re.compile(r"\bthis\s*\.\s*\w+")

# Target sub-fields scanned for templates (issue #1011).
_TARGET_FIELDS = ("entity_id", "device_id", "area_id", "floor_id", "label_id")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_automation_config(
    config: dict[str, Any],
    *,
    skill_prefix: str | None = _DEFAULT_SKILL_PREFIX,
) -> list[str]:
    """Return best-practice warnings for an automation config.

    Args:
        config: The automation configuration dict.
        skill_prefix: Base URI for skill references (e.g.
            "skill://home-assistant-best-practices/references").
            Pass None when skills are disabled — warnings still fire
            but without the "See skill://..." suffix.
    """
    if "use_blueprint" in config:
        return []

    warnings: list[str] = []

    # Condition templates
    _check_condition_templates(config.get("condition", []), warnings, skill_prefix)

    # Action tree (wait_template + nested conditions + target templates)
    _check_action_tree(config.get("action", []), warnings, skill_prefix)

    # Trigger templates + device_id
    _check_triggers(config.get("trigger", []), warnings, skill_prefix)

    # Mode vs motion pattern
    _check_mode_motion(config, warnings, skill_prefix)

    return _dedupe(warnings)


def check_script_config(
    config: dict[str, Any],
    *,
    skill_prefix: str | None = _DEFAULT_SKILL_PREFIX,
) -> list[str]:
    """Return best-practice warnings for a script config.

    Args:
        config: The script configuration dict.
        skill_prefix: Base URI for skill references.
            Pass None when skills are disabled.
    """
    if "use_blueprint" in config:
        return []

    warnings: list[str] = []
    _check_action_tree(config.get("sequence", []), warnings, skill_prefix)
    return _dedupe(warnings)


# ---------------------------------------------------------------------------
# Skill reference helper
# ---------------------------------------------------------------------------


def _ref(skill_prefix: str | None, path: str) -> str:
    """Return a ' See <URI>' suffix when skills are enabled, empty otherwise."""
    if skill_prefix:
        return f" See {skill_prefix}/{path}"
    return ""


# ---------------------------------------------------------------------------
# Condition template checks
# ---------------------------------------------------------------------------


def _check_condition_templates(
    conditions: Any, warnings: list[str], skill_prefix: str | None
) -> None:
    """Check condition tree for template anti-patterns."""
    for cond in _as_list(conditions):
        if isinstance(cond, str) and "{{" in cond:
            # Shorthand template condition
            _check_template_string(cond, warnings, skill_prefix, "condition")
        elif isinstance(cond, dict):
            if cond.get("condition") == "template":
                vt = cond.get("value_template", "")
                if isinstance(vt, str):
                    _check_template_string(vt, warnings, skill_prefix, "condition")
            # Recurse into compound conditions (and/or/not)
            nested = cond.get("conditions")
            if nested:
                _check_condition_templates(nested, warnings, skill_prefix)


def _check_template_string(
    template: str,
    warnings: list[str],
    skill_prefix: str | None,
    position: str,
) -> None:
    """Check a single template string for known anti-patterns.

    ``position`` is "condition" or "trigger" — used by the generic fallback
    when none of the specific detectors match, so the message can name the
    position concretely.
    """
    initial_count = len(warnings)

    if _RE_NUMERIC_CMP.search(template):
        warnings.append(
            "Condition uses template with float/int comparison — use native "
            "`numeric_state` condition instead "
            "(e.g., `condition: numeric_state, entity_id: sensor.temp, above: 25`). "
            "Native conditions are validated at config load and don't bypass HA's schema."
            + _ref(skill_prefix, "automation-patterns.md#native-conditions")
        )
    if _RE_SUN.search(template):
        warnings.append(
            "Condition uses template referencing `sun.sun` — use native "
            "`sun` condition instead "
            "(e.g., `condition: sun, after: sunset` or `before: sunrise`)."
            + _ref(skill_prefix, "automation-patterns.md#native-conditions")
        )
    elif _RE_IS_STATE.search(template):
        # Only flag if not already flagged as sun pattern
        warnings.append(
            "Condition uses template with `is_state()` — use native "
            "`state` condition instead "
            "(e.g., `condition: state, entity_id: light.bedroom, state: 'on'`)."
            + _ref(skill_prefix, "automation-patterns.md#native-conditions")
        )
    if _RE_NOW_TIME.search(template):
        warnings.append(
            "Condition uses template with `now().hour/minute` — use native "
            "`time` condition instead "
            "(e.g., `condition: time, after: '09:00:00', before: '17:00:00'`)."
            + _ref(skill_prefix, "automation-patterns.md#native-conditions")
        )
    if _RE_WEEKDAY.search(template):
        warnings.append(
            "Condition uses template for day-of-week check — use native "
            "`time` condition with `weekday:` list instead "
            "(e.g., `condition: time, weekday: ['mon', 'tue', 'wed']`)."
            + _ref(skill_prefix, "automation-patterns.md#native-conditions")
        )
    if _RE_NOW_DATE.search(template):
        warnings.append(
            "Condition uses date-based check (`now().date()` / `now().year/month/day`) — "
            "for one-shot date-specific firing, use a `time` trigger and self-disable via "
            "`automation.turn_off` with a hardcoded `entity_id` (the next `00:01` fire IS the "
            "target date on creation day). For recurring date logic, expose a `sensor.date` via "
            "the `time_date` integration and use a `state` trigger/condition."
            + _ref(skill_prefix, "automation-patterns.md#native-conditions")
        )
    if _RE_STATE_IN.search(template):
        warnings.append(
            "Condition uses template with `states(...) in [...]` — use native "
            "`state` condition with `state:` list instead "
            "(e.g., `condition: state, entity_id: climate.living_room, state: ['heat', 'cool']`)."
            + _ref(skill_prefix, "automation-patterns.md#native-conditions")
        )
    if _RE_DIRECT_STATE.search(template):
        warnings.append(
            "Template uses `states.domain.entity.state` direct access which "
            "errors if entity doesn't exist — use the `states('entity_id')` "
            "function instead (returns 'unknown' if missing rather than raising)."
            + _ref(skill_prefix, "template-guidelines.md#common-patterns")
        )

    # Generic fallback: any Jinja in this logic position that didn't match
    # a specific detector. Catches new anti-patterns (issue #1011) and
    # reframes #695 from "enumerate bad shapes" to "surface every template
    # in a logic position". Specific detectors above keep their tailored
    # messages.
    if (
        len(warnings) == initial_count
        and _RE_ANY_TEMPLATE.search(template)
    ):
        warnings.append(
            f"Template detected in {position} — if this maps to a native option "
            "(`numeric_state`, `state`, `time`, `sun`, `zone`, `device`), use that "
            "instead. Templates fail silently at runtime and bypass schema validation."
            + _ref(skill_prefix, "template-guidelines.md#when-to-avoid-templates")
        )


# ---------------------------------------------------------------------------
# Action tree checks
# ---------------------------------------------------------------------------


def _check_choose_actions(
    choose: Any, warnings: list[str], skill_prefix: str | None
) -> None:
    for option in _as_list(choose):
        if isinstance(option, dict):
            _check_condition_templates(
                option.get("conditions", []), warnings, skill_prefix
            )
            _check_action_tree(
                option.get("sequence", []), warnings, skill_prefix
            )


def _check_repeat_actions(
    repeat: dict, warnings: list[str], skill_prefix: str | None
) -> None:
    _check_condition_templates(repeat.get("while", []), warnings, skill_prefix)
    _check_condition_templates(repeat.get("until", []), warnings, skill_prefix)
    _check_action_tree(repeat.get("sequence", []), warnings, skill_prefix)


def _check_action_tree(
    actions: Any, warnings: list[str], skill_prefix: str | None
) -> None:
    """Walk action tree checking for wait_template, nested conditions, and target templates."""
    for action in _as_list(actions):
        if not isinstance(action, dict):
            continue

        # Inline condition steps (e.g. `- condition: template, value_template: ...`
        # in a sequence). Without this, templates in condition shorthand inside
        # scripts and automation actions slipped past the checker — only
        # conditions in `if:`, `choose.conditions`, and `repeat.while/until`
        # were inspected.
        cond_kind = action.get("condition")
        if isinstance(cond_kind, str) and "service" not in action:
            _check_condition_templates([action], warnings, skill_prefix)

        if "wait_template" in action:
            warnings.append(
                "Action uses `wait_template` — consider `wait_for_trigger` "
                "with a state trigger (note: different semantics — "
                "`wait_for_trigger` waits for a *change*, `wait_template` "
                "passes immediately if already true)."
                + _ref(skill_prefix, "automation-patterns.md#wait-actions")
            )

        # Templated service dispatch: `service:` containing `{{ }}` or any
        # `service_template:` field. The native alternative is a `choose`
        # (or `if/then/else`) action that picks between hardcoded service
        # names based on state.
        _check_service_template(action, warnings, skill_prefix)

        # Templates in target sub-fields (issue #1011 — `{{ this.entity_id }}`
        # in target.entity_id was missed by #695). Service `data`, `event_data`,
        # and notification message/title are intentionally NOT scanned — those
        # are the documented legitimate dynamic-data positions.
        target = action.get("target")
        if isinstance(target, dict):
            _check_target_dict(target, warnings, skill_prefix)

        # Nested conditions in choose/if/repeat
        if "choose" in action:
            _check_choose_actions(action["choose"], warnings, skill_prefix)

        if "if" in action:
            _check_condition_templates(action["if"], warnings, skill_prefix)

        for key in ("then", "else", "default"):
            nested = action.get(key)
            if isinstance(nested, list):
                _check_action_tree(nested, warnings, skill_prefix)

        if "repeat" in action and isinstance(action["repeat"], dict):
            _check_repeat_actions(action["repeat"], warnings, skill_prefix)


def _check_service_template(
    action: dict[str, Any], warnings: list[str], skill_prefix: str | None
) -> None:
    """Flag template-based service dispatch in an action.

    Two shapes:
    - ``service_template:`` — the explicit (deprecated) way to template a
      service name. Flag any value.
    - ``service:`` containing ``{{`` — modern syntax with a template.

    The native alternative is a ``choose`` (or ``if/then/else``) action that
    dispatches to different hardcoded service names based on state.
    """
    if "service_template" in action:
        warnings.append(
            "Action uses `service_template` — use a `choose` (or `if/then/else`) "
            "action that dispatches to different hardcoded `service:` names based "
            "on state. Native dispatch validates each service name at config load."
            + _ref(skill_prefix, "automation-patterns.md#ifthen-vs-choose")
        )
        return
    service = action.get("service")
    if isinstance(service, str) and _RE_ANY_TEMPLATE.search(service):
        warnings.append(
            "Action `service:` field contains a template — use a `choose` "
            "(or `if/then/else`) action with hardcoded service names instead. "
            "Templates here bypass HA's service-name validation and fail "
            "silently if the resolved string is invalid."
            + _ref(skill_prefix, "automation-patterns.md#ifthen-vs-choose")
        )


def _check_target_dict(
    target: dict[str, Any], warnings: list[str], skill_prefix: str | None
) -> None:
    """Flag any Jinja in target.entity_id/device_id/area_id/floor_id/label_id.

    Targets are resolved at write time — every legitimate use case can be
    written as a literal. `{{ this.entity_id }}` self-references in particular
    are pure noise (the agent already knows the entity_id).
    """
    for field in _TARGET_FIELDS:
        value = target.get(field)
        for item in _as_list(value):
            if not isinstance(item, str) or not _RE_ANY_TEMPLATE.search(item):
                continue
            if _RE_THIS_REFERENCE.search(item):
                warnings.append(
                    f"Action `target.{field}` uses a `this.*` self-reference template — "
                    f"hardcode the literal value instead. The self-reference is always "
                    f"resolvable at write time, so the template adds runtime cost without "
                    f"any flexibility."
                    + _ref(skill_prefix, "template-guidelines.md#when-to-avoid-templates")
                )
            else:
                warnings.append(
                    f"Action `target.{field}` uses a template — prefer a hardcoded literal, "
                    f"or use a `choose` action with native conditions to dispatch to different "
                    f"hardcoded targets. Templates in target fields fail silently if they "
                    f"resolve to a non-existent entity."
                    + _ref(skill_prefix, "template-guidelines.md#when-to-avoid-templates")
                )


# ---------------------------------------------------------------------------
# Trigger checks
# ---------------------------------------------------------------------------


def _check_triggers(
    triggers: Any, warnings: list[str], skill_prefix: str | None
) -> None:
    """Check triggers for device_id and template anti-patterns."""
    for trigger in _as_list(triggers):
        if not isinstance(trigger, dict):
            continue

        platform = trigger.get("platform", trigger.get("trigger", ""))

        # Device trigger → prefer entity_id-based triggers
        if platform == "device":
            warnings.append(
                "Trigger uses `device` platform with `device_id` — prefer "
                "`state` or `event` trigger with `entity_id` when possible "
                "(device_id breaks on re-add)."
                + _ref(skill_prefix, "device-control.md#entity-id-vs-device-id")
            )

        # Template trigger — specific shapes first, generic fallback after.
        if platform == "template":
            vt = trigger.get("value_template", "")
            if isinstance(vt, str):
                initial = len(warnings)
                if _RE_NUMERIC_CMP.search(vt):
                    warnings.append(
                        "Trigger uses template with float/int comparison — "
                        "use native `numeric_state` trigger instead "
                        "(e.g., `platform: numeric_state, entity_id: sensor.temp, above: 30`)."
                        + _ref(
                            skill_prefix,
                            "automation-patterns.md#trigger-types",
                        )
                    )
                if _RE_IS_STATE.search(vt):
                    warnings.append(
                        "Trigger uses template with `is_state()` — use "
                        "native `state` trigger instead "
                        "(e.g., `platform: state, entity_id: light.x, to: 'on'`)."
                        + _ref(
                            skill_prefix,
                            "automation-patterns.md#trigger-types",
                        )
                    )
                # Generic fallback for unmatched template triggers (issue #1011).
                if len(warnings) == initial and _RE_ANY_TEMPLATE.search(vt):
                    warnings.append(
                        "Trigger uses `template` platform — if this maps to a native option "
                        "(`state`, `numeric_state`, `time`, `time_pattern`, `sun`, `zone`, "
                        "`event`), use that instead. Native triggers are event-driven; "
                        "template triggers re-evaluate on every state change."
                        + _ref(
                            skill_prefix,
                            "automation-patterns.md#trigger-types",
                        )
                    )


# ---------------------------------------------------------------------------
# Mode + motion check
# ---------------------------------------------------------------------------


def _check_mode_motion(
    config: dict[str, Any], warnings: list[str], skill_prefix: str | None
) -> None:
    """Detect mode:single (default) with motion triggers and delay/wait."""
    mode = config.get("mode", "single")
    if mode != "single":
        return

    triggers = _as_list(config.get("trigger", []))
    has_motion = any(
        isinstance(t, dict)
        and any(
            isinstance(e, str) and _RE_MOTION.search(e)
            for e in _as_list(t.get("entity_id", []))
        )
        for t in triggers
    )
    if not has_motion:
        return

    if _has_delay_or_wait(config.get("action", [])):
        warnings.append(
            "Automation uses motion trigger with delay/wait but "
            "`mode: single` (default) — consider `mode: restart` so "
            "re-triggers reset the timer."
            + _ref(skill_prefix, "automation-patterns.md#automation-modes")
        )


def _has_delay_or_wait_in_nested(action: dict) -> bool:
    for key in ("then", "else", "default", "sequence"):
        if key in action and _has_delay_or_wait(action[key]):
            return True
    if "choose" in action:
        for opt in _as_list(action["choose"]):
            if isinstance(opt, dict) and _has_delay_or_wait(opt.get("sequence", [])):
                return True
    if "repeat" in action and isinstance(action["repeat"], dict):
        if _has_delay_or_wait(action["repeat"].get("sequence", [])):
            return True
    return False


def _has_delay_or_wait(actions: Any) -> bool:
    """Recursively check if any action uses delay or wait."""
    for action in _as_list(actions):
        if not isinstance(action, dict):
            continue
        if any(k in action for k in ("delay", "wait_for_trigger", "wait_template")):
            return True
        if _has_delay_or_wait_in_nested(action):
            return True
    return False


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _as_list(val: Any) -> list:
    """Coerce a value to a list."""
    if isinstance(val, list):
        return val
    return [val] if val else []


def _dedupe(warnings: list[str]) -> list[str]:
    """Remove duplicate warnings while preserving order."""
    seen: set[str] = set()
    result: list[str] = []
    for w in warnings:
        if w not in seen:
            seen.add(w)
            result.append(w)
    return result
