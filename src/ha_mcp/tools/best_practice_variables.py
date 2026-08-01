"""Ordering scan for a `variables:` block.

HA renders a variables block one key at a time, so a key reading a name
declared further down renders against a name that does not exist yet. This
module holds that check and the Jinja span walk it needs; see
:func:`_check_variables_order` for the semantics and the known limits.
"""

import re
from collections.abc import Iterator
from typing import Any

from .best_practice_result import BestPracticeCheckResult, _emit

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
_RE_FOR = re.compile(
    r"[-+]?\s*for\s+(.*?)\s+in\s+(.*?)(?:\s+if\s+(.*?))?\s*[-+]?$", re.DOTALL
)
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
        "and",
        "as",
        "autoescape",
        "block",
        "break",
        "call",
        "context",
        "continue",
        "do",
        "elif",
        "else",
        "endautoescape",
        "endblock",
        "endcall",
        "endfilter",
        "endfor",
        "endif",
        "endmacro",
        "endraw",
        "endset",
        "endtrans",
        "endwith",
        "extends",
        "false",
        "filter",
        "for",
        "from",
        "if",
        "ignore",
        "import",
        "in",
        "include",
        "is",
        "loop",
        "macro",
        "missing",
        "none",
        "not",
        "or",
        "pluralize",
        "raw",
        "recursive",
        "required",
        "scoped",
        "set",
        "trans",
        "true",
        "with",
        "without",
        "False",
        "None",
        "True",
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
        target = chunk.strip()
        if _RE_PLAIN_NAME.fullmatch(target):
            bound.add(target)
        else:
            reads |= _read_names(target, set())
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


def _scan_for(body: str, visible: set[str], scopes: list[set[str]]) -> set[str]:
    """Read a `{% for %}` tag and push the scope its targets bind in."""
    match = _RE_FOR.match(body)
    if match is None:
        scopes.append(set())
        return set()
    targets, iterable, condition = match.groups()
    # The iterable is evaluated where the loop stands, so it reads from the
    # enclosing scope; the filter clause runs per item, with the targets bound.
    names = _read_names(iterable, visible)
    bound, reads = _split_targets(targets)
    names |= reads - visible
    scopes.append(bound)
    if condition:
        names |= _read_names(condition, visible | bound)
    return names


def _scan_macro(body: str, visible: set[str], scopes: list[set[str]]) -> set[str]:
    """Read a `{% macro %}` tag, binding its name outside and its params in."""
    match = _RE_MACRO.match(body)
    if match is None:
        scopes.append(set())
        return set()
    scopes[-1].add(match.group(1))
    bound, reads = _split_parameters(match.group(2))
    scopes.append(bound)
    return reads - visible


def _scan_binding(
    tag: str, body: str, visible: set[str], scopes: list[set[str]]
) -> set[str]:
    """Read a `{% set %}` / `{% with %}` tag and apply its bindings.

    A `{% set %}` binds in the current scope from here on; a `{% with %}`
    opens a scope of its own that `{% endwith %}` discards.
    """
    names: set[str] = set()
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
    return names


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
            elif tag == "for":
                names |= _scan_for(body, visible, scopes)
            elif tag == "macro":
                names |= _scan_macro(body, visible, scopes)
            elif tag in ("set", "with"):
                names |= _scan_binding(tag, body, visible, scopes)
            else:
                if tag == "call":
                    scopes.append(set())
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
    ``trigger_variables`` blocks), so a name declared further down is undefined
    at render time. Which uses then raise ``UndefinedError`` and which stay
    quiet is documented for users in ``automation-patterns.md#variables``; this
    scan only has to find the forward read.

    Only *later* siblings count. Names coming from anywhere else — an earlier
    sibling, an earlier action's ``response_variable``, the trigger context —
    are legitimate and never flagged.

    Known false positives, deliberately left in rather than paid for with a
    full Jinja parser:

    * a later sibling whose name also exists in an outer scope, where the
      reference is legal and resolves outward (the reference file names this
      one)
    * a sibling whose name collides with an HA template global (``states``,
      ``trigger``, ...) or with a filter or test named in a position the strip
      passes in :func:`_read_names` do not reach. Jinja's own keywords are
      excluded (:data:`_JINJA_KEYWORDS`); the HA globals are not, because that
      set drifts with HA and a stale copy would suppress real warnings

    Known gap: when an automation declares both ``trigger_variables`` and
    ``variables``, HA merges them into a single sequentially-rendered mapping
    (``components/automation/__init__.py``, ``_create_automation_entities``),
    so a ``trigger_variables`` key reading a ``variables`` key is a forward
    read this per-block scan does not see. The reverse direction is safe for
    distinct names: the merge puts ``trigger_variables`` first, so a
    ``variables`` key reading one of them is a backward read. A name present in
    *both* is the exception — ``dict.update`` keeps the ``trigger_variables``
    position and takes the ``variables`` template, so that entry renders early.
    ``trigger_variables`` is additionally rendered on its own when the triggers
    are attached (``_async_attach_triggers``, ``limited=True``), so a forward
    read inside that block is real even when a later ``variables`` key
    overwrites the same name.
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
            f"`{block_key}` key `{key}` reads {reads}, declared later in the "
            "same block — HA renders a variables block one key at a time, so a "
            "name further down is not defined yet. Move the keys it reads "
            "above it.",
            skill_prefix,
            "automation-patterns.md#variables",
        )
