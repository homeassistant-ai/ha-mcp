"""Tests for Python expression sandbox."""

import pytest

from ha_mcp.utils.python_sandbox import (
    _EXECUTION_ERROR_TEXT_LIMIT,
    PythonSandboxError,
    PythonSandboxExecutionError,
    PythonSandboxValidationError,
    format_sandbox_error,
    safe_execute,
    safe_execute_expression,
    validate_expression,
)


class TestValidateExpression:
    """Test expression validation."""

    def test_simple_assignment(self):
        """Test simple dictionary assignment."""
        expr = "config['views'][0]['icon'] = 'mdi:lamp'"
        valid, error = validate_expression(expr)
        assert valid is True
        assert error == ""

    def test_list_append(self):
        """Test list append method."""
        expr = "config['views'][0]['cards'].append({'type': 'button'})"
        valid, error = validate_expression(expr)
        assert valid is True

    def test_deletion(self):
        """Test deletion operation."""
        expr = "del config['views'][0]['cards'][2]"
        valid, error = validate_expression(expr)
        assert valid is True

    def test_loop_with_conditional(self):
        """Test for loop with conditional."""
        expr = """
for view in config['views']:
    for card in view.get('cards', []):
        if 'light' in card.get('entity', ''):
            card['icon'] = 'mdi:lightbulb'
"""
        valid, error = validate_expression(expr)
        assert valid is True

    def test_list_comprehension(self):
        """Test list comprehension."""
        expr = "config['entities'] = [e for e in config.get('entities', []) if 'light' in e]"
        valid, error = validate_expression(expr)
        assert valid is True


class TestUnaryOperators:
    """Regression tests for issue #1115 — negative numbers in expressions."""

    def test_negative_number_literal(self):
        valid, error = validate_expression("x = -1")
        assert valid is True, error

    def test_unary_plus_literal(self):
        valid, error = validate_expression("x = +1")
        assert valid is True, error

    def test_bitwise_invert(self):
        valid, error = validate_expression("x = ~1")
        assert valid is True, error

    def test_negative_in_dict_value(self):
        expr = 'config["views"][0]["min"] = -10'
        valid, error = validate_expression(expr)
        assert valid is True, error

    def test_dashboard_view_with_negative_axis_range(self):
        """Reproduces issue #1115: appending a card with a negative gauge min."""
        config = {"views": [{"cards": []}]}
        expr = (
            'config["views"][0]["cards"].append('
            '{"type": "gauge", "entity": "sensor.power", "min": -5000, "max": 5000})'
        )
        result = safe_execute(expr, config)
        assert result["views"][0]["cards"][0]["min"] == -5000

    def test_negation_in_arithmetic(self):
        config = {"value": 5}
        expr = 'config["value"] = -config["value"]'
        result = safe_execute(expr, config)
        assert result["value"] == -5


