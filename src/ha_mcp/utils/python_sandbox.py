"""
Python expression validation for dashboard transformations.

Restricts expressions to a known-safe subset: dict/list operations,
basic control flow, and whitelisted methods. Not a security boundary —
callers are already authenticated MCP users with full HA access.
"""

import ast
from typing import Any, cast


class PythonSandboxError(Exception):
    """Base class for sandbox failures.

    Catch this when callers don't need to distinguish validation-time
    rejection from runtime exceptions — otherwise prefer the subclasses.
    """


class PythonSandboxValidationError(PythonSandboxError):
    """Raised when AST validation rejects the expression before execution.

    The expression contains a forbidden node, function, or method, or
    failed to parse. The user can fix the input.
    """


class PythonSandboxExecutionError(PythonSandboxError):
    """Raised when a validated expression raised at runtime.

    The expression passed AST validation but produced a Python exception
    when executed (e.g. KeyError on a missing key, TypeError on a bad
    operation). Different from a validation failure: the *shape* of the
    expression is fine, but it doesn't apply cleanly to the input data.
    """


# Cap on how much of a runtime exception's text gets surfaced. HA configs
# can carry tokens / passwords / device addresses, and Python's default
# repr happily embeds dict and list values into KeyError/TypeError text.
# 240 chars is enough to identify the failure (exception type + a short
# snippet) without pasting the input config back to the caller.
_EXECUTION_ERROR_TEXT_LIMIT = 240


