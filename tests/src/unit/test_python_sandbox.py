"""Tests for Python expression sandbox."""

import pytest

from ha_mcp.utils.python_sandbox import (
    PythonSandboxError,
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
        """Test that blocked expressions raise PythonSandboxError."""
        config = {}
        expr = "import os"
        with pytest.raises(PythonSandboxError, match="validation failed"):
            safe_execute(expr, config)

    def test_execution_error_raises(self):
        """Test that execution errors are caught."""
        config = {}
        expr = "config['nonexistent']['key'] = 'value'"
        with pytest.raises(PythonSandboxError, match="Execution error"):
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
        """If result_key is not in variables, raise PythonSandboxError up front."""
        with pytest.raises(PythonSandboxError, match="result_key"):
            safe_execute_expression(
                "response = 1", {"other": 1}, "response"
            )

    def test_validation_failure_raises(self):
        """Invalid expressions raise with 'validation failed' prefix."""
        with pytest.raises(PythonSandboxError, match="validation failed"):
            safe_execute_expression("import os", {"response": None}, "response")

    def test_execution_error_raises(self):
        """Runtime errors in the expression raise with 'Execution error' prefix."""
        with pytest.raises(PythonSandboxError, match="Execution error"):
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
        with pytest.raises(PythonSandboxError, match="validation failed"):
            safe_execute_expression(
                "open('/etc/passwd')", {"response": None}, "response"
            )

    def test_builtins_do_not_include_getattr(self):
        """getattr remains blocked at AST validation."""
        with pytest.raises(PythonSandboxError, match="validation failed"):
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
