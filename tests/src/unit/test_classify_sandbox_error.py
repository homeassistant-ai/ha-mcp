"""A failed custom-tool run names the kind of failure and how to recover.

``ha_manage_custom_tool`` maps a sandbox failure to one of three buckets —
``SANDBOX_LIMIT_EXCEEDED`` / ``SANDBOX_SYNTAX_UNSUPPORTED`` /
``SANDBOX_RUNTIME_ERROR`` — with category-tailored suggestions, so an agent
does not read a memory cap or a forbidden import as a syntax problem. Each
case runs code in the real sandbox.
"""

import json
from typing import Any

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.errors import ErrorCode

from ._code_mode import run_custom_tool


async def _failure(
    client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    code: str,
    **settings: Any,
) -> tuple[str, str, str]:
    """The error code, message and joined suggestions of a failed run."""
    with pytest.raises(ToolError) as exc_info:
        await run_custom_tool(client, monkeypatch, code, **settings)
    error = json.loads(str(exc_info.value))["error"]
    return error["code"], error["message"], " ".join(error["suggestions"])


class TestLimitExceededBucket:
    async def test_memory_error(
        self, core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The default limit is 10 MB; this allocates about 12 MB.
        code, message, suggestions = await _failure(
            core_client, monkeypatch, "x = 'x' * (12 * 1024 * 1024)\nlen(x)"
        )
        assert code == ErrorCode.SANDBOX_LIMIT_EXCEEDED
        assert "memory" in message.lower()
        assert "memory" in suggestions.lower()
        assert "code_mode_max_memory" in suggestions.lower()

    async def test_recursion_error(
        self, core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        code, message, suggestions = await _failure(
            core_client, monkeypatch, "def f(n):\n    return f(n + 1)\n\nf(0)"
        )
        assert code == ErrorCode.SANDBOX_LIMIT_EXCEEDED
        assert "recursion" in message.lower()
        assert "recursion" in suggestions.lower()
        assert "code_mode_max_recursion" in suggestions.lower()

    async def test_timeout_error(
        self, core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        code, message, suggestions = await _failure(
            core_client,
            monkeypatch,
            "i = 0\nwhile True:\n    i += 1\ni",
            code_mode_max_duration=1.0,
        )
        assert code == ErrorCode.SANDBOX_LIMIT_EXCEEDED
        assert "time" in message.lower() or "wall" in message.lower()
        assert "code_mode_max_duration" in suggestions.lower()


class TestSyntaxUnsupportedBucket:
    async def test_module_not_found_error(
        self, core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        code, message, suggestions = await _failure(
            core_client, monkeypatch, "import time\ntime.time()"
        )
        assert code == ErrorCode.SANDBOX_SYNTAX_UNSUPPORTED
        assert "import" in message.lower()
        assert "import" in suggestions.lower()
        assert "api_get" in suggestions or "helper" in suggestions.lower()

    async def test_unsupported_feature(
        self, core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        code, _message, _suggestions = await _failure(
            core_client, monkeypatch, "with open('x') as f:\n    pass"
        )
        assert code == ErrorCode.SANDBOX_SYNTAX_UNSUPPORTED

    async def test_syntax_error(
        self, core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        code, _message, _suggestions = await _failure(
            core_client, monkeypatch, "def foo(:\n  pass"
        )
        assert code == ErrorCode.SANDBOX_SYNTAX_UNSUPPORTED


class TestRuntimeErrorBucket:
    async def test_type_error(
        self, core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        code, message, _suggestions = await _failure(
            core_client, monkeypatch, "next([1, 2])"
        )
        assert code == ErrorCode.SANDBOX_RUNTIME_ERROR
        # Monty wraps the failure in MontyRuntimeError; the message keeps the
        # inner exception the agent has to fix.
        assert "TypeError" in message

    async def test_lookup_error_for_missing_builtin(
        self, core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Referencing a name that is not among the injected helpers (e.g.
        ``bytearray``) falls into the default bucket — the right call, because
        the user-actionable advice is "use the injected helpers", which the
        default suggestions already give.
        """
        code, _message, _suggestions = await _failure(
            core_client, monkeypatch, "bytearray(1)"
        )
        assert code == ErrorCode.SANDBOX_RUNTIME_ERROR
