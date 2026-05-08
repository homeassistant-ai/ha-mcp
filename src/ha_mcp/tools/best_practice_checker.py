"""Reactive best-practice checker for HA automation/script configs.

Stateless payload inspection — returns warnings pointing to skill reference
files. Zero overhead on clean calls (returns empty list).

Warnings include skill:// URIs so the LLM can read the relevant reference
file via the bundled SkillsDirectoryProvider. The ``skill_prefix`` kwarg
lets callers pass any URL prefix (e.g., a GitHub mirror) when skill://
isn't reachable, or ``None`` to omit references entirely.

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
# sun.sun entity references
_RE_SUN = re.compile(r"(?:is_state|state_attr|states)\s*\(\s*['\"]sun\.sun['\"]")
# states('x') in [...] or states('x') in (...)
_RE_STATE_IN = re.compile(r"states\s*\([^)]+\)\s+in\s+[\[(]")
# Unsafe direct state access: states.sensor.x.state
_RE_DIRECT_STATE = re.compile(r"\bstates\.\w+\.\w+\.state\b")
# Motion entity pattern
_RE_MOTION = re.compile(r"binary_sensor\.\w*motion", re.IGNORECASE)


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

    # Action tree (wait_template + nested conditions)
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
            _check_template_string(cond, warnings, skill_prefix)
        elif isinstance(cond, dict):
            if cond.get("condition") == "template":
                vt = cond.get("value_template", "")
                if isinstance(vt, str):
                    _check_template_string(vt, warnings, skill_prefix)
            # Recurse into compound conditions (and/or/not)
            nested = cond.get("conditions")
            if nested:
                _check_condition_templates(nested, warnings, skill_prefix)


def _check_template_string(
    template: str, warnings: list[str], skill_prefix: str | None
) -> None:
    """Check a single template string for known anti-patterns."""
    if _RE_NUMERIC_CMP.search(template):
        warnings.append(
            "Condition uses template with float/int comparison — use native "
            "`numeric_state` condition instead."
            + _ref(skill_prefix, "automation-patterns.md#native-conditions")
        )
    if _RE_SUN.search(template):
        warnings.append(
            "Condition uses template referencing `sun.sun` — use native "
            "`sun` condition instead."
            + _ref(skill_prefix, "automation-patterns.md#native-conditions")
        )
    elif _RE_IS_STATE.search(template):
        # Only flag if not already flagged as sun pattern
        warnings.append(
            "Condition uses template with `is_state()` — use native "
            "`state` condition instead."
            + _ref(skill_prefix, "automation-patterns.md#native-conditions")
        )
    if _RE_NOW_TIME.search(template):
        warnings.append(
            "Condition uses template with `now().hour/minute` — use native "
            "`time` condition instead."
            + _ref(skill_prefix, "automation-patterns.md#native-conditions")
        )
    if _RE_WEEKDAY.search(template):
        warnings.append(
            "Condition uses template for day-of-week check — use native "
            "`time` condition with `weekday:` list instead."
            + _ref(skill_prefix, "automation-patterns.md#native-conditions")
        )
    if _RE_STATE_IN.search(template):
        warnings.append(
            "Condition uses template with `states(...) in [...]` — use native "
            "`state` condition with `state:` list instead."
            + _ref(skill_prefix, "automation-patterns.md#native-conditions")
        )
    if _RE_DIRECT_STATE.search(template):
        warnings.append(
            "Template uses `states.domain.entity.state` direct access which "
            "errors if entity doesn't exist — use `states('entity_id')` "
            "function instead."
            + _ref(skill_prefix, "template-guidelines.md#common-patterns")
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
    """Walk action tree checking for wait_template and nested conditions."""
    for action in _as_list(actions):
        if not isinstance(action, dict):
            continue

        if "wait_template" in action:
            warnings.append(
                "Action uses `wait_template` — consider `wait_for_trigger` "
                "with a state trigger (note: different semantics — "
                "`wait_for_trigger` waits for a *change*, `wait_template` "
                "passes immediately if already true)."
                + _ref(skill_prefix, "automation-patterns.md#wait-actions")
            )

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

        # Template trigger with detectable native alternative
        if platform == "template":
            vt = trigger.get("value_template", "")
            if isinstance(vt, str):
                if _RE_NUMERIC_CMP.search(vt):
                    warnings.append(
                        "Trigger uses template with float/int comparison — "
                        "use native `numeric_state` trigger instead."
                        + _ref(
                            skill_prefix,
                            "automation-patterns.md#trigger-types",
                        )
                    )
                if _RE_IS_STATE.search(vt):
                    warnings.append(
                        "Trigger uses template with `is_state()` — use "
                        "native `state` trigger instead."
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
