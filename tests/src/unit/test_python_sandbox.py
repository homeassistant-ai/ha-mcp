"""Tests for Python expression sandbox."""

import pytest

from ha_mcp.utils.python_sandbox import (
    PythonSandboxError,
    safe_execute,
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