class TestSafeNodes1159:
    """Regression tests for issue #1159 — safe AST nodes that were over-restricted.

    The sandbox is documented as a sanity-check whitelist (not a security
    boundary), so missing pure-AST nodes cause spurious rejections of
    legitimate Python idioms. These tests pin the gap shut.
    """

    def test_pass_statement(self):
        """`pass` is a no-op; rejecting it forced agents to invent dummy expressions."""
        expr = """
for view in config['views']:
    if view.get('path') == 'skip-me':
        pass
"""
        valid, error = validate_expression(expr)
        assert valid is True, error

    def test_generator_expression_executes(self):
        """`sum(x for x in ...)` validates AND runs."""
        config = {"views": [{"cards": [{"type": "tile"}, {"type": "btn"}, {"type": "tile"}]}]}
        expr = "config['count'] = sum(1 for c in config['views'][0]['cards'] if c.get('type') == 'tile')"
        result = safe_execute(expr, config)
        assert result["count"] == 2

    def test_ternary_expression_executes(self):
        """`x if c else y` validates AND runs."""
        config = {"state": "on"}
        expr = "config['icon'] = 'mdi:on' if config.get('state') == 'on' else 'mdi:off'"
        result = safe_execute(expr, config)
        assert result["icon"] == "mdi:on"

    def test_keyword_argument_in_call_executes(self):
        """`sorted(..., key=len, reverse=True)` validates AND runs (sorted/len are safe builtins)."""
        config = {"items": ["aaa", "b", "cc"]}
        expr = "config['items'] = sorted(config['items'], key=len, reverse=True)"
        result = safe_execute(expr, config)
        assert result["items"] == ["aaa", "cc", "b"]

    def test_starred_unpacking_in_list_executes(self):
        """`[*existing, new]` validates AND runs."""
        config = {"cards": [{"type": "a"}, {"type": "b"}]}
        expr = "config['cards'] = [*config['cards'], {'type': 'c'}]"
        result = safe_execute(expr, config)
        assert [c["type"] for c in result["cards"]] == ["a", "b", "c"]

    def test_dict_double_star_unpacking_executes(self):
        """`{**existing, 'k': v}` validates AND runs (Dict node, not Starred)."""
        config = {"view": {"icon": "old", "title": "Home"}}
        expr = "config['view'] = {**config['view'], 'icon': 'mdi:home'}"
        result = safe_execute(expr, config)
        assert result["view"] == {"icon": "mdi:home", "title": "Home"}

    def test_slice_expression_executes(self):
        """`list[:N]` validates AND runs."""
        config = {"cards": list(range(10))}
        expr = "config['cards'] = config['cards'][:5]"
        result = safe_execute(expr, config)
        assert result["cards"] == [0, 1, 2, 3, 4]

    def test_slice_with_step_executes(self):
        """`list[::2]` — slice with step (uses ast.Slice with step field)."""
        config = {"cards": list(range(10))}
        expr = "config['cards'] = config['cards'][::2]"
        result = safe_execute(expr, config)
        assert result["cards"] == [0, 2, 4, 6, 8]

    def test_lambda_as_kwarg_executes(self):
        """`sorted(..., key=lambda x: ...)` — the canonical lambda use case.

        Previously broken: ast.Lambda was whitelisted but the ast.arguments
        / ast.arg structure nodes inside it were not, so any lambda failed
        validation despite the comment "Lambda (for comprehensions)" claiming
        otherwise.
        """
        config = {"items": [{"n": 3}, {"n": 1}, {"n": 2}]}
        expr = "config['items'] = sorted(config['items'], key=lambda x: x['n'])"
        result = safe_execute(expr, config)
        assert [i["n"] for i in result["items"]] == [1, 2, 3]

    def test_compositional_pattern_executes(self):
        """Realistic agent code: ternary inside a comprehension, kwarg call,
        starred unpacking — all in one transform."""
        config = {
            "cards": [
                {"name": "a", "score": 3},
                {"name": "b", "score": 1},
                {"name": "c", "score": 2},
            ],
            "extras": [{"name": "z", "score": 99}],
        }
        # Sort by score (kwarg call), pick top 2 (slice), tag each (ternary
        # inside comprehension), then unpack with extras (starred).
        expr = """
top = sorted(config['cards'], key=lambda c: c['score'], reverse=True)[:2]
config['cards'] = [
    {**c, 'tier': 'gold' if c['score'] >= 3 else 'silver'}
    for c in top
]
config['cards'] = [*config['cards'], *config['extras']]
"""
        result = safe_execute(expr, config)
        names = [c["name"] for c in result["cards"]]
        assert names == ["a", "c", "z"]
        tiers = [c.get("tier") for c in result["cards"]]
        assert tiers == ["gold", "silver", None]

    def test_reporter_pattern_executes(self):
        """End-to-end: reconstruct cards list with conditional skip via `pass`.

        This is the exact shape of the transform from issue #1159 — pass branch
        inside an if inside a loop. Now executes and produces the expected result.
        """
        config = {
            "views": [
                {
                    "path": "home",
                    "cards": [
                        {"type": "tile", "name": "keep-1"},
                        {"type": "tile", "name": "drop"},
                        {"type": "tile", "name": "keep-2"},
                    ],
                }
            ]
        }
        expr = """
new_cards = []
for card in config['views'][0]['cards']:
    if card.get('name') == 'drop':
        pass
    else:
        new_cards.append(card)
config['views'][0]['cards'] = new_cards
"""
        result = safe_execute(expr, config)
        names = [c["name"] for c in result["views"][0]["cards"]]
        assert names == ["keep-1", "keep-2"]


