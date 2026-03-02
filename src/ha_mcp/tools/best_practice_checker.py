"""Reactive best-practice checker for HA automation/script configs.

Stateless payload inspection — returns warnings pointing to skill reference
files. Zero overhead on clean calls (returns empty list).

Anti-patterns sourced from:
  https://github.com/homeassistant-ai/skills
  skill://home-assistant-best-practices
"""

from __future__ import annotations

import re
from typing import Any

_SKILL = "skill://home-assistant-best-practices/references"

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


def check_automation_config(config: dict[str, Any]) -> list[str]:
    """Return best-practice warnings for an automation config."""
    if "use_blueprint" in config:
        return []

    warnings: list[str] = []

    # Condition templates
    _check_condition_templates(config.get("condition", []), warnings)

    # Action tree (wait_template + nested conditions)
    _check_action_tree(config.get("action", []), warnings)

    # Trigger templates + device_id
    _check_triggers(config.get("trigger", []), warnings)

    # Mode vs motion pattern
    _check_mode_motion(config, warnings)

    return _dedupe(warnings)


def check_script_config(config: dict[str, Any]) -> list[str]:
    """Return best-practice warnings for a script config."""
    if "use_blueprint" in config:
        return []

    warnings: list[str] = []
    _check_action_tree(config.get("sequence", []), warnings)
    return _dedupe(warnings)


# ---------------------------------------------------------------------------
# Condition template checks
# ---------------------------------------------------------------------------


def _check_condition_templates(conditions: Any, warnings: list[str]) -> None:
    """Check condition tree for template anti-patterns."""
    for cond in _as_list(conditions):
        if isinstance(cond, str) and "{{" in cond:
            # Shorthand template condition
            _check_template_string(cond, warnings)
        elif isinstance(cond, dict):
            if cond.get("condition") == "template":
                vt = cond.get("value_template", "")
                if isinstance(vt, str):
                    _check_template_string(vt, warnings)
            # Recurse into compound conditions (and/or/not)
            nested = cond.get("conditions")
            if nested:
                _check_condition_templates(nested, warnings)


def _check_template_string(template: str, warnings: list[str]) -> None:
    """Check a single template string for known anti-patterns."""
    if _RE_NUMERIC_CMP.search(template):
        warnings.append(
            "Condition uses template with float/int comparison — use native "
            f"`numeric_state` condition instead. "
            f"See {_SKILL}/automation-patterns.md#native-conditions"
        )
    if _RE_SUN.search(template):
        warnings.append(
            "Condition uses template referencing `sun.sun` — use native "
            f"`sun` condition instead. "
            f"See {_SKILL}/automation-patterns.md#native-conditions"
        )
    elif _RE_IS_STATE.search(template):
        # Only flag if not already flagged as sun pattern
        warnings.append(
            "Condition uses template with `is_state()` — use native "
            f"`state` condition instead. "
            f"See {_SKILL}/automation-patterns.md#native-conditions"
        )
    if _RE_NOW_TIME.search(template):
        warnings.append(
            "Condition uses template with `now().hour/minute` — use native "
            f"`time` condition instead. "
            f"See {_SKILL}/automation-patterns.md#native-conditions"
        )
    if _RE_WEEKDAY.search(template):
        warnings.append(
            "Condition uses template for day-of-week check — use native "
            f"`time` condition with `weekday:` list instead. "
            f"See {_SKILL}/automation-patterns.md#native-conditions"
        )
    if _RE_STATE_IN.search(template):
        warnings.append(
            "Condition uses template with `states(...) in [...]` — use native "
            f"`state` condition with `state:` list instead. "
            f"See {_SKILL}/automation-patterns.md#native-conditions"
        )
    if _RE_DIRECT_STATE.search(template):
        warnings.append(
            "Template uses `states.domain.entity.state` direct access which "
            "errors if entity doesn't exist — use `states('entity_id')` "
            f"function instead. "
            f"See {_SKILL}/template-guidelines.md#common-patterns"
        )


# ---------------------------------------------------------------------------
# Action tree checks
# ---------------------------------------------------------------------------


def _check_action_tree(actions: Any, warnings: list[str]) -> None:
    """Walk action tree checking for wait_template and nested conditions."""
    for action in _as_list(actions):
        if not isinstance(action, dict):
            continue

        if "wait_template" in action:
            warnings.append(
                "Action uses `wait_template` — consider `wait_for_trigger` "
                "with a state trigger (note: different semantics — "
                "`wait_for_trigger` waits for a *change*, `wait_template` "
                "passes immediately if already true). "
                f"See {_SKILL}/automation-patterns.md#wait-actions"
            )

        # Nested conditions in choose/if/repeat
        if "choose" in action:
            for option in _as_list(action["choose"]):
                if isinstance(option, dict):
                    _check_condition_templates(
                        option.get("conditions", []), warnings
                    )
                    _check_action_tree(option.get("sequence", []), warnings)

        if "if" in action:
            _check_condition_templates(action["if"], warnings)

        for key in ("then", "else", "default"):
            nested = action.get(key)
            if isinstance(nested, list):
                _check_action_tree(nested, warnings)

        if "repeat" in action and isinstance(action["repeat"], dict):
            repeat = action["repeat"]
            _check_condition_templates(repeat.get("while", []), warnings)
            _check_condition_templates(repeat.get("until", []), warnings)
            _check_action_tree(repeat.get("sequence", []), warnings)


# ---------------------------------------------------------------------------
# Trigger checks
# ---------------------------------------------------------------------------


def _check_triggers(triggers: Any, warnings: list[str]) -> None:
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
                "(device_id breaks on re-add). "
                f"See {_SKILL}/device-control.md#entity-id-vs-device-id"
            )

        # Template trigger with detectable native alternative
        if platform == "template":
            vt = trigger.get("value_template", "")
            if isinstance(vt, str):
                if _RE_NUMERIC_CMP.search(vt):
                    warnings.append(
                        "Trigger uses template with float/int comparison — "
                        "use native `numeric_state` trigger instead. "
                        f"See {_SKILL}/automation-patterns.md#trigger-types"
                    )
                if _RE_IS_STATE.search(vt):
                    warnings.append(
                        "Trigger uses template with `is_state()` — use "
                        "native `state` trigger instead. "
                        f"See {_SKILL}/automation-patterns.md#trigger-types"
                    )


# ---------------------------------------------------------------------------
# Mode + motion check
# ---------------------------------------------------------------------------


def _check_mode_motion(config: dict[str, Any], warnings: list[str]) -> None:
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
            "re-triggers reset the timer. "
            f"See {_SKILL}/automation-patterns.md#automation-modes"
        )


def _has_delay_or_wait(actions: Any) -> bool:
    """Recursively check if any action uses delay or wait."""
    for action in _as_list(actions):
        if not isinstance(action, dict):
            continue
        if any(k in action for k in ("delay", "wait_for_trigger", "wait_template")):
            return True
        for key in ("then", "else", "default", "sequence"):
            if key in action and _has_delay_or_wait(action[key]):
                return True
        if "choose" in action:
            for opt in _as_list(action["choose"]):
                if isinstance(opt, dict) and _has_delay_or_wait(
                    opt.get("sequence", [])
                ):
                    return True
        if "repeat" in action and isinstance(action["repeat"], dict):
            if _has_delay_or_wait(action["repeat"].get("sequence", [])):
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
