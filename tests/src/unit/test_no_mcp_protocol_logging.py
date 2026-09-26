"""Guard: ha-mcp code must not send MCP protocol log messages through ``ctx``.

MCP 2026-07-28 deprecates the logging capability (SEP-2577).
``Context.debug/info/warning/error/log`` calls that pass FastMCP's client
log-level filter (all of them by default) reach the SDK's ``send_log_message``,
which issues ``MCPDeprecationWarning`` on each call, even when the request did
not opt in and the message is dropped (#2464). Status lines belong in the
module logger instead.

The per-tool tests in ``test_context_injection.py`` pin the known call paths;
this scan catches a new call anywhere in the non-vendored source. It matches
a bare receiver name (``ctx``, and ``fastmcp_context`` as middleware binds it)
and flags any ``send_log_message`` call whatever its receiver.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "src" / "ha_mcp"
VENDOR_ROOT = SRC_ROOT / "_vendor"
LOG_METHODS = frozenset({"debug", "info", "warning", "error", "log"})
CONTEXT_NAMES = frozenset({"ctx", "fastmcp_context"})


def _is_protocol_log_call(func: ast.expr) -> bool:
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr == "send_log_message":
        return True
    return (
        func.attr in LOG_METHODS
        and isinstance(func.value, ast.Name)
        and func.value.id in CONTEXT_NAMES
    )


def _protocol_log_calls(source: str, filename: str) -> list[str]:
    return [
        f"{filename}:{node.lineno}: {ast.unparse(node.func)}(...)"
        for node in ast.walk(ast.parse(source, filename=filename))
        if isinstance(node, ast.Call) and _is_protocol_log_call(node.func)
    ]


def test_scanner_detects_a_protocol_log_call() -> None:
    source = (
        "async def f(ctx, context, session):\n"
        "    await ctx.info('x')\n"
        "    await ctx.log('x', level='info')\n"
        "    fastmcp_context = context.fastmcp_context\n"
        "    await fastmcp_context.warning('x')\n"
        "    await session.send_log_message(level='info', data='x')\n"
        "    logger.info('y')\n"
    )
    assert _protocol_log_calls(source, "sample.py") == [
        "sample.py:2: ctx.info(...)",
        "sample.py:3: ctx.log(...)",
        "sample.py:5: fastmcp_context.warning(...)",
        "sample.py:6: session.send_log_message(...)",
    ]


def test_source_sends_no_protocol_log_messages() -> None:
    files = [p for p in SRC_ROOT.rglob("*.py") if VENDOR_ROOT not in p.parents]
    assert len(files) > 50, f"scan found only {len(files)} files under {SRC_ROOT}"

    hits = [
        hit
        for path in files
        for hit in _protocol_log_calls(
            path.read_text(encoding="utf-8"), str(path.relative_to(SRC_ROOT))
        )
    ]
    assert hits == [], "use the module logger instead:\n" + "\n".join(hits)
