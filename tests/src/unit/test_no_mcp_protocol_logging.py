"""Guard: ha-mcp code must not send MCP protocol log messages through ``ctx``.

MCP 2026-07-28 deprecates the logging capability (SEP-2577).
``Context.debug/info/warning/error/log`` calls that pass FastMCP's client
log-level filter (all of them by default) reach the SDK's ``send_log_message``,
which issues ``MCPDeprecationWarning`` on each call, even when the request did
not opt in and the message is dropped (#2464). Status lines belong in the
module logger instead.

The per-tool tests in ``test_context_injection.py`` pin the known call paths;
this scan catches a new call anywhere in the non-vendored source. It matches
the receiver by name, relying on the ``ctx`` naming every Context parameter
uses today.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "src" / "ha_mcp"
VENDOR_ROOT = SRC_ROOT / "_vendor"
LOG_METHODS = frozenset({"debug", "info", "warning", "error", "log"})


def _protocol_log_calls(source: str, filename: str) -> list[str]:
    hits = []
    for node in ast.walk(ast.parse(source, filename=filename)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in LOG_METHODS
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "ctx"
        ):
            hits.append(f"{filename}:{node.lineno}: ctx.{node.func.attr}(...)")
    return hits


def test_scanner_detects_a_protocol_log_call() -> None:
    source = "async def f(ctx):\n    await ctx.info('x')\n    logger.info('y')\n"
    assert _protocol_log_calls(source, "sample.py") == [
        "sample.py:2: ctx.info(...)"
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
