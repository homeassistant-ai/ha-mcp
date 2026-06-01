"""Unit tests for ``_classify_sandbox_error`` in ``ha_mcp.tools.tools_code``.

The classifier maps Monty / sandbox runtime exceptions to one of three
buckets — ``SANDBOX_LIMIT_EXCEEDED`` / ``SANDBOX_SYNTAX_UNSUPPORTED`` /
``SANDBOX_RUNTIME_ERROR`` — with category-tailored suggestions. Some
mappings (notably ``RecursionError``) aren't directly exercisable from
sandbox-supplied code because Monty doesn't allow user-defined recursive
functions; this suite covers them at the helper level so a regression
in the type-name match doesn't slip through CI.
"""

from ha_mcp.errors import ErrorCode
from ha_mcp.tools.tools_code import _classify_sandbox_error


class TestLimitExceededBucket:
    def test_memory_error(self):
        code, message, suggestions = _classify_sandbox_error(
            MemoryError("10485779 bytes > 10485760")
        )
        assert code == ErrorCode.SANDBOX_LIMIT_EXCEEDED
        assert "memory" in message.lower()
        joined = " ".join(suggestions).lower()
        assert "memory" in joined
        assert "code_mode_max_memory" in joined.lower()

    def test_recursion_error(self):
        code, message, suggestions = _classify_sandbox_error(
            RecursionError("maximum recursion depth exceeded")
        )
        assert code == ErrorCode.SANDBOX_LIMIT_EXCEEDED
        assert "recursion" in message.lower()
        joined = " ".join(suggestions).lower()
        assert "recursion" in joined
        assert "code_mode_max_recursion" in joined.lower()

    def test_timeout_error(self):
        code, message, suggestions = _classify_sandbox_error(
            TimeoutError("operation timed out after 30s")
        )
        assert code == ErrorCode.SANDBOX_LIMIT_EXCEEDED
        assert "time" in message.lower() or "wall" in message.lower()
        joined = " ".join(suggestions).lower()
        assert "code_mode_max_duration" in joined.lower()


class TestSyntaxUnsupportedBucket:
    def test_module_not_found_error(self):
        code, message, suggestions = _classify_sandbox_error(
            ModuleNotFoundError("No module named 'time'")
        )
        assert code == ErrorCode.SANDBOX_SYNTAX_UNSUPPORTED
        assert "import" in message.lower()
        joined = " ".join(suggestions).lower()
        assert "import" in joined
        assert "api_get" in joined or "helper" in joined

    def test_not_implemented_error(self):
        code, message, suggestions = _classify_sandbox_error(
            NotImplementedError(
                "Monty does not yet support context managers (with statements)"
            )
        )
        assert code == ErrorCode.SANDBOX_SYNTAX_UNSUPPORTED

    def test_syntax_error(self):
        code, _message, _suggestions = _classify_sandbox_error(
            SyntaxError("invalid syntax")
        )
        assert code == ErrorCode.SANDBOX_SYNTAX_UNSUPPORTED


class TestRuntimeErrorBucket:
    def test_type_error(self):
        code, _message, suggestions = _classify_sandbox_error(
            TypeError("'list' object is not an iterator")
        )
        assert code == ErrorCode.SANDBOX_RUNTIME_ERROR
        # Default-bucket suggestions name the actual exception type.
        joined = " ".join(suggestions)
        assert "TypeError" in joined

    def test_attribute_error(self):
        code, _message, _suggestions = _classify_sandbox_error(
            AttributeError("'tuple' object has no attribute '__class__'")
        )
        assert code == ErrorCode.SANDBOX_RUNTIME_ERROR

    def test_value_error(self):
        code, _message, _suggestions = _classify_sandbox_error(
            ValueError("something went wrong")
        )
        assert code == ErrorCode.SANDBOX_RUNTIME_ERROR

    def test_lookup_error_for_missing_builtin(self):
        """``LookupError: Unable to find 'X' in external functions dict``
        is what Monty raises when sandbox code references a name that's
        not in the injected helpers (e.g. ``bytearray``). Classifier
        falls into the default bucket — that's the right call because
        the user-actionable advice is "use the injected helpers" which
        is already in the default suggestions.
        """
        code, _message, _suggestions = _classify_sandbox_error(
            LookupError("Unable to find 'bytearray' in external functions dict")
        )
        assert code == ErrorCode.SANDBOX_RUNTIME_ERROR
