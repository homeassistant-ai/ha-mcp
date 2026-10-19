"""The in-process server's uvicorn listener must not write access lines.

``log_config=None`` leaves ``uvicorn.access`` without a handler of its own, so
its records propagate to Home Assistant's log, which ``ha_get_logs`` serves to
MCP clients. Every access line carries the request path, and on this listener
that path is the secret path.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SOURCE = (
    Path(__file__).resolve().parents[3]
    / "custom_components"
    / "ha_mcp_tools"
    / "embedded_server.py"
)


def test_embedded_uvicorn_config_turns_off_the_access_log() -> None:
    calls = [
        node
        for node in ast.walk(ast.parse(_SOURCE.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "uvicorn.Config"
    ]
    assert len(calls) == 1
    kwargs = {kw.arg: kw.value for kw in calls[0].keywords}
    assert "access_log" in kwargs
    assert ast.literal_eval(kwargs["access_log"]) is False