class TestErrorSuggestions1159:
    """Issue #1159 — node-rejection errors should hint at the right alternative."""

    def test_try_block_includes_hint(self):
        """try/except is rejected with the exact mapped hint."""
        expr = "try:\n    config['x'] = 1\nexcept Exception:\n    config['x'] = 0"
        valid, error = validate_expression(expr)
        assert valid is False
        assert error == (
            "Forbidden node type: Try — "
            "validate inputs with isinstance/in/.get() instead of try/except"
        )

    def test_function_def_includes_hint(self):
        """Function definitions are rejected with the exact mapped hint."""
        expr = "def helper():\n    return 1"
        valid, error = validate_expression(expr)
        assert valid is False
        assert error == (
            "Forbidden node type: FunctionDef — "
            "use a list comprehension or inline the logic"
        )

    def test_match_statement_includes_hint(self):
        """match/case (3.10+) is rejected with the mapped hint."""
        expr = "match config:\n    case _:\n        config['x'] = 1"
        valid, error = validate_expression(expr)
        assert valid is False
        assert error == (
            "Forbidden node type: Match — "
            "use if/elif/else or a dict lookup instead of match/case"
        )

    def test_unmapped_node_falls_back_to_generic(self):
        """Nodes without a mapped suggestion produce a bare 'Forbidden node type: X'.

        ``raise`` is a deliberately unmapped node — it isn't in SAFE_NODES
        and intentionally has no recovery hint (raising in transforms is
        nonsense; the right fix is to not raise at all). Picking a node
        that's structurally unfit for any hint keeps the test stable as
        new hints get added.
        """
        expr = "raise ValueError('nope')"
        valid, error = validate_expression(expr)
        assert valid is False
        assert error == "Forbidden node type: Raise"


class TestBlockedOperations:
    """Test that dangerous operations are blocked."""

    def test_block_import(self):
        """Test that imports are blocked."""
        expr = "import os"
        valid, error = validate_expression(expr)
        assert valid is False
        assert "import" in error.lower()

    def test_block_from_import(self):
        """Test that from imports are blocked."""
        expr = "from os import system"
        valid, error = validate_expression(expr)
        assert valid is False
        assert "import" in error.lower()

    def test_block_dunder_import(self):
        """Test that __import__ is blocked."""
        expr = "__import__('os')"
        valid, error = validate_expression(expr)
        assert valid is False
        assert "import" in error.lower() or "forbidden" in error.lower()

    def test_block_open(self):
        """Test that open() is blocked."""
        expr = "open('/etc/passwd')"
        valid, error = validate_expression(expr)
        assert valid is False
        assert "open" in error.lower()

    def test_block_eval(self):
        """Test that eval is blocked."""
        expr = "eval('print(1)')"
        valid, error = validate_expression(expr)
        assert valid is False
        assert "eval" in error.lower()

    def test_block_exec(self):
        """Test that exec is blocked."""
        expr = "exec('import os')"
        valid, error = validate_expression(expr)
        assert valid is False
        assert "exec" in error.lower()

    def test_block_dunder_class(self):
        """Test that __class__ access is blocked."""
        expr = "config.__class__"
        valid, error = validate_expression(expr)
        assert valid is False
        assert "dunder" in error.lower() or "__class__" in error

    def test_block_dunder_bases(self):
        """Test that __bases__ access is blocked."""
        expr = "().__class__.__bases__[0]"
        valid, error = validate_expression(expr)
        assert valid is False
        assert "dunder" in error.lower()

    def test_block_function_def(self):
        """Test that function definitions are blocked."""
        expr = "def evil(): pass"
        valid, error = validate_expression(expr)
        assert valid is False
        assert "function" in error.lower()

    def test_block_class_def(self):
        """Test that class definitions are blocked."""
        expr = "class Evil: pass"
        valid, error = validate_expression(expr)
        assert valid is False
        assert "class" in error.lower()

    def test_block_forbidden_method(self):
        """Test that non-whitelisted methods are blocked."""
        expr = "config.some_random_method()"
        valid, error = validate_expression(expr)
        assert valid is False
        assert "method" in error.lower()

    def test_block_subscript_call(self):
        """Test that calls on subscript results are blocked."""
        expr = "config['fn']()"
        valid, error = validate_expression(expr)
        assert valid is False
        assert "Subscript" in error

    def test_block_chained_call(self):
        """Test that calls on method results are blocked."""
        expr = "config.get('fn')()"
        valid, error = validate_expression(expr)
        assert valid is False
        assert "Call" in error


