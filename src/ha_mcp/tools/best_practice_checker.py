"""Reactive best-practice checker for HA automation/script configs.

Stateless payload inspection. Returns a :class:`BestPracticeCheckResult`
(a ``list[str]`` subclass) carrying warning strings and the set of skill
files those warnings reference. Zero overhead on clean configs — the
returned result is empty and ``referenced_files`` is the empty set.

Each warning ends with a 2-route ' See ...' suffix naming every
LLM-discoverable way to pull the relevant skill content:

1. ``skill://`` resource URI — for clients that auto-fetch resource URIs.
2. ``ha_get_skill_guide(skill=..., file=...)`` — explicit tool call,
   works on every MCP client regardless of resource-fetch support.

The write tools also auto-embed the matching section into the next
response via ``referenced_files`` and accept a ``MandatoryBPS`` opt-out
parameter. Neither is advertised in the warning suffix by design —
see ``util_helpers._SKILL_CONTENT_OPTOUT_HINT`` for the param contract.

The ``skill_prefix`` kwarg lets callers pass any URL prefix (e.g., a
GitHub mirror) when ``skill://`` isn't reachable, or ``None`` to omit
the suffix entirely — ``None`` signals skills are disabled server-wide,
in which case neither route can resolve (the URI fails and the
tool isn't registered), so naming them would mislead.

Each warning carries the native alternative inline (a concrete example
or short explanation) before the routes, so even clients that ignore
both routes still receive actionable guidance.

The checker covers two layers:

1. *Specific* detectors for known anti-pattern shapes — each emits a tailored
   message that names the native alternative concretely.
2. A *generic* fallback that fires when ``{{ ... }}`` or ``{% ... %}`` shows
   up in a logic position (condition / trigger / wait_template / target field)
   without matching a specific pattern. This catches new template misuse
   without waiting for a regex to be added.

Allowlist by design — these positions are NOT walked by any recursion path,
so templates in them never trigger a warning even when present. They are the
documented legitimate dynamic-data positions per
``template-guidelines.md#when-templates-are-appropriate``:

* Action ``data.*`` fields (notification messages, brightness, volume, etc.)
* Notification ``message`` / ``title`` bodies
* Action ``event_data.*`` (HA evaluates event_data as a template at runtime)
* Top-level ``variables.*``
* Action ``service_data.*`` (legacy alias for ``data``)

The allowlist covers template *content*. Key *order* inside a ``variables``
block is separately load-bearing — HA renders one key at a time — so those
blocks get their own ordering-only pass (:func:`_check_variables_order`).

Anti-patterns sourced from:
  https://github.com/homeassistant-ai/skills
  skill://home-assistant-best-practices
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

_SKILL_URI_PREFIX = "skill://home-assistant-best-practices/references"
_DEFAULT_SKILL_PREFIX = _SKILL_URI_PREFIX
_SKILL_NAME = "home-assistant-best-practices"


class BestPracticeCheckResult(list[str]):
    """Warning list with an attached set of referenced skill files.

    Behaves as a plain ``list[str]`` for all call sites — ``len()``,
    indexing, iteration, equality with ``[...]`` all work unchanged.
    ``referenced_files`` is added on top so the write tools can resolve
    and embed the relevant skill bodies into responses.

    Use :func:`_emit` to append warnings; direct ``append``/``extend``
    skip the ``referenced_files`` mirror. Slicing, ``list()`` coercion,
    and ``copy.copy``/``copy.deepcopy`` all return plain ``list[str]``
    without the attribute — no current call site does any of these.
    """

    __slots__ = ("referenced_files",)

    referenced_files: set[str]

    def __init__(self, items: list[str] | None = None) -> None:
        super().__init__(items or [])
        self.referenced_files = set()


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
# Date-component checks: now().date(), now().year/month/day.
# `\b` after year/month/day prevents matching `day_of_week`/`day_of_year`/etc.;
# `(?!\s*\()` rejects method-call shapes like `now().day()` that don't exist
# in HA's Jinja env.
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
# Duration/recency checks via last_changed or last_updated arithmetic.
# Catches the shapes that compute "how long since X changed", all of which map
# to the native ``for:`` field:
#   now() - X.last_changed                          (forward subtraction)
#   X.last_changed (<|<=|>|>=) now() - <delta>      (reversed; the subtraction is
#       required — a bare ``X.last_changed < now()`` is *always* true and carries
#       no duration, so it is intentionally NOT flagged: ``for:`` cannot express it)
#   now() (<|<=|>|>=) X.last_changed + <delta>      (now() on the left)
#   X.last_changed + <delta> (<|<=|>|>=) now()      (delta added to the attribute)
#   now().timestamp() - X.last_changed.timestamp()  (epoch subtraction)
#   as_timestamp(now()) - as_timestamp(X.last_changed)        (function form)
#   as_timestamp(now()) - X.last_changed | as_timestamp       (filter form)
# Every alternation requires a dotted qualifier ending on a word boundary, so
# bare Jinja variables literally named ``last_changed`` and longer look-alike
# attributes (``last_changed_at``) are not falsely flagged; a leading ``word.``
# (``trigger.``, ``states.sensor.x.``) is the minimum.
# Intentionally NOT matched (heuristic limits, low value): the reversed
# as_timestamp operand order, the reversed ``.timestamp()`` order, and
# ``state_attr(e, 'last_changed')`` / ``states('e').last_changed`` — the latter
# two are not valid ways to read a state's ``last_changed`` in HA anyway. These
# fall through to the generic template fallback in condition/trigger positions.
_RE_DURATION_MATH = re.compile(
    r"\bnow\(\)\s*-\s*(?:\w+\.)+last_(?:changed|updated)\b"
    r"|\b(?:\w+\.)+last_(?:changed|updated)\b\s*[<>]=?\s*now\(\)\s*-"
    r"|\bnow\(\)\s*[<>]=?\s*(?:\w+\.)+last_(?:changed|updated)\b\s*\+"
    r"|\b(?:\w+\.)+last_(?:changed|updated)\b\s*\+\s*[^<>{}]+?[<>]=?\s*now\(\)"
    r"|\bnow\(\)\.timestamp\(\)\s*-\s*(?:\w+\.)+last_(?:changed|updated)\.timestamp\(\)"
    r"|\bas_timestamp\(\s*now\(\)\s*\)\s*-\s*as_timestamp\([^)]*\.last_(?:changed|updated)\b"
    r"|\bas_timestamp\(\s*now\(\)\s*\)\s*-\s*(?:\w+\.)+last_(?:changed|updated)\b\s*\|\s*as_timestamp\b"
)
# Motion entity pattern
_RE_MOTION = re.compile(r"binary_sensor\.\w*motion", re.IGNORECASE)
# Any Jinja template marker — catch-all and target-field scan.
_RE_ANY_TEMPLATE = re.compile(r"\{\{|\{%")
# `this.X` self-reference (e.g. `{{ this.entity_id }}`)
_RE_THIS_REFERENCE = re.compile(r"\bthis\s*\.\s*\w+")

# --- Variables-block ordering scan (see _check_variables_order) -------------
# Opening delimiter of a Jinja span. Only text inside `{{ }}` / `{% %}` can
# read a variable, so a plain string value never counts as a reference. The
# closing delimiter is found by walking forward (see _span_end) instead of by a
# lazy `.*?`: only a walk that steps over string literals knows that the `}}`
# in `{{ "}}" ~ later }}` is text and does not end the span.
_RE_SPAN_OPEN = re.compile(r"\{\{|\{%|\{#")
_SPAN_CLOSERS = {"{{": "}}", "{%": "%}", "{#": "#}"}
# Quoted literal inside a span — a sibling's name appearing in one (including
# as a dict subscript, `x['meldung']`) is text, not a read. Jinja takes Python's
# string rules, so a backslash escape has to be consumed as one unit or the
# literal ends early and its tail is scanned as code (`{{ 'don\'t drop x' }}`).
# ``re.DOTALL`` so an escaped newline inside a literal is consumed with it,
# matching Jinja's own ``string_re``; without it the literal goes unrecognised
# and its contents are scanned as code.
_RE_STRING_LITERAL = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"", re.DOTALL)
# Statement tag name, past the optional whitespace-control marker. Jinja
# accepts `-` and `+` there, so both have to be stepped over.
_RE_STMT_TAG = re.compile(r"[-+]?\s*([^\W\d]\w*)")
# `{% set a, b = ... %}` / `{% with a = ... %}` — targets left of the `=`, the
# expression right of it. Bindings are applied by position, not collected up
# front: the right-hand side is read *before* the binding takes effect (see
# _referenced_names). `=(?!=)` so a comparison in the body is not read as an
# assignment.
_RE_ASSIGN = re.compile(r"[-+]?\s*(?:set|with)\s+([^=]*?)\s*=(?!=)(.*)", re.DOTALL)
# `{% set x %}...{% endset %}` — the block form binds without an `=`.
_RE_BLOCK_SET = re.compile(r"[-+]?\s*set\s+([^\W\d]\w*)\s*[-+]?\s*$")
# `{% for a, b in expr if cond %}` — the targets bind for the loop body, the
# iterable is evaluated in the enclosing scope, and the filter clause sees the
# targets already bound.
_RE_FOR = re.compile(r"[-+]?\s*for\s+(.*?)\s+in\s+(.*?)(?:\s+if\s+(.*?))?\s*[-+]?$", re.DOTALL)
# `{% macro name(a, b=1) %}` — the name binds in the enclosing scope, the
# parameters inside the macro body.
_RE_MACRO = re.compile(r"[-+]?\s*macro\s+([^\W\d]\w*)\s*\((.*?)\)", re.DOTALL)
# A bare identifier, used to tell a binding target (`x`) from an assignment
# into an existing object (`ns.x`, `d[k]`), which reads rather than binds.
_RE_PLAIN_NAME = re.compile(r"[^\W\d]\w*")
# `| name` is a filter, never a variable read.
_RE_FILTER_NAME = re.compile(r"\|\s*[^\W\d]\w*")
# `is name` / `is not name` — a Jinja test, never a variable read.
_RE_TEST_NAME = re.compile(r"\bis\s+(?:not\s+)?[^\W\d]\w*")
# `name=` inside a call is a keyword argument, never a read (`{{ dict(x=1) }}`).
_RE_KEYWORD_ARG = re.compile(r"[^\W\d]\w*\s*=(?!=)")
# `.name` is attribute access on whatever sits to its left, not a read of
# `name`. Stripped as a pass rather than handled by a lookbehind on the
# identifier, because Jinja also parses the spaced form (`wetter . meldung`)
# as attribute access and a lookbehind cannot reach across the space.
_RE_ATTRIBUTE = re.compile(r"\.\s*[^\W\d]\w*")
# An identifier read. A longer name is handled by the greedy `\w*` alone, which
# consumes `offene_tueren_extra` whole rather than reporting `offene_tueren`.
# The lookbehind covers a preceding word char, which only arises after a digit
# (`e3` in the literal `2.5e3`).
# `[^\W\d]` rather than `[A-Za-z_]` because Jinja inherits Python's identifier
# rules, so `über` and `变量` are valid variable names.
_RE_JINJA_IDENT = re.compile(r"(?<![\w.])[^\W\d]\w*")
# Jinja's own keywords and literals. A sibling sharing one of these names is
# never read by the token that spells it. HA's template globals (`states`,
# `now`, `trigger`, ...) are deliberately NOT listed — that set drifts with HA,
# and a stale copy would suppress real warnings; it stays a known false
# positive instead.
_JINJA_KEYWORDS = frozenset(
    {
        "and", "as", "autoescape", "block", "break", "call", "context",
        "continue", "do", "elif", "else", "endautoescape", "endblock",
        "endcall", "endfilter", "endfor", "endif", "endmacro", "endraw",
        "endset", "endtrans", "endwith", "extends", "false", "filter", "for",
        "from", "if", "ignore", "import", "in", "include", "is", "loop",
        "macro", "missing", "none", "not", "or", "pluralize", "raw",
        "recursive", "required", "scoped", "set", "trans", "true", "with",
        "without", "False", "None", "True",
    }
)
# Tags that open a variable scope, mapped to the tag that closes it. A binding
# made inside one does not survive it: `{% for %}{% set x = 1 %}{% endfor %}`
# leaves `x` undefined again, so bindings live on a stack rather than in one
# flat set.
_SCOPE_OPENERS = {
    "for": "endfor",
    "macro": "endmacro",
    "with": "endwith",
    "call": "endcall",
}
_SCOPE_CLOSERS = frozenset(_SCOPE_OPENERS.values())

# Target sub-fields scanned for templates. These are the only keys allowed
# under ``target:`` in HA's modern action schema.
_TARGET_FIELDS = ("entity_id", "device_id", "area_id", "floor_id", "label_id")

# Keys that hold the service/action name in an action step. HA accepts both
# ``service:`` (legacy) and ``action:`` (modern, 2024+) for the same field.
_SERVICE_KEYS = ("service", "action")

# Purpose-specific trigger/condition keys renamed in HA 2026.7 — the old keys
# no longer load (home-assistant/core#174463).
_RENAMED_TRIGGER_KEYS = {
    "battery.low": "battery.became_low",
    "battery.not_low": "battery.no_longer_low",
    "lawn_mower.docked": "lawn_mower.returned_to_dock",
    "schedule.turned_on": "schedule.block_started",
    "schedule.turned_off": "schedule.block_ended",
    "timer.time_remaining": "timer.remaining_time_reached",
    "update.update_became_available": "update.became_available",
    "vacuum.docked": "vacuum.returned_to_dock",
}
_RENAMED_CONDITION_KEYS = {
    "climate.target_temperature": "climate.is_target_temperature",
    "climate.target_humidity": "climate.is_target_humidity",
}
# Trigger ``options.behavior`` values renamed in HA 2026.7: any→each,
# last→all (home-assistant/core#173259). The old values still load but raise
# an HA repair issue and face removal. Condition ``options.behavior`` keeps
# ``any``/``all`` — conditions are never flagged.
_DEPRECATED_TRIGGER_BEHAVIOR = {"any": "each", "last": "all"}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_automation_config(
    config: dict[str, Any],
    *,
    skill_prefix: str | None = _DEFAULT_SKILL_PREFIX,
) -> BestPracticeCheckResult:
    """Return a best-practice scan result for an automation config.

    The return value behaves as a ``list[str]`` of warning strings for
    back-compat (so ``result == []`` and iteration work unchanged), and
    additionally exposes ``referenced_files`` — the set of skill file
    paths (relative to the skill root, e.g.
    ``"references/automation-patterns.md"``) referenced by at least one
    emitted warning. Callers use that set to fetch the bodies via
    :func:`ha_mcp.utils.skill_loader.resolve_skill_files` and embed them
    in the response under ``skill_content``.

    Args:
        config: The automation configuration dict.
        skill_prefix: Base URI for skill references (e.g.
            ``"skill://home-assistant-best-practices/references"``).
            Pass ``None`` when skills are disabled server-wide — warnings
            still fire but the entire ' See ...' suffix is suppressed
            (neither route resolves when skills are off).
    """
    if "use_blueprint" in config:
        return BestPracticeCheckResult()

    warnings = BestPracticeCheckResult()

    # Read the canonical 2024.10+ plural root keys, falling back to the singular
    # aliases. The internal pipeline always pre-normalizes to plural, so the
    # fallback is defensive for direct/public callers of check_automation_config
    # (HA accepts both forms). Mirrors _check_triggers' platform/trigger tolerance.
    # Condition templates
    _check_condition_templates(
        config.get("conditions", config.get("condition", [])), warnings, skill_prefix
    )

    # Action tree (wait_template + nested conditions + target templates)
    _check_action_tree(
        config.get("actions", config.get("action", [])), warnings, skill_prefix
    )

    # Trigger templates + device_id
    _check_triggers(
        config.get("triggers", config.get("trigger", [])), warnings, skill_prefix
    )

    # Mode vs motion pattern
    _check_mode_motion(config, warnings, skill_prefix)

    # Key order inside the top-level variables blocks. Both render one key at a
    # time, so a forward reference between siblings is silently undefined.
    for block_key in ("variables", "trigger_variables"):
        _check_variables_order(config.get(block_key), warnings, skill_prefix, block_key)

    _dedupe_inplace(warnings)
    return warnings


def check_script_config(
    config: dict[str, Any],
    *,
    skill_prefix: str | None = _DEFAULT_SKILL_PREFIX,
) -> BestPracticeCheckResult:
    """Return a best-practice scan result for a script config.

    See :func:`check_automation_config` for the return shape and the
    ``skill_prefix`` contract.
    """
    if "use_blueprint" in config:
        return BestPracticeCheckResult()

    warnings = BestPracticeCheckResult()
    _check_action_tree(config.get("sequence", []), warnings, skill_prefix)
    _check_variables_order(config.get("variables"), warnings, skill_prefix, "variables")
    _dedupe_inplace(warnings)
    return warnings


# ---------------------------------------------------------------------------
# Warning emission + skill-reference helpers
# ---------------------------------------------------------------------------


def _emit(
    warnings: BestPracticeCheckResult,
    message: str,
    skill_prefix: str | None,
    file_ref: str,
) -> None:
    """Append a warning with the 2-route ' See ...' suffix and track the file.

    Args:
        warnings: Accumulator (also holds ``referenced_files``).
        message: Human-readable warning body — the inline alternative.
        skill_prefix: When set, embedded as the ``skill://`` URI route.
            When ``None``, the URI route is omitted but the other two
            routes still appear.
        file_ref: File path relative to the ``references/`` directory of
            the home-assistant-best-practices skill, optionally with a
            ``#anchor`` suffix
            (e.g. ``"automation-patterns.md#native-conditions"``). The
            anchor is preserved end-to-end: in the ``skill://`` URI for
            display, and in ``referenced_files`` so the auto-embed path
            ships only the matching markdown section instead of the
            whole 10-20 KB reference file.
    """
    warnings.append(message + _skill_route_suffix(skill_prefix, file_ref))
    warnings.referenced_files.add(f"references/{file_ref}")


def _skill_route_suffix(skill_prefix: str | None, file_ref: str) -> str:
    """Build the ' See ...' suffix naming the available skill access routes.

    When ``skill_prefix`` is ``None`` the entire suffix is suppressed —
    skills are off server-wide, so neither route resolves. Otherwise the
    suffix names both so the LLM has a working path regardless of which
    mechanism its client supports:

    1. ``skill://`` URI — for clients that auto-fetch resource URIs.
       Anchor preserved.
    2. ``ha_get_skill_guide(skill=..., file=...)`` — explicit tool call,
       works on every MCP client. Anchor stripped (the tool reads the
       whole file).

    Auto-embed of the matching section still happens in the next
    write's response, driven by ``referenced_files``; the
    ``MandatoryBPS`` opt-out param is not named here by design.
    """
    if not skill_prefix:
        # Skills feature is disabled server-wide; none of the routes work.
        # Matches the historical no-suffix behaviour.
        return ""
    bare_file = f"references/{file_ref.split('#', 1)[0]}"
    routes = [
        f"{skill_prefix}/{file_ref}",
        f"call ha_get_skill_guide(skill={_SKILL_NAME!r}, file={bare_file!r})",
    ]
    return " See " + " | ".join(routes)


# ---------------------------------------------------------------------------
# Condition template checks
# ---------------------------------------------------------------------------


def _check_condition_templates(
    conditions: Any, warnings: BestPracticeCheckResult, skill_prefix: str | None
) -> None:
    """Check condition tree for template anti-patterns."""
    for cond in _as_list(conditions):
        if isinstance(cond, str) and "{{" in cond:
            # Shorthand template condition
            _check_template_string(cond, warnings, skill_prefix, "condition")
        elif isinstance(cond, dict):
            # 2026.7 renamed purpose-specific condition keys — old keys no
            # longer load.
            condition_key = cond.get("condition")
            renamed_condition = (
                _RENAMED_CONDITION_KEYS.get(condition_key)
                if isinstance(condition_key, str)
                else None
            )
            if renamed_condition:
                _emit(
                    warnings,
                    f"Condition key `{condition_key}` was renamed to "
                    f"`{renamed_condition}` in HA 2026.7 and the old key no "
                    f"longer loads — use `condition: {renamed_condition}`.",
                    skill_prefix,
                    "automation-patterns.md#native-conditions",
                )
            if cond.get("condition") == "template":
                vt = cond.get("value_template", "")
                if isinstance(vt, str):
                    _check_template_string(vt, warnings, skill_prefix, "condition")
            else:
                # Non-template conditions (numeric_state, state, etc.) can
                # still carry a `value_template` field (numeric_state uses one
                # to compute the numeric value being compared). Scan it too,
                # otherwise these templates slip past every detector.
                vt = cond.get("value_template", "")
                if isinstance(vt, str) and "{{" in vt:
                    _check_template_string(vt, warnings, skill_prefix, "condition")
            # Recurse into compound conditions (and/or/not)
            nested = cond.get("conditions")
            if nested:
                _check_condition_templates(nested, warnings, skill_prefix)


def _check_template_string(
    template: str,
    warnings: BestPracticeCheckResult,
    skill_prefix: str | None,
    position: str,
) -> None:
    """Check a single template string for known anti-patterns.

    ``position`` is currently only "condition" (the function is called from
    ``_check_condition_templates``). It's parameterized so both the warning
    prefix AND the suggestion text adapt if a future caller passes "trigger".
    The native shapes named here (numeric_state, state, time, sun) work as
    both conditions and triggers in HA — only the noun changes.

    Detection is split across three helpers (comparison/sun/is_state, time
    patterns, then state-list/direct-state/duration) purely to keep each
    function's branching low. They run in the same order as before and
    share ``duration_match`` so the split is invisible to callers.
    """
    initial_count = len(warnings)
    label = position.capitalize()

    # Duration/recency math (e.g. `(now() - X.last_changed).total_seconds() > 300`)
    # also contains a numeric comparison, so it matches `_RE_NUMERIC_CMP` too. But its
    # correct native replacement is the `for:` field, NOT `numeric_state`. Suppress the
    # numeric_state suggestion when duration math is present so the user isn't handed
    # two conflicting native alternatives for one template (the duration warning below
    # fires instead).
    duration_match = _RE_DURATION_MATH.search(template)

    _check_template_comparison_patterns(
        template, warnings, skill_prefix, position, label, duration_match
    )
    _check_template_time_patterns(template, warnings, skill_prefix, position, label)
    _check_template_state_and_duration_patterns(
        template, warnings, skill_prefix, position, label, duration_match
    )

    # Generic fallback: any Jinja in this logic position that didn't match
    # a specific detector. Catches new anti-patterns (issue #1011) and
    # reframes #695 from "enumerate bad shapes" to "surface every template
    # in a logic position". Specific detectors above keep their tailored
    # messages.
    if len(warnings) == initial_count and _RE_ANY_TEMPLATE.search(template):
        _emit(
            warnings,
            f"Template detected in {position} — if this maps to a native option "
            "(`numeric_state`, `state`, `time`, `sun`, `zone`, `device`), use that "
            "instead. Templates fail silently at runtime and bypass schema validation.",
            skill_prefix,
            "template-guidelines.md#when-to-avoid-templates",
        )


def _check_template_comparison_patterns(
    template: str,
    warnings: BestPracticeCheckResult,
    skill_prefix: str | None,
    position: str,
    label: str,
    duration_match: re.Match[str] | None,
) -> None:
    """Flag numeric-comparison, `sun.sun`, and `is_state()` template shapes.

    Split out of :func:`_check_template_string` to keep its complexity low.
    ``duration_match`` is precomputed by the caller so the numeric-comparison
    check can suppress its suggestion when duration math is also present.
    """
    if _RE_NUMERIC_CMP.search(template) and not duration_match:
        _emit(
            warnings,
            f"{label} uses template with float/int comparison — use native "
            f"`numeric_state` {position} instead "
            f"(e.g., `{position}: numeric_state, entity_id: sensor.temp, above: 25`). "
            "Native conditions are validated at config load and don't bypass HA's schema.",
            skill_prefix,
            "automation-patterns.md#native-conditions",
        )
    if _RE_SUN.search(template):
        _emit(
            warnings,
            f"{label} uses template referencing `sun.sun` — use native "
            f"`sun` {position} instead "
            f"(e.g., `{position}: sun, after: sunset` or `before: sunrise`).",
            skill_prefix,
            "automation-patterns.md#native-conditions",
        )
    elif _RE_IS_STATE.search(template):
        # Only flag if not already flagged as sun pattern
        _emit(
            warnings,
            f"{label} uses template with `is_state()` — use native "
            f"`state` {position} instead "
            f"(e.g., `{position}: state, entity_id: light.bedroom, state: 'on'`).",
            skill_prefix,
            "automation-patterns.md#native-conditions",
        )


def _check_template_time_patterns(
    template: str,
    warnings: BestPracticeCheckResult,
    skill_prefix: str | None,
    position: str,
    label: str,
) -> None:
    """Flag `now().hour/minute`, weekday, and date-based template shapes.

    Split out of :func:`_check_template_string` to keep its complexity low.
    """
    if _RE_NOW_TIME.search(template):
        _emit(
            warnings,
            f"{label} uses template with `now().hour/minute` — use native "
            f"`time` {position} instead "
            f"(e.g., `{position}: time, after: '09:00:00', before: '17:00:00'`).",
            skill_prefix,
            "automation-patterns.md#native-conditions",
        )
    if _RE_WEEKDAY.search(template):
        _emit(
            warnings,
            f"{label} uses template for day-of-week check — use native "
            f"`time` {position} with `weekday:` list instead "
            f"(e.g., `{position}: time, weekday: ['mon', 'tue', 'wed']`).",
            skill_prefix,
            "automation-patterns.md#native-conditions",
        )
    if _RE_NOW_DATE.search(template):
        _emit(
            warnings,
            f"{label} uses date-based check (`now().date()` / `now().year/month/day`) — "
            "for one-shot date-specific firing, use a `time` trigger and self-disable via "
            "`automation.turn_off` with a hardcoded `entity_id` (the next `00:01` fire IS the "
            "target date on creation day). For recurring date logic, expose a `sensor.date` via "
            f"the `time_date` integration and use a `state` {position}.",
            skill_prefix,
            "automation-patterns.md#native-conditions",
        )


def _check_template_state_and_duration_patterns(
    template: str,
    warnings: BestPracticeCheckResult,
    skill_prefix: str | None,
    position: str,
    label: str,
    duration_match: re.Match[str] | None,
) -> None:
    """Flag `states(...) in [...]`, direct-state-access, and duration-math shapes.

    Split out of :func:`_check_template_string` to keep its complexity low.
    ``duration_match`` is precomputed by the caller.
    """
    if _RE_STATE_IN.search(template):
        _emit(
            warnings,
            f"{label} uses template with `states(...) in [...]` — use native "
            f"`state` {position} with `state:` list instead "
            f"(e.g., `{position}: state, entity_id: climate.living_room, state: ['heat', 'cool']`).",
            skill_prefix,
            "automation-patterns.md#native-conditions",
        )
    if _RE_DIRECT_STATE.search(template):
        _emit(
            warnings,
            f"{label} template uses `states.domain.entity.state` direct access which "
            "errors if entity doesn't exist — use the `states('entity_id')` "
            "function instead (returns 'unknown' if missing rather than raising).",
            skill_prefix,
            "template-guidelines.md#common-patterns",
        )
    if duration_match:
        _emit(
            warnings,
            f"{label} uses template for duration/recency check "
            "(`now() - X.last_changed/last_updated`) — use the native `for:` field "
            "on a `state` trigger or condition instead "
            "(e.g., `platform: state, entity_id: binary_sensor.motion, to: 'off', "
            "for: {minutes: 5}`). Native `for:` is event-driven and avoids repeated "
            "template evaluation on every state change.",
            skill_prefix,
            "automation-patterns.md#native-conditions",
        )


# ---------------------------------------------------------------------------
# Action tree checks
# ---------------------------------------------------------------------------


def _check_choose_actions(
    choose: Any, warnings: BestPracticeCheckResult, skill_prefix: str | None
) -> None:
    for option in _as_list(choose):
        if isinstance(option, dict):
            _check_condition_templates(
                option.get("conditions", []), warnings, skill_prefix
            )
            _check_action_tree(option.get("sequence", []), warnings, skill_prefix)


def _check_repeat_actions(
    repeat: dict, warnings: BestPracticeCheckResult, skill_prefix: str | None
) -> None:
    _check_condition_templates(repeat.get("while", []), warnings, skill_prefix)
    _check_condition_templates(repeat.get("until", []), warnings, skill_prefix)
    _check_action_tree(repeat.get("sequence", []), warnings, skill_prefix)


def _check_control_flow_actions(
    action: dict[str, Any], warnings: BestPracticeCheckResult, skill_prefix: str | None
) -> None:
    """Check choose/if/then/else/repeat/parallel/sequence sub-trees in one action.

    ``sequence`` is both a grouping action of its own
    (``cv.SCRIPT_ACTION_SEQUENCE``) and the canonical shape of a ``parallel:``
    branch — HA normalises the shorthand branch list into ``{"sequence": ...}``
    (``_SCRIPT_PARALLEL_SCHEMA`` / ``_parallel_sequence_action``). Without this
    arm only the shorthand branch was walked, so the canonical form of both was
    invisible to every check below this point.
    """
    if "choose" in action:
        _check_choose_actions(action["choose"], warnings, skill_prefix)

    if "if" in action:
        _check_condition_templates(action["if"], warnings, skill_prefix)

    # Every one of these is a `SCRIPT_SCHEMA` position, and that schema is
    # `vol.All(ensure_list, [script_action])` — a lone action mapping is valid
    # wherever a list is, so both shapes have to be walked.
    for key in ("then", "else", "default", "sequence"):
        nested = action.get(key)
        if isinstance(nested, list | dict):
            _check_action_tree(nested, warnings, skill_prefix)

    if "repeat" in action and isinstance(action["repeat"], dict):
        _check_repeat_actions(action["repeat"], warnings, skill_prefix)

    # `parallel:` runs sub-actions concurrently — same shape as `sequence`,
    # different semantics. Recurse so templates inside parallel branches
    # are inspected the same as templates inside choose/repeat sequences.
    if isinstance(action.get("parallel"), list | dict):
        _check_action_tree(action["parallel"], warnings, skill_prefix)


def _check_action_tree(
    actions: Any, warnings: BestPracticeCheckResult, skill_prefix: str | None
) -> None:
    """Walk action tree checking for wait_template, nested conditions, and target templates."""
    for action in _as_list(actions):
        if not isinstance(action, dict):
            continue

        # Inline condition steps (e.g. `- condition: template, value_template: ...`
        # in a sequence). Detect by `condition: <str>` AND no service/action key
        # present — a service-call step uses `condition:` as a legacy run-if
        # filter, not as a step kind. Without this branch, templates in
        # condition shorthand inside scripts/automation actions slipped past
        # the checker; only conditions in `if:`, `choose.conditions`, and
        # `repeat.while/until` were inspected.
        cond_kind = action.get("condition")
        if isinstance(cond_kind, str) and not any(k in action for k in _SERVICE_KEYS):
            _check_condition_templates([action], warnings, skill_prefix)

        if "wait_template" in action:
            _emit(
                warnings,
                "Action uses `wait_template` — consider `wait_for_trigger` "
                "with a state trigger (note: different semantics — "
                "`wait_for_trigger` waits for a *change*, `wait_template` "
                "passes immediately if already true).",
                skill_prefix,
                "automation-patterns.md#wait-actions",
            )

        # Templated service dispatch: `service:`/`action:` containing `{{ }}`
        # or any `service_template:` field. The native alternative is a
        # `choose` (or `if/then/else`) action that picks between hardcoded
        # service names based on state.
        _check_service_template(action, warnings, skill_prefix)

        # A `variables:` step is a legitimate template position, so it is never
        # walked for template misuse. Its key *order* is still load-bearing —
        # scanned separately.
        _check_variables_order(
            action.get("variables"), warnings, skill_prefix, "variables"
        )

        # Templates in target sub-fields. Action `data`, `event_data`,
        # `service_data`, notification message/title, and `variables` are
        # legitimate dynamic-data positions per template-guidelines.md and
        # are not walked by any recursion path here.
        target = action.get("target")
        if isinstance(target, dict):
            _check_target_dict(target, warnings, skill_prefix)

        _check_control_flow_actions(action, warnings, skill_prefix)


def _check_service_template(
    action: dict[str, Any], warnings: BestPracticeCheckResult, skill_prefix: str | None
) -> None:
    """Flag template-based service dispatch in an action.

    Three shapes:
    - ``service_template:`` — legacy explicit way to template a service name.
      Flag any value.
    - ``service:`` containing ``{{`` — modern syntax with a template.
    - ``action:`` containing ``{{`` — HA's 2024+ rename of ``service:``.

    The native alternative is a ``choose`` (or ``if/then/else``) action that
    dispatches to different hardcoded service names based on state.
    """
    if "service_template" in action:
        _emit(
            warnings,
            "Action uses `service_template` (legacy templated service dispatch) — "
            "use a `choose` (or `if/then/else`) action that dispatches to different "
            "hardcoded `action:` names based on state. Native dispatch validates "
            "each service name at config load.",
            skill_prefix,
            "automation-patterns.md#ifthen-vs-choose",
        )
        return
    for key in _SERVICE_KEYS:
        value = action.get(key)
        if isinstance(value, str) and _RE_ANY_TEMPLATE.search(value):
            _emit(
                warnings,
                f"Action `{key}:` field contains a template — use a `choose` "
                "(or `if/then/else`) action with hardcoded service names instead. "
                "Templates here bypass HA's service-name validation and fail "
                "silently if the resolved string is invalid.",
                skill_prefix,
                "automation-patterns.md#ifthen-vs-choose",
            )
            return


def _check_target_dict(
    target: dict[str, Any], warnings: BestPracticeCheckResult, skill_prefix: str | None
) -> None:
    """Flag any Jinja in target.entity_id/device_id/area_id/floor_id/label_id.

    Templates in target fields bypass HA's entity-existence validation at
    config load and fail silently if they resolve to a non-existent entity.
    `{{ this.entity_id }}`-style self-references are especially pointless —
    the calling automation/script already knows its own entity_id, so
    hardcoding the literal is both simpler and safer.
    """
    for field in _TARGET_FIELDS:
        value = target.get(field)
        for item in _as_list(value):
            if not isinstance(item, str) or not _RE_ANY_TEMPLATE.search(item):
                continue
            if _RE_THIS_REFERENCE.search(item):
                _emit(
                    warnings,
                    f"Action `target.{field}` uses a `this.*` self-reference template — "
                    f"hardcode the literal value instead. The self-reference is always "
                    f"resolvable at write time, so the template adds runtime cost without "
                    f"any flexibility.",
                    skill_prefix,
                    "template-guidelines.md#when-to-avoid-templates",
                )
            else:
                _emit(
                    warnings,
                    f"Action `target.{field}` uses a template — prefer a hardcoded literal, "
                    f"or use a `choose` action with native conditions to dispatch to different "
                    f"hardcoded targets. Templates in target fields fail silently if they "
                    f"resolve to a non-existent entity.",
                    skill_prefix,
                    "template-guidelines.md#when-to-avoid-templates",
                )


# ---------------------------------------------------------------------------
# Variables-block ordering
# ---------------------------------------------------------------------------


def _iter_strings(value: Any) -> Iterator[str]:
    """Yield every string a variables-block value renders.

    Mirrors ``homeassistant.helpers.template.render_complex``, which recurses
    into lists and mappings and renders mapping *keys* as well as values.
    """
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _iter_strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _iter_strings(key)
            yield from _iter_strings(item)


def _span_end(text: str, start: int, closer: str, *, literal_aware: bool) -> int | None:
    """Return the index of ``closer``, stepping over string literals.

    ``None`` when the span never closes, or when a literal inside it never
    closes — neither is parseable, and guessing where the span ended is what
    produces both a hidden read and a phantom one.
    """
    index = start
    while index < len(text):
        if literal_aware and text[index] in "'\"":
            literal = _RE_STRING_LITERAL.match(text, index)
            if literal is None:
                return None
            index = literal.end()
            continue
        if text.startswith(closer, index):
            return index
        index += 1
    return None


def _stmt_tag(body: str) -> str:
    """Return the tag name opening a ``{% %}`` span, or ``""``."""
    match = _RE_STMT_TAG.match(body)
    return match.group(1) if match else ""


def _iter_spans(text: str) -> Iterator[tuple[bool, str]]:
    """Yield ``(is_statement, body)`` per Jinja span, string literals blanked.

    A ``{#...#}`` comment and everything between ``{% raw %}`` and
    ``{% endraw %}`` yield nothing: neither is code, so a sibling's name in
    there is not a read.
    """
    position = 0
    in_raw = False
    while (opening := _RE_SPAN_OPEN.search(text, position)) is not None:
        opener = opening.group(0)
        closer = _SPAN_CLOSERS[opener]
        # A comment body is not code, so a quote inside it binds nothing.
        end = _span_end(text, opening.end(), closer, literal_aware=opener != "{#")
        if end is None:
            return
        body = _RE_STRING_LITERAL.sub(" ", text[opening.end() : end])
        position = end + len(closer)
        if opener == "{#":
            continue
        is_statement = opener == "{%"
        tag = _stmt_tag(body) if is_statement else ""
        if in_raw:
            in_raw = tag != "endraw"
            continue
        if tag == "raw":
            in_raw = True
            continue
        yield is_statement, body


def _read_names(body: str, bound: set[str]) -> set[str]:
    """Return the identifiers ``body`` reads, minus the ones already bound.

    The strip passes remove the positions where an identifier token is not a
    variable read at all: a filter name, a test name, a call's keyword
    argument, and an attribute on the value to its left.
    """
    body = _RE_FILTER_NAME.sub(" ", body)
    body = _RE_TEST_NAME.sub(" ", body)
    body = _RE_KEYWORD_ARG.sub(" ", body)
    body = _RE_ATTRIBUTE.sub(" ", body)
    return {
        name
        for name in _RE_JINJA_IDENT.findall(body)
        if name not in bound and name not in _JINJA_KEYWORDS
    }


def _split_targets(targets: str) -> tuple[set[str], set[str]]:
    """Split an assignment target list into ``(bound names, names read)``.

    A plain identifier binds. Anything else assigns *into* an existing object
    (``{% set ns.x = 1 %}``, ``{% set d['k'] = 1 %}``), which reads the base
    name rather than binding it.
    """
    bound: set[str] = set()
    reads: set[str] = set()
    for chunk in targets.strip().strip("()[]").split(","):
        chunk = chunk.strip()
        if _RE_PLAIN_NAME.fullmatch(chunk):
            bound.add(chunk)
        else:
            reads |= _read_names(chunk, set())
    return bound, reads


def _split_parameters(parameters: str) -> tuple[set[str], set[str]]:
    """Split a macro parameter list into ``(bound names, names read)``.

    A default value is evaluated where the macro is defined, so it reads from
    the enclosing scope while the parameter itself binds inside the macro.
    """
    bound: set[str] = set()
    reads: set[str] = set()
    for chunk in parameters.split(","):
        name, _, default = chunk.partition("=")
        name = name.strip()
        if _RE_PLAIN_NAME.fullmatch(name):
            bound.add(name)
        if default:
            reads |= _read_names(default, set())
    return bound, reads


def _referenced_names(value: Any) -> set[str]:
    """Return the identifier tokens a variables-block value reads."""
    names: set[str] = set()
    for text in _iter_strings(value):
        # Bindings the template makes itself, innermost scope last. A
        # `{% set %}` shadows a sibling only from its own position onward, so
        # the spans are walked in order; collecting targets up front would
        # swallow a genuine read happening earlier in the string, or in the
        # binding's own right-hand side. A scope-opening tag pushes a frame
        # that its closing tag discards again, so a binding made inside a loop
        # stops shadowing after `{% endfor %}` — which is where the read it
        # was hiding actually happens.
        scopes: list[set[str]] = [set()]
        for is_statement, body in _iter_spans(text):
            visible = set().union(*scopes)
            tag = _stmt_tag(body) if is_statement else ""

            if tag in _SCOPE_CLOSERS:
                if len(scopes) > 1:
                    scopes.pop()
                continue

            if tag == "for":
                match = _RE_FOR.match(body)
                if match is None:
                    scopes.append(set())
                    continue
                targets, iterable, condition = match.groups()
                names |= _read_names(iterable, visible)
                bound, reads = _split_targets(targets)
                names |= reads - visible
                scopes.append(bound)
                if condition:
                    names |= _read_names(condition, visible | bound)
                continue

            if tag == "macro":
                match = _RE_MACRO.match(body)
                if match is None:
                    scopes.append(set())
                    continue
                scopes[-1].add(match.group(1))
                bound, reads = _split_parameters(match.group(2))
                names |= reads - visible
                scopes.append(bound)
                continue

            if tag in ("set", "with"):
                assignment = _RE_ASSIGN.match(body)
                if assignment is not None:
                    bound, reads = _split_targets(assignment.group(1))
                    names |= _read_names(assignment.group(2), visible)
                    names |= reads - visible
                else:
                    block_set = _RE_BLOCK_SET.match(body)
                    bound = {block_set.group(1)} if block_set else set()
                if tag == "with":
                    scopes.append(bound)
                else:
                    scopes[-1] |= bound
                continue

            if tag == "call":
                names |= _read_names(body, visible)
                scopes.append(set())
                continue

            names |= _read_names(body, visible)
    return names


def _check_variables_order(
    variables: Any,
    warnings: BestPracticeCheckResult,
    skill_prefix: str | None,
    block_key: str,
) -> None:
    """Flag a variables key whose template reads a *later* key in the same block.

    HA renders a variables block one entry at a time, feeding each result into
    the context for the next (``ScriptVariables.async_simple_render`` for an
    action-level step, ``async_render`` for the top-level and
    ``trigger_variables`` blocks). A name declared further down is therefore
    undefined at render time, and what that costs depends on how the template
    uses it. HA's undefined is ``LoggingUndefined(jinja2.Undefined)``
    (``helpers/template/__init__.py``), which overrides only ``__str__``,
    ``__iter__`` and ``__bool__`` to log; every other operation goes through
    ``_fail_with_undefined_error``:

    * silent — a bare ``{{ later }}``, ``{% if later %}``, ``~`` concatenation,
      ``==``/``!=``, ``{% for x in later %}`` and ``| length`` yield an empty,
      falsy, empty-iterating value, so the automation loads and runs on through
      the wrong branch with only a warning in the log and on the trace
    * raising — attribute access, item access, arithmetic, an ordering
      comparison and casts such as ``| int`` raise ``UndefinedError``, log at
      ERROR and abort the step

    Only *later* siblings count. Names coming from anywhere else — an earlier
    sibling, an earlier action's ``response_variable``, the trigger context —
    are legitimate and never flagged.

    Known false positives, deliberately left in rather than paid for with a
    full Jinja parser:

    * a later sibling whose name also exists in an outer scope, where the
      reference is legal and resolves outward (the warning text names this one)
    * a sibling whose name collides with an HA template global (``states``,
      ``trigger``, ...) or with a filter or test named in a position the strip
      passes in :func:`_read_names` do not reach. Jinja's own keywords are
      excluded (:data:`_JINJA_KEYWORDS`); the HA globals are not, because that
      set drifts with HA and a stale copy would suppress real warnings

    Known gap: when an automation declares both ``trigger_variables`` and
    ``variables``, HA merges them into a single sequentially-rendered mapping
    (``components/automation/__init__.py``, ``_create_automation_entities``),
    so a ``trigger_variables`` key reading a ``variables`` key is a forward
    read this per-block scan does not see. The reverse direction is safe: the
    merge puts ``trigger_variables`` first, so a ``variables`` key reading one
    of them is a backward read. ``trigger_variables`` is additionally rendered
    on its own when the triggers are attached (``_async_attach_triggers``,
    ``limited=True``), so a forward read inside that block is real even when
    a later ``variables`` key overwrites the same name.
    """
    if not isinstance(variables, dict) or len(variables) < 2:
        return

    names = [key for key in variables if isinstance(key, str)]
    # The last key has no later sibling, so it can never carry a forward read.
    for index, key in enumerate(names[:-1]):
        later = set(names[index + 1 :])
        forward = sorted(later & _referenced_names(variables[key]))
        if not forward:
            continue
        reads = ", ".join(f"`{name}`" for name in forward)
        _emit(
            warnings,
            f"`{block_key}` key `{key}` reads {reads}, declared later in the same "
            "block — HA renders a variables block one key at a time, so a name "
            "further down is not defined yet. How that fails depends on the use: "
            "attribute or item access, arithmetic, an ordering comparison and "
            "casts like `| int` raise `UndefinedError` and abort the step, while "
            "a bare `{{ }}`, `{% if %}`, `~`, `==`, iteration and `| length` "
            "silently yield an empty, falsy value and let the automation run on "
            "through the wrong branch. Move the keys it reads above it, or split "
            "them into consecutive `variables:` steps. Not a problem when the "
            "name is also defined in an outer scope — the reference then "
            "resolves outward.",
            skill_prefix,
            "automation-patterns.md#variables",
        )


# ---------------------------------------------------------------------------
# Trigger checks
# ---------------------------------------------------------------------------


def _check_triggers(
    triggers: Any, warnings: BestPracticeCheckResult, skill_prefix: str | None
) -> None:
    """Check triggers for device_id and template anti-patterns.

    Per-trigger detection is split into focused helpers (renamed-key check,
    deprecated-behavior check, template-trigger check, numeric_state-trigger
    check) purely to keep this function's complexity low. They run in the
    same order as before.
    """
    for trigger in _as_list(triggers):
        if not isinstance(trigger, dict):
            continue

        platform = trigger.get("platform", trigger.get("trigger", ""))

        _check_renamed_trigger_key(platform, warnings, skill_prefix)
        _check_deprecated_trigger_behavior(trigger, warnings, skill_prefix)

        # Device trigger → prefer entity_id-based triggers
        if platform == "device":
            _emit(
                warnings,
                "Trigger uses `device` platform with `device_id` — prefer a "
                "purpose-specific trigger (`<domain>.<name>` with a `target:` "
                "of entities/areas/floors/labels, HA 2026.7+) or a `state`/"
                "`event` trigger with `entity_id` when possible "
                "(device_id breaks on re-add).",
                skill_prefix,
                "device-control.md#entity-id-vs-device-id",
            )

        # Template trigger — specific shapes first, generic fallback after.
        if platform == "template":
            _check_template_trigger(trigger, warnings, skill_prefix)

        # numeric_state trigger: value_template can also contain duration math
        # (e.g. transforming last_changed into a seconds value for the threshold).
        if platform == "numeric_state":
            _check_numeric_state_trigger(trigger, warnings, skill_prefix)


def _check_renamed_trigger_key(
    platform: Any, warnings: BestPracticeCheckResult, skill_prefix: str | None
) -> None:
    """Flag HA 2026.7 purpose-specific trigger key renames (old keys no longer load)."""
    renamed_trigger = (
        _RENAMED_TRIGGER_KEYS.get(platform) if isinstance(platform, str) else None
    )
    if renamed_trigger:
        _emit(
            warnings,
            f"Trigger key `{platform}` was renamed to `{renamed_trigger}` "
            "in HA 2026.7 and the old key no longer loads — use "
            f"`trigger: {renamed_trigger}`.",
            skill_prefix,
            "automation-patterns.md#trigger-types",
        )


def _check_deprecated_trigger_behavior(
    trigger: dict[str, Any], warnings: BestPracticeCheckResult, skill_prefix: str | None
) -> None:
    """Flag HA 2026.7 trigger `options.behavior` value renames (any→each, last→all)."""
    options = trigger.get("options")
    if not isinstance(options, dict):
        return
    behavior = options.get("behavior")
    if isinstance(behavior, str) and behavior in _DEPRECATED_TRIGGER_BEHAVIOR:
        _emit(
            warnings,
            f"Trigger `options.behavior: {behavior}` was renamed to "
            f"`{_DEPRECATED_TRIGGER_BEHAVIOR[behavior]}` in HA 2026.7 — "
            "the old value still loads but raises a repair issue and "
            "will be removed. Valid trigger values: `each`, `first`, "
            "`all` (conditions keep `any`/`all`).",
            skill_prefix,
            "automation-patterns.md#trigger-types",
        )


def _check_template_trigger(
    trigger: dict[str, Any], warnings: BestPracticeCheckResult, skill_prefix: str | None
) -> None:
    """Check a `template` trigger's `value_template` for anti-patterns.

    Split out of :func:`_check_triggers` to keep its complexity low.
    """
    vt = trigger.get("value_template", "")
    if not isinstance(vt, str):
        return
    initial = len(warnings)
    # See `_check_template_string`: duration math also trips the numeric
    # comparison detector, but maps to `for:`, not `numeric_state`. Suppress
    # the numeric_state suggestion when duration math is present.
    duration_match = _RE_DURATION_MATH.search(vt)
    if _RE_NUMERIC_CMP.search(vt) and not duration_match:
        _emit(
            warnings,
            "Trigger uses template with float/int comparison — "
            "use native `numeric_state` trigger instead "
            "(e.g., `platform: numeric_state, entity_id: sensor.temp, above: 30`).",
            skill_prefix,
            "automation-patterns.md#trigger-types",
        )
    if _RE_IS_STATE.search(vt):
        _emit(
            warnings,
            "Trigger uses template with `is_state()` — use "
            "native `state` trigger instead "
            "(e.g., `platform: state, entity_id: light.x, to: 'on'`).",
            skill_prefix,
            "automation-patterns.md#trigger-types",
        )
    if duration_match:
        _emit(
            warnings,
            "Trigger uses template for duration/recency check "
            "(`now() - X.last_changed/last_updated`) — use the native "
            "`for:` field on a `state` trigger instead "
            "(e.g., `platform: state, entity_id: binary_sensor.motion, "
            "to: 'off', for: {minutes: 5}`). Native `for:` is event-driven "
            "and doesn't re-evaluate on every state change.",
            skill_prefix,
            "automation-patterns.md#trigger-types",
        )
    # Generic fallback for unmatched template triggers.
    if len(warnings) == initial and _RE_ANY_TEMPLATE.search(vt):
        _emit(
            warnings,
            "Trigger uses `template` platform — if this maps to a native option "
            "(`state`, `numeric_state`, `time`, `time_pattern`, `sun`, `zone`, "
            "`event`), use that instead. Native triggers are event-driven; "
            "template triggers re-evaluate on every state change.",
            skill_prefix,
            "automation-patterns.md#trigger-types",
        )


def _check_numeric_state_trigger(
    trigger: dict[str, Any], warnings: BestPracticeCheckResult, skill_prefix: str | None
) -> None:
    """Flag a `numeric_state` trigger's `value_template` used for duration math.

    Split out of :func:`_check_triggers` to keep its complexity low.
    """
    vt = trigger.get("value_template", "")
    if isinstance(vt, str) and _RE_DURATION_MATH.search(vt):
        _emit(
            warnings,
            "`numeric_state` trigger uses `value_template` for duration/recency "
            "check (`now() - X.last_changed/last_updated`) — use the native "
            "`for:` field on a `state` trigger instead "
            "(e.g., `platform: state, entity_id: binary_sensor.motion, "
            "to: 'off', for: {minutes: 5}`). Native `for:` is event-driven "
            "and doesn't re-evaluate on every state change.",
            skill_prefix,
            "automation-patterns.md#trigger-types",
        )


# ---------------------------------------------------------------------------
# Mode + motion check
# ---------------------------------------------------------------------------


def _check_mode_motion(
    config: dict[str, Any], warnings: BestPracticeCheckResult, skill_prefix: str | None
) -> None:
    """Detect mode:single (default) with motion triggers and delay/wait."""
    mode = config.get("mode", "single")
    if mode != "single":
        return

    triggers = _as_list(config.get("triggers", config.get("trigger", [])))
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

    if _has_delay_or_wait(config.get("actions", config.get("action", []))):
        _emit(
            warnings,
            "Automation uses motion trigger with delay/wait but "
            "`mode: single` (default) — consider `mode: restart` so "
            "re-triggers reset the timer.",
            skill_prefix,
            "automation-patterns.md#automation-modes",
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


def _dedupe_inplace(warnings: BestPracticeCheckResult) -> None:
    """Remove duplicate warning strings in place, preserving order.

    Mutates ``warnings`` and leaves ``warnings.referenced_files``
    untouched — dedup'ing the strings doesn't change which files were
    referenced; a duplicate emission for the same file still leaves a
    single set entry, and unique emissions for different files are
    preserved by the dedup either way.
    """
    seen: set[str] = set()
    kept: list[str] = []
    for w in warnings:
        if w not in seen:
            seen.add(w)
            kept.append(w)
    warnings.clear()
    warnings.extend(kept)
