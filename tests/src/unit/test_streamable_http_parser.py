"""Regression tests for the shared Streamable-HTTP SSE parser (e2e utilities).

Live-found failure class (PR #1741): a real 258 KB ``tools/list`` SSE body
was unparseable because (a) ``requests`` decodes charset-less
``text/event-stream`` as ISO-8859-1, mangling UTF-8 multibyte sequences into
stray bytes (0x85/NEL among them), and (b) ``str.splitlines()`` treats NEL —
plus \\v, \\f, \\x1c-\\x1e and U+2028/U+2029 — as line boundaries the SSE spec
does not have, shearing the ``data:`` line mid-JSON. Hermetic ASCII fixtures
missed both; these tests pin the exact hazards.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "e2e" / "utilities" / "streamable_http.py"
)
_spec = importlib.util.spec_from_file_location("streamable_http", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
streamable_http = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(streamable_http)

parse_mcp_response = streamable_http.parse_mcp_response
sse_event_payloads = streamable_http.sse_event_payloads

SSE = "text/event-stream"


def _sse_body(payload: str, *, line_end: str = "\n") -> str:
    return f"event: message{line_end}data: {payload}{line_end}{line_end}"


class TestSseSpecLineSplitting:
    def test_nel_inside_payload_does_not_shear_the_data_line(self):
        # U+0085 (NEL) raw inside a JSON string value (legal: JSON only forbids
        # raw controls < 0x20): str.splitlines() would split here; the SSE spec
        # does not. This is the live-found tools/list bug.
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": 2, "result": {"v": "a\x85b"}},
            ensure_ascii=False,
        )
        assert "\x85" in payload
        parsed = parse_mcp_response(SSE, _sse_body(payload))
        assert parsed is not None and parsed["result"]["v"] == "a\x85b"

    def test_ls_ps_inside_payload(self):
        # U+2028/U+2029 are ALSO legal raw in JSON strings and ALSO
        # splitlines() boundaries. (Raw controls < 0x20 like \v/\f/\x1c-\x1e
        # are invalid JSON, so a spec-compliant server can never emit them.)
        hazards = "\u2028\u2029"
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "result": {"v": hazards}},
            ensure_ascii=False,
        )
        parsed = parse_mcp_response(SSE, _sse_body(payload))
        assert parsed is not None and parsed["result"]["v"] == hazards

    def test_crlf_framing(self):
        payload = '{"jsonrpc":"2.0","id":1,"result":{}}'
        parsed = parse_mcp_response(SSE, _sse_body(payload, line_end="\r\n"))
        assert parsed is not None and parsed["id"] == 1


class TestUtf8ByteDecoding:
    def test_multibyte_utf8_bytes_parse_correctly(self):
        # A body whose UTF-8 multibyte sequences contain 0x85 continuation
        # bytes (e.g. U+2019 RIGHT SINGLE QUOTATION MARK = E2 80 99, and "…"
        # = E2 80 A6). Decoded as ISO-8859-1 these become NEL-class bytes;
        # passing raw bytes must yield the true characters.
        text_value = "it’s here … really"
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": 3, "result": {"v": text_value}},
            ensure_ascii=False,
        )
        body = _sse_body(payload).encode("utf-8")
        parsed = parse_mcp_response(SSE, body)
        assert parsed is not None and parsed["result"]["v"] == text_value

    def test_plain_json_bytes(self):
        parsed = parse_mcp_response(
            "application/json", b'{"jsonrpc":"2.0","id":9,"result":{}}'
        )
        assert parsed is not None and parsed["id"] == 9


class TestMultiLineDataFraming:
    def test_pretty_printed_78_tool_listing_spans_many_data_lines(self):
        big = {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {
                "tools": [
                    {"name": f"t{i}", "inputSchema": {"type": "object"}}
                    for i in range(78)
                ]
            },
        }
        pretty = json.dumps(big, indent=2)
        body = (
            "event: message\n"
            + "\n".join("data: " + line for line in pretty.split("\n"))
            + "\n\n"
        )
        parsed = parse_mcp_response(SSE, body)
        assert parsed is not None and len(parsed["result"]["tools"]) == 78

    def test_first_result_event_wins_and_keepalives_are_skipped(self):
        body = (
            ": keepalive\n\n"
            'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/x"}\n\n'
            'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"n":1}}\n\n'
            'event: message\ndata: {"jsonrpc":"2.0","id":2,"result":{"n":2}}\n\n'
        )
        parsed = parse_mcp_response(SSE, body)
        assert parsed is not None and parsed["result"]["n"] == 1

    def test_trailing_event_without_blank_line(self):
        body = 'data: {"jsonrpc":"2.0","id":1,"result":{}}'
        assert parse_mcp_response(SSE, body) is not None

    def test_garbage_returns_none(self):
        assert parse_mcp_response(SSE, "event: message\ndata: {truncated") is None
        assert parse_mcp_response(SSE, ": keepalive only\n\n") is None
