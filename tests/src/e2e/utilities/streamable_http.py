"""Shared Streamable-HTTP (MCP) response parsing for the e2e suite.

One implementation for every place that probes an MCP endpoint over plain
HTTP (the embedded-server smoke test, the HAOS embedded probe, and the
conftest embedded-backend readiness gate), so the SSE framing rules live in
exactly one spot.
"""

from __future__ import annotations

import json
import re
from typing import Any

# SSE-legal line boundaries ONLY (\r\n, \r, \n). str.splitlines() must never be
# used on SSE bodies: it also splits on NEL (\x85), \v, \f, \x1c-\x1e and
# U+2028/U+2029 — characters that legitimately appear INSIDE a UTF-8 JSON
# payload (or arise from a mis-decode) and would shear the data line mid-JSON.
_SSE_LINE_SPLIT = re.compile(r"\r\n|\r|\n")


def sse_event_payloads(text: str) -> list[str]:
    """Return each SSE event's payload from a Streamable-HTTP body.

    Per the SSE framing an event is a run of lines ended by a blank line, and
    its ``data:`` fields are concatenated with ``\\n`` (one optional leading
    space stripped per field). A single ``data:`` line per event is NOT a safe
    assumption: a large ``tools/list`` (78 tool schemas) is split across
    several ``data:`` lines within one ``event: message`` block, so parsing
    each line on its own hits mid-JSON and fails. Accumulate per event.
    """
    payloads: list[str] = []
    data_lines: list[str] = []

    def _flush() -> None:
        if data_lines:
            payloads.append("\n".join(data_lines))
            data_lines.clear()

    for line in _SSE_LINE_SPLIT.split(text):
        if line.startswith("data:"):
            value = line[len("data:") :]
            if value.startswith(" "):  # strip exactly ONE leading space (SSE spec)
                value = value[1:]
            data_lines.append(value)
        elif line == "":
            _flush()  # blank line terminates the event
        # event:/id:/retry:/comment lines carry no payload -- ignore them.
    _flush()  # a trailing event with no terminating blank line
    return payloads


def parse_mcp_response(content_type: str, body: bytes | str) -> dict[str, Any] | None:
    """Parse a Streamable-HTTP MCP response body to its JSON-RPC dict.

    Accepts both negotiated shapes: a plain JSON body, or an SSE stream whose
    first ``result``/``error`` event carries the JSON-RPC response. Returns
    None when no such payload is present.

    Pass the RAW BYTES where possible: SSE bodies are UTF-8 by spec, but the
    server sends ``Content-Type: text/event-stream`` without a charset, so
    ``requests``' ``.text`` decodes them as ISO-8859-1 — mangling every
    multibyte character into stray bytes (0x85 among them) that then break
    line framing and JSON parsing. Live-found against a real 258 KB
    ``tools/list`` response.
    """
    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
    if "text/event-stream" in content_type:
        for payload in sse_event_payloads(text):
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and ("result" in obj or "error" in obj):
                return obj
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None
