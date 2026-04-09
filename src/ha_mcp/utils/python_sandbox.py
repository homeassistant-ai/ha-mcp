"""
Python expression validation for dashboard transformations.

Restricts expressions to a known-safe subset: dict/list operations,
basic control flow, and whitelisted methods. Not a security boundary —
callers are already authenticated MCP users with full HA access.
"""

import ast
from typing import Any


class PythonSandboxError(Exception):
    """Raised when expression validation fails."""



# Whitelist of safe AST node types
SAFE_NODES = {
    # Structural
    ast.Module,
    ast.Expr,
    ast.Assign,
    ast.AugAssign,  # +=, -=, etc.
    ast.AnnAssign,  # type annotations
    # Control flow
    ast.If,
    ast.For,
    ast.While,
    ast.Break,
    ast.Continue,
    # Data access
    ast.Subscript,
    ast.Attribute,
    ast.Index,
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
    # Comprehensions
    ast.ListComp,
    ast.DictComp,
    ast.SetComp,
    ast.comprehension,
    # Lambda (for comprehensions)
    ast.Lambda,
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
    """Validate a single AST node. Returns error message or None if safe."""
    if type(node) not in SAFE_NODES:
        return f"Forbidden node type: {type(node).__name__}"

    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return "Forbidden: imports not allowed"

    if isinstance(node, ast.Attribute):
        if node.attr.startswith("__") and node.attr.endswith("__"):
            return f"Forbidden: dunder attribute access ({node.attr})"

    if isinstance(node, ast.Call):
        return _validate_call_node(node)

    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return "Forbidden: function/class definitions not allowed"

    if isinstance(node, (ast.With, ast.AsyncWith)):
        return "Forbidden: with statements not allowed"

    if isinstance(node, (ast.Try, ast.ExceptHandler)):
        return "Forbidden: try/except not allowed"

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


def safe_execute(expr: str, config: dict[str, Any]) -> dict[str, Any]:
    """
    Execute validated Python expression in restricted environment.

    Args:
        expr: Python expression to execute
        config: Dashboard configuration dict (will be modified in-place)

    Returns:
        Modified config dict

    Raises:
        PythonSandboxError: If expression validation fails or execution errors

    Examples:
        >>> config = {'views': [{'cards': [{'icon': 'old'}]}]}
        >>> safe_execute("config['views'][0]['cards'][0]['icon'] = 'new'", config)
        {'views': [{'cards': [{'icon': 'new'}]}]}
    """
    # Validate expression
    valid, error = validate_expression(expr)
    if not valid:
        raise PythonSandboxError(f"Expression validation failed: {error}")

    # Execute in restricted environment
    # No builtins to prevent access to dangerous functions
    safe_globals: dict[str, Any] = {
        "__builtins__": {},
        "__name__": "__main__",
        "__doc__": None,
    }

    safe_locals: dict[str, Any] = {
        "config": config,
    }

    try:
        exec(expr, safe_globals, safe_locals)
    except Exception as e:
        raise PythonSandboxError(f"Execution error: {type(e).__name__}: {e}") from e

    return config


def get_security_documentation() -> str:
    """
    Get formatted documentation of security restrictions.

    Used in tool descriptions to inform agents of allowed operations.
    """
    return """
PYTHON TRANSFORM SECURITY:

✅ ALLOWED:
- Dictionary/list access: config['views'][0]['cards'][1]
- Assignment: config['key'] = 'value'
- Deletion: del config['key'] or config.pop('key')
- List methods: append, insert, pop, remove, clear, extend
- Dict methods: update, get, setdefault, keys, values, items
- Loops: for, while, if/else
- Comprehensions: [x for x in ...]
- String methods: startswith, endswith, lower, upper, split, join

❌ FORBIDDEN:
- Imports: import, from, __import__
- File operations: open, read, write
- Dunder access: __class__, __bases__, __subclasses__
- Dangerous builtins: eval, exec, compile
- Function definitions: def, class
- Exception handling: try/except (use validation instead)
""".strip()