class TestSafeExecute:
    """Test safe execution of expressions."""

    def test_simple_update(self):
        """Test simple dictionary update."""
        config = {"views": [{"icon": "old"}]}
        expr = "config['views'][0]['icon'] = 'new'"
        result = safe_execute(expr, config)
        assert result["views"][0]["icon"] == "new"

    def test_list_append(self):
        """Test list append."""
        config = {"views": [{"cards": []}]}
        expr = "config['views'][0]['cards'].append({'type': 'button'})"
        result = safe_execute(expr, config)
        assert len(result["views"][0]["cards"]) == 1
        assert result["views"][0]["cards"][0]["type"] == "button"

    def test_deletion(self):
        """Test deletion."""
        config = {"views": [{"cards": [1, 2, 3]}]}
        expr = "del config['views'][0]['cards'][1]"
        result = safe_execute(expr, config)
        assert result["views"][0]["cards"] == [1, 3]

    def test_pattern_update(self):
        """Test pattern-based update with loop."""
        config = {
            "views": [
                {
                    "cards": [
                        {"entity": "light.living_room", "icon": "old"},
                        {"entity": "light.bedroom", "icon": "old"},
                        {"entity": "climate.thermostat", "icon": "old"},
                    ]
                }
            ]
        }
        expr = """
for card in config['views'][0]['cards']:
    if 'light' in card.get('entity', ''):
        card['icon'] = 'mdi:lightbulb'
"""
        result = safe_execute(expr, config)
        assert result["views"][0]["cards"][0]["icon"] == "mdi:lightbulb"
        assert result["views"][0]["cards"][1]["icon"] == "mdi:lightbulb"
        assert result["views"][0]["cards"][2]["icon"] == "old"  # Not a light

    def test_blocked_expression_raises(self):
        """Test that blocked expressions raise PythonSandboxValidationError."""
        config = {}
        expr = "import os"
        with pytest.raises(PythonSandboxValidationError):
            safe_execute(expr, config)

    def test_execution_error_raises(self):
        """Test that execution errors are caught."""
        config = {}
        expr = "config['nonexistent']['key'] = 'value'"
        with pytest.raises(PythonSandboxExecutionError):
            safe_execute(expr, config)