def _truncate_for_error(text: str, limit: int = _EXECUTION_ERROR_TEXT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


# Whitelist of safe AST node types
SAFE_NODES = {
    # Structural
    ast.Module,
    ast.Expr,
    ast.Assign,
    ast.AugAssign,  # +=, -=, etc.
    ast.AnnAssign,  # type annotations
    ast.Pass,  # explicit no-op
    # Control flow
    ast.If,
    ast.For,
    ast.While,
    ast.Break,
    ast.Continue,
    ast.IfExp,  # ternary: x if c else y
    # Data access
    ast.Subscript,
    ast.Attribute,
    ast.Index,
    ast.Slice,  # list[1:3]
    ast.Name,
    ast.Load,
    ast.Store,
    ast.Del,
    # Literals
    ast.Constant,
    ast.List,
    ast.Dict,
    ast.Tuple,
    ast.Set,
    ast.Starred,  # *iterable in calls/literals: f(*xs), [*xs, y]
    ast.JoinedStr,  # f"…" — outer node holding parts
    ast.FormattedValue,  # the {expr} part inside an f-string
    # Operations
    ast.Delete,
    ast.BinOp,
    ast.UnaryOp,
    ast.Compare,
    ast.BoolOp,
    # Operators
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.Mod,
    ast.And,
    ast.Or,
    ast.Not,
    ast.USub,
    ast.UAdd,
    ast.Invert,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
    ast.Is,
    ast.IsNot,
    # Function calls (validated separately)
    ast.Call,
    ast.keyword,  # keyword arguments: func(key=value)
    # Comprehensions
    ast.ListComp,
    ast.DictComp,
    ast.SetComp,
    ast.GeneratorExp,  # (x for x in ...)
    ast.comprehension,
    # Lambda — useful as `key=` for sorted/min/max. ast.arguments and
    # ast.arg are the structure nodes ast.walk descends into for the
    # parameter list; they have no execution semantics on their own
    # (FunctionDef would be blocked at the SAFE_NODES check above).
    ast.Lambda,
    ast.arguments,
    ast.arg,
}


# Hints to help agents recover when a forbidden node is encountered.
# Keyed on AST class name (string, not class) so entries for
# version-specific nodes like Match (3.10+) or TryStar (3.11+) stay
# evaluable on any Python. Unmapped keys fall through to the generic
# "Forbidden node type: X" message in `_validate_node`.
_NODE_SUGGESTIONS: dict[str, str] = {
    "Try": "validate inputs with isinstance/in/.get() instead of try/except",
    "TryStar": "validate inputs with isinstance/in/.get() instead of try/except",
    "ExceptHandler": "validate inputs with isinstance/in/.get() instead of try/except",
    "With": "perform the inner logic directly; with-blocks aren't supported",
    "AsyncWith": "perform the inner logic directly; with-blocks aren't supported",
    "FunctionDef": "use a list comprehension or inline the logic",
    "AsyncFunctionDef": "use a list comprehension or inline the logic",
    "ClassDef": "use a dict literal instead of defining a class",
    "Yield": "build a list with a comprehension or for-loop append",
    "YieldFrom": "build a list with a comprehension or for-loop append",
    "Global": "assign directly to the variable; scope keywords aren't supported",
    "Nonlocal": "assign directly to the variable; scope keywords aren't supported",
    "Import": "imports aren't available; built-ins like isinstance/len/range are exposed",
    "ImportFrom": "imports aren't available; built-ins like isinstance/len/range are exposed",
    "Match": "use if/elif/else or a dict lookup instead of match/case",
    # If Match ever enters SAFE_NODES, the sub-pattern nodes shouldn't
    # silently slip through with a generic message.
    "MatchAs": "use if/elif/else or a dict lookup instead of match/case",
    "MatchValue": "use if/elif/else or a dict lookup instead of match/case",
    "MatchClass": "use if/elif/else or a dict lookup instead of match/case",
    "MatchSingleton": "use if/elif/else or a dict lookup instead of match/case",
    "MatchSequence": "use if/elif/else or a dict lookup instead of match/case",
    "MatchMapping": "use if/elif/else or a dict lookup instead of match/case",
    "MatchOr": "use if/elif/else or a dict lookup instead of match/case",
    "MatchStar": "use if/elif/else or a dict lookup instead of match/case",
}

# Whitelist of safe methods that can be called
SAFE_METHODS = {
    # List methods
    "append",
    "insert",
    "pop",
    "remove",
    "clear",
    "extend",
    "index",
    "count",
    "sort",
    "reverse",
    # Dict methods
    "update",
    "get",
    "setdefault",
    "keys",
    "values",
    "items",
    # String methods (for entity filtering)
    "startswith",
    "endswith",
    "lower",
    "upper",
    "strip",
    "split",
    "join",
    "replace",
}

# Blocked function names
BLOCKED_FUNCTIONS = {
    "eval",
    "exec",
    "compile",
    "__import__",
    "open",
    "input",
    "exit",
    "quit",
    "help",
    "dir",
    "vars",
    "globals",
    "locals",
    "getattr",
    "setattr",
    "delattr",
    "hasattr",
}


# Minimal set of builtins exposed to sandboxed expressions. All entries are
# pure (no side effects, no I/O, no imports) and commonly needed by data
# transforms — type checks, length, numeric/string coercion, simple
# collection helpers. Expanding this list is fine if another pure builtin
# is genuinely needed; adding anything that touches the filesystem, network,
# or interpreter state is not.
_SAFE_BUILTINS: dict[str, Any] = {
    "isinstance": isinstance,
    "len": len,
    "range": range,
    "enumerate": enumerate,
    "zip": zip,
    "sorted": sorted,
    "reversed": reversed,
    "min": min,
    "max": max,
    "sum": sum,
    "abs": abs,
    "any": any,
    "all": all,
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "list": list,
    "dict": dict,
    "tuple": tuple,
    "set": set,
    "round": round,
}


def validate_expression(expr: str) -> tuple[bool, str]:
    """
    Validate Python expression is safe to execute.

    Returns:
        tuple: (is_valid, error_message)
        - (True, "") if expression is safe
        - (False, error_message) if expression is unsafe

    Examples:
        >>> validate_expression("config['views'][0]['icon'] = 'lamp'")
        (True, "")

        >>> validate_expression("import os")
        (False, "Forbidden: imports not allowed")
    """
    if not expr or not expr.strip():
        return False, "Empty expression"

    # Parse expression
    try:
        tree = ast.parse(expr, mode="exec")
    except SyntaxError as e:
        return False, f"Syntax error: {e}"

    # Validate all nodes
    for node in ast.walk(tree):
        error = _validate_node(node)
        if error:
            return False, error

    return True, ""


def _validate_node(node: ast.AST) -> str | None:
    """Validate a single AST node. Returns error message or None if safe.

    Whitelist check first: any node not in ``SAFE_NODES`` is rejected with
    its class name and (when available) a recovery hint from
    ``_NODE_SUGGESTIONS``. After that, only nodes that *are* safe but need
    extra checks (Attribute → block dunder access, Call → block forbidden
    functions/methods) get further validation.
    """
    if type(node) not in SAFE_NODES:
        name = type(node).__name__
        hint = _NODE_SUGGESTIONS.get(name)
        if hint:
            return f"Forbidden node type: {name} — {hint}"
        return f"Forbidden node type: {name}"

    if isinstance(node, ast.Attribute):
        if node.attr.startswith("__") and node.attr.endswith("__"):
            return f"Forbidden: dunder attribute access ({node.attr})"

    if isinstance(node, ast.Call):
        return _validate_call_node(node)

    return None


def _validate_call_node(node: ast.Call) -> str | None:
    """Validate a function/method call node. Returns error message or None."""
    if isinstance(node.func, ast.Name):
        if node.func.id in BLOCKED_FUNCTIONS:
            return f"Forbidden function: {node.func.id}"
    elif isinstance(node.func, ast.Attribute):
        method_name = node.func.attr
        if method_name.startswith("__") and method_name.endswith("__"):
            return f"Forbidden: dunder method call ({method_name})"
        if method_name not in SAFE_METHODS:
            return f"Forbidden method: {method_name} (allowed: {', '.join(sorted(SAFE_METHODS))})"
    else:
        return f"Forbidden call target type: {type(node.func).__name__}"
    return None


def safe_execute_expression(
    expr: str,
    variables: dict[str, Any],
    result_key: str,
) -> Any:
    """
    Execute a validated Python expression in a restricted environment.

    The expression runs with ``variables`` available as locals. After
    execution, the value bound to ``result_key`` is returned. This supports
    both in-place mutation (``response.append(...)``) and reassignment
    (``response = [...]``) — in the reassignment case the returned object
    is the new one, not the original reference.

    Args:
        expr: Python expression to execute
        variables: Mapping of variable names to values exposed to the expression
        result_key: Name of the variable in ``variables`` whose post-execution
            value should be returned

    Returns:
        The value of ``result_key`` in the local namespace after execution

    Raises:
        PythonSandboxError: If expression validation fails or execution errors

    Examples:
        >>> safe_execute_expression(
        ...     "response = [m for m in response if m.get('level') == 'ERROR']",
        ...     {"response": [{"level": "INFO"}, {"level": "ERROR"}]},
        ...     "response",
        ... )
        [{'level': 'ERROR'}]
    """
    valid, error = validate_expression(expr)
    if not valid:
        raise PythonSandboxValidationError(error)

    if result_key not in variables:
        raise PythonSandboxValidationError(
            f"result_key {result_key!r} not found in variables",
        )

    safe_globals: dict[str, Any] = {
        "__builtins__": _SAFE_BUILTINS,
        "__name__": "__main__",
        "__doc__": None,
    }
    safe_locals: dict[str, Any] = dict(variables)

    try:
        exec(expr, safe_globals, safe_locals)
    except (MemoryError, RecursionError):
        # Resource exhaustion — let the host decide. Reframing
        # "ran out of memory" as "your transform was bad" would
        # mislead the agent into rewriting an expression that
        # was structurally fine.
        #
        # FastMCP's tool dispatch (server.py call_tool) catches
        # `except Exception` and wraps in
        # ``ToolError(f"Error calling tool {name!r}: {e}") from e`` —
        # so the original exception's class name and text reach the
        # agent, with the raw exception preserved as ``__cause__``.
        # That's an acceptable surfacing (not opaque INTERNAL_ERROR).
        raise
    except Exception as e:
        # Truncate so embedded reprs of input data (config dicts, tokens,
        # etc.) don't reach the caller verbatim.
        detail = _truncate_for_error(f"{type(e).__name__}: {e}")
        raise PythonSandboxExecutionError(detail) from e

    return safe_locals[result_key]


def safe_execute(expr: str, config: dict[str, Any]) -> dict[str, Any]:
    """
    Execute validated Python expression against a ``config`` dict.

    Thin wrapper around :func:`safe_execute_expression` that exposes the
    input as the variable ``config`` (used by dashboard/automation/script
    transforms).

    Args:
        expr: Python expression to execute
        config: Configuration dict (may be modified in-place)

    Returns:
        The value bound to ``config`` after execution — typically the same
        dict mutated in place, but also supports expressions that reassign
        ``config`` to a new object.

    Raises:
        PythonSandboxError: If expression validation fails or execution errors

    Examples:
        >>> config = {'views': [{'cards': [{'icon': 'old'}]}]}
        >>> safe_execute("config['views'][0]['cards'][0]['icon'] = 'new'", config)
        {'views': [{'cards': [{'icon': 'new'}]}]}
    """
    # safe_execute_expression returns Any (generic over result_key); at this
    # call site the result is always the dict bound to `config`, so narrow
    # for mypy and existing callers that depend on the dict interface.
    return cast(
        dict[str, Any],
        safe_execute_expression(expr, {"config": config}, "config"),
    )


def format_sandbox_error(
    error: PythonSandboxError,
    expr: str,
    variable_name: str = "config",
) -> tuple[str, list[str]]:
    """Build a (message, suggestions) pair appropriate for the error subclass.

    ``PythonSandboxValidationError`` means the expression's shape was
    rejected before execution — suggestions point at syntax/allowed-ops.
    ``PythonSandboxExecutionError`` means the expression was accepted
    but raised at runtime — suggestions point at keys/types/values.
    Plain ``PythonSandboxError`` (no subclass) falls back to the
    validation form.

    ``variable_name`` is the name of the mutable target the expression
    operates on. The default ``"config"`` matches the dashboard /
    automation / script callers; addon helpers pass ``"response"`` and
    a one-liner about that name is prepended to the suggestions so
    agents know which variable to mutate.

    Used by ``ha_config_set_*`` and addon helpers so each caller emits
    the same shape of MCP error without duplicating the boilerplate.
    """
    preview = expr[:100] + ("..." if len(expr) > 100 else "")
    if isinstance(error, PythonSandboxExecutionError):
        message = f"Expression raised at runtime: {error}"
        suggestions = [
            "Verify referenced keys/indices exist in the input",
            "Check that types match (e.g. dict vs list operations)",
            "Use .get(key, default) to handle missing keys",
            f"Expression: {preview}",
        ]
    else:
        message = f"Expression validation failed: {error}"
        suggestions = [
            "Check expression syntax",
            "Ensure only allowed operations are used",
            "See tool description for allowed operations",
            f"Expression: {preview}",
        ]
        # Agents commonly emit ``\"`` (intending a JSON-style quote escape)
        # inside transforms. Outside Python string literals this is a
        # line-continuation token followed by an unexpected character.
        # Pinpoint the actual mistake so the agent fixes it on the first
        # retry instead of guessing at "syntax".
        if "line continuation character" in str(error):
            suggestions.insert(
                0,
                'Remove the leading backslash before quotes — write "foo" '
                'not \\"foo\\" in the Python source.',
            )
    if variable_name != "config":
        suggestions = [
            f"Operate on the `{variable_name}` variable (in-place or reassign)",
            *suggestions,
        ]
    return message, suggestions


def get_security_documentation() -> str:
    """
    Get formatted documentation of security restrictions.

    Used in tool descriptions to inform agents of allowed operations.
    """
    return """
PYTHON TRANSFORM SECURITY:

✅ ALLOWED:
- Dictionary/list access: config['views'][0]['cards'][1]
- Slicing: config['views'][0]['cards'][1:3]
- Assignment: config['key'] = 'value'
- Deletion: del config['key'] or config.pop('key')
- List methods: append, insert, pop, remove, clear, extend
- Dict methods: update, get, setdefault, keys, values, items
- Loops: for, while, if/else, pass, break, continue
- Comprehensions: [x for x in ...], {k: v for ...}, (x for x in ...)
- Ternary: x if condition else y
- Iterable unpacking (* in calls/literals): f(*xs), [*xs, y]
- Dict unpacking (**) in calls and dict literals: {**d, 'k': v}
- Keyword arguments: func(key=value)
- Lambdas (e.g. for `key=`): sorted(items, key=lambda x: x['score'])
- String methods: startswith, endswith, lower, upper, strip, split, join, replace
- Safe builtins: isinstance, len, range, enumerate, zip, sorted, reversed,
  min, max, sum, abs, any, all, round, str, int, float, bool, list, dict,
  tuple, set

❌ FORBIDDEN:
- Imports: import, from, __import__
- File operations: open, read, write
- Dunder access: __class__, __bases__, __subclasses__
- Dangerous builtins: eval, exec, compile, getattr, setattr, delattr, hasattr
- Function definitions: def, class
- Exception handling: try/except (validate with isinstance/in/.get() instead)

🎯 PATTERNS:
- Filter cards: cards = [c for c in cards if keep(c)]
- Skip in a loop: prefer `continue` over an empty `pass` branch (clearer)
- Conditionally include: build a new list and `.append(x)` only the
  cards you want, instead of iterating the original and using if/pass
  branches to drop entries
- Modify in place when possible (single pass, fewer surprises) over
  reconstructing the entire list
""".strip()