class TestSafeExecuteExpression:
    """Tests for the generalized safe_execute_expression."""

    def test_custom_variable_name(self):
        """Supports arbitrary variable names, not just 'config'."""
        expr = "response = [x for x in response if x > 1]"
        result = safe_execute_expression(expr, {"response": [1, 2, 3]}, "response")
        assert result == [2, 3]

    def test_reassignment_returns_new_object(self):
        """Reassignment inside the expression is reflected in the return value.

        The old safe_execute semantics returned the original reference, which
        silently dropped reassigned values. safe_execute_expression returns
        the post-execution binding, so `response = [...]` works.
        """
        expr = "response = {'filtered': True}"
        result = safe_execute_expression(expr, {"response": {}}, "response")
        assert result == {"filtered": True}

    def test_in_place_mutation(self):
        """In-place mutations on mutable values are returned as expected."""
        original = [1, 2, 3]
        expr = "response.append(4)"
        result = safe_execute_expression(expr, {"response": original}, "response")
        assert result == [1, 2, 3, 4]
        assert original == [1, 2, 3, 4]  # same reference, mutated

    def test_missing_result_key_raises(self):
        """If result_key is not in variables, raise PythonSandboxValidationError up front."""
        with pytest.raises(PythonSandboxValidationError, match="result_key"):
            safe_execute_expression(
                "response = 1", {"other": 1}, "response"
            )

    def test_validation_failure_raises(self):
        """Invalid expressions raise PythonSandboxValidationError."""
        with pytest.raises(PythonSandboxValidationError):
            safe_execute_expression("import os", {"response": None}, "response")

    def test_execution_error_raises(self):
        """Runtime errors raise PythonSandboxExecutionError."""
        with pytest.raises(PythonSandboxExecutionError):
            safe_execute_expression(
                "response['missing']['key'] = 1",
                {"response": {}},
                "response",
            )

    def test_mixed_shape_list_with_isinstance(self):
        """Transforms handle heterogeneous list[dict | str] using isinstance.

        The WebSocket message list is intentionally heterogeneous (parsed JSON
        dicts interleaved with raw ANSI-stripped strings). Agents need
        isinstance/str to reason about the shape — both are in the minimal
        safe-builtins set.
        """
        messages = [
            {"level": "INFO", "text": "Starting"},
            "raw text line",
            {"level": "ERROR", "text": "Boom"},
            "another raw line",
        ]
        expr = (
            "response = [m for m in response "
            "if isinstance(m, dict) and m.get('level') == 'ERROR']"
        )
        result = safe_execute_expression(
            expr, {"response": messages}, "response"
        )
        assert result == [{"level": "ERROR", "text": "Boom"}]

    def test_str_coercion_available(self):
        """str() is in the safe builtins for text-content matching."""
        messages = [{"level": "ERROR"}, "plain string", 42]
        expr = "response = [m for m in response if 'ERROR' in str(m)]"
        result = safe_execute_expression(
            expr, {"response": messages}, "response"
        )
        assert result == [{"level": "ERROR"}]

    def test_builtins_do_not_include_open(self):
        """Dangerous builtins like open remain blocked at AST validation."""
        with pytest.raises(PythonSandboxValidationError):
            safe_execute_expression(
                "open('/etc/passwd')", {"response": None}, "response"
            )

    def test_builtins_do_not_include_getattr(self):
        """getattr remains blocked at AST validation."""
        with pytest.raises(PythonSandboxValidationError):
            safe_execute_expression(
                "getattr(response, '__class__')",
                {"response": []},
                "response",
            )

    def test_safe_execute_wrapper_still_works(self):
        """safe_execute should remain backward-compatible with existing callers."""
        config = {"views": [{"icon": "old"}]}
        result = safe_execute("config['views'][0]['icon'] = 'new'", config)
        assert result["views"][0]["icon"] == "new"


class TestSandboxErrorSubclasses:
    """Issue #1159 — distinguish validation-time vs runtime sandbox failures.

    Both subclasses inherit ``PythonSandboxError`` so any pre-existing
    ``except PythonSandboxError`` block still catches them; but new code
    can branch on the subclass to give the user the right suggestion.
    """

    def test_subclasses_inherit_base(self):
        """Backward compat: callers catching the base class still work."""
        assert issubclass(PythonSandboxValidationError, PythonSandboxError)
        assert issubclass(PythonSandboxExecutionError, PythonSandboxError)

    def test_validation_error_is_distinct_from_execution_error(self):
        """A validation failure should not match an execution-error catcher."""
        with pytest.raises(PythonSandboxValidationError):
            safe_execute("import os", {})
        # And the execution-error class shouldn't catch a validation failure.
        try:
            safe_execute("import os", {})
        except PythonSandboxExecutionError:
            pytest.fail("validation error matched PythonSandboxExecutionError")
        except PythonSandboxValidationError:
            pass

    def test_execution_error_is_distinct_from_validation_error(self):
        """A runtime failure should not match a validation-error catcher."""
        with pytest.raises(PythonSandboxExecutionError):
            safe_execute("config['nope']['x'] = 1", {})
        try:
            safe_execute("config['nope']['x'] = 1", {})
        except PythonSandboxValidationError:
            pytest.fail("execution error matched PythonSandboxValidationError")
        except PythonSandboxExecutionError:
            pass

    def test_execution_error_truncates_long_exception_text(self):
        """Runtime exception text is capped so embedded config/data isn't pasted whole.

        HA dashboards/automations may carry tokens or device IDs that get
        reflected back in KeyError messages; without truncation those would
        reach the caller verbatim. Asserts on the constant + cap rather than
        the literal sentinel so the test survives a sentinel change.
        """
        long_key = "x" * 1000
        # KeyError on a missing key embeds the key (repr'd) in its str().
        expr = f"config[{long_key!r}]"
        with pytest.raises(PythonSandboxExecutionError) as exc_info:
            safe_execute(expr, {})
        text = str(exc_info.value)
        assert len(text) <= _EXECUTION_ERROR_TEXT_LIMIT, (
            f"runtime error text not truncated: {len(text)} chars"
        )
        # Without truncation the message would include the full 1000-char key
        # plus the KeyError/repr framing — we want strict evidence that
        # nothing close to that survived.
        assert len(text) < len(long_key)

    def test_memory_error_propagates(self):
        """Resource-exhaustion errors must not be reframed as user-input failures.

        MemoryError / RecursionError from exec() aren't transform-syntax
        issues the agent can fix by trying again — they're infrastructure
        signals the host needs to handle. Wrapping them in
        PythonSandboxExecutionError with "verify keys/types" suggestions
        would mislead.
        """
        from unittest.mock import patch

        for infra_exc in (MemoryError("oom"), RecursionError("too deep")):
            with (
                patch(
                    "ha_mcp.utils.python_sandbox.exec",
                    side_effect=infra_exc,
                ),
                pytest.raises(type(infra_exc)),
            ):
                safe_execute_expression(
                    "response = 1", {"response": 0}, "response"
                )


class TestFormatSandboxError:
    """Issue #1159 — caller-facing helper that picks message + suggestions
    based on the exception subclass."""

    def test_validation_error_suggestions_point_at_syntax(self):
        err = PythonSandboxValidationError("Forbidden node type: Try")
        message, suggestions = format_sandbox_error(err, "try: pass\nexcept: pass")
        assert message.startswith("Expression validation failed:")
        assert any("syntax" in s.lower() for s in suggestions)
        assert any("allowed operations" in s.lower() for s in suggestions)

    def test_execution_error_suggestions_point_at_data(self):
        err = PythonSandboxExecutionError("KeyError: 'foo'")
        message, suggestions = format_sandbox_error(err, "config['foo']['bar'] = 1")
        assert message.startswith("Expression raised at runtime:")
        # Should NOT advise the user to fix syntax — the syntax was fine.
        assert not any("syntax" in s.lower() for s in suggestions)
        assert any(
            "key" in s.lower() or ".get" in s.lower() or "type" in s.lower()
            for s in suggestions
        )

    def test_expression_preview_truncates_long_input(self):
        long_expr = "config['x'] = " + ("'a' + " * 50) + "'end'"
        err = PythonSandboxExecutionError("boom")
        _, suggestions = format_sandbox_error(err, long_expr)
        preview = next(s for s in suggestions if s.startswith("Expression:"))
        assert preview.endswith("...")
        # Body of the preview (after "Expression: ") is exactly 100 chars + "..."
        assert len(preview) <= len("Expression: ") + 103

    def test_plain_base_error_falls_back_to_validation_form(self):
        """A bare PythonSandboxError (no subclass) should not look like a runtime failure."""
        err = PythonSandboxError("legacy")
        message, suggestions = format_sandbox_error(err, "x = 1")
        assert message.startswith("Expression validation failed:")
        assert any("syntax" in s.lower() for s in suggestions)

    def test_default_variable_name_omits_target_hint(self):
        """The default `config` callers don't get a redundant variable-name hint."""
        err = PythonSandboxValidationError("Forbidden node type: Try")
        _, suggestions = format_sandbox_error(err, "try: pass\nexcept: pass")
        assert not any(
            "Operate on the" in s and "variable" in s for s in suggestions
        )

    def test_non_default_variable_name_prepends_hint(self):
        """A non-`config` caller (e.g. addons with `response`) gets a leading
        'Operate on the `<name>` variable' suggestion so the agent knows what
        to mutate."""
        err = PythonSandboxExecutionError("KeyError: 'foo'")
        _, suggestions = format_sandbox_error(
            err, "response['foo']", variable_name="response"
        )
        assert suggestions[0] == (
            "Operate on the `response` variable (in-place or reassign)"
        )
