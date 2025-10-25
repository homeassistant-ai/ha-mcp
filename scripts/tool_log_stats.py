"""Utilities for analyzing Home Assistant MCP tool call logs.

This script parses log lines emitted when ``HOMEASSISTANT_TOOL_LOG_DIR`` is set
and provides basic statistics to highlight verbose tools.

Usage examples::

    # Summaries for every tool using character counts (default)
    python scripts/tool_log_stats.py summary path/to/tool_calls.ndjson.zst

    # Use token counts (requires ``tiktoken``) with a specific encoding
    python scripts/tool_log_stats.py summary path/to/tool_calls.ndjson.zst --tokens --encoding cl100k_base

    # Largest response across all tools
    python scripts/tool_log_stats.py largest path/to/tool_calls.ndjson.zst

    # Largest response for a single tool
    python scripts/tool_log_stats.py largest path/to/tool_calls.ndjson.zst --tool ha_call_service
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import zstandard


@dataclass
class ToolLogEntry:
    """Representation of a parsed tool call log entry."""

    tool: str
    status: str
    request: Any
    response: Any | None
    request_characters: int
    response_characters: int | None

    def request_text(self) -> str:
        """Return request payload as JSON string."""

        return json.dumps(self.request, ensure_ascii=False, sort_keys=True)

    def response_text(self) -> str:
        """Return response payload as JSON string."""

        if self.response is None:
            return ""
        return json.dumps(self.response, ensure_ascii=False, sort_keys=True)


def _iter_lines(log_path: Path) -> Iterable[str]:
    """Yield decoded lines from plain text or zstd-compressed NDJSON files."""

    if log_path.suffix == ".zst":
        decompressor = zstandard.ZstdDecompressor()
        with log_path.open("rb") as raw:
            with decompressor.stream_reader(raw) as reader:
                text_stream = io.TextIOWrapper(reader, encoding="utf-8")
                yield from text_stream
        return

    with log_path.open("r", encoding="utf-8") as handle:
        yield from handle


def load_entries(log_path: Path) -> Iterable[ToolLogEntry]:
    """Iterate over parsed tool log entries from the provided file."""

    for line in _iter_lines(log_path):
        line = line.strip()
        if not line:
            continue

        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue

        if data.get("event") != "tool_call":
            continue

        yield ToolLogEntry(
            tool=data.get("tool", "unknown"),
            status=data.get("status", "unknown"),
            request=data.get("request"),
            response=data.get("response"),
            request_characters=int(data.get("request_characters", 0)),
            response_characters=(
                int(data["response_characters"])
                if "response_characters" in data
                else None
            ),
        )


def build_tokenizer(encoding_name: str | None):
    """Build a tokenizer callable from ``tiktoken`` if available."""

    if encoding_name is None:
        return None

    try:
        import tiktoken
    except ModuleNotFoundError:  # pragma: no cover - optional dependency
        raise SystemExit(
            "tiktoken is required for token-based statistics. Install it or omit --tokens."
        )

    encoding = tiktoken.get_encoding(encoding_name)

    def _token_count(text: str) -> int:
        return len(encoding.encode(text))

    return _token_count


def summarize(
    entries: Iterable[ToolLogEntry], use_tokens: bool, encoding_name: str | None
) -> None:
    """Print aggregate statistics per tool."""

    tokenizer = build_tokenizer(encoding_name) if use_tokens else None

    stats: dict[str, dict[str, Any]] = defaultdict(lambda: defaultdict(float))

    for entry in entries:
        tool_stats = stats[entry.tool]
        tool_stats["count"] += 1

        if tokenizer is None:
            tool_stats["request_metric"] += entry.request_characters
            if entry.response_characters is not None:
                tool_stats["response_metric"] += entry.response_characters
                tool_stats["response_max"] = max(
                    tool_stats.get("response_max", 0), entry.response_characters
                )
        else:
            request_tokens = tokenizer(entry.request_text())
            response_tokens = (
                tokenizer(entry.response_text()) if entry.response is not None else 0
            )
            tool_stats["request_metric"] += request_tokens
            tool_stats["response_metric"] += response_tokens
            tool_stats["response_max"] = max(
                tool_stats.get("response_max", 0), response_tokens
            )

    if not stats:
        print("No tool call entries found.")
        return

    metric_label = "Tokens" if tokenizer else "Characters"
    header = f"{'Tool':30} {'Calls':>7} {('Avg Request ' + metric_label):>20} {('Avg Response ' + metric_label):>22} {(metric_label + ' Max'):>12}"
    print(header)
    print("-" * len(header))

    for tool, tool_stats in sorted(stats.items()):
        count = int(tool_stats["count"])
        avg_request = tool_stats["request_metric"] / count if count else 0
        avg_response = tool_stats["response_metric"] / count if count else 0
        max_response = tool_stats.get("response_max", 0)
        print(
            f"{tool:30} {count:7d} {avg_request:20.2f} {avg_response:22.2f} {max_response:12.2f}"
        )


def largest(
    entries: Iterable[ToolLogEntry],
    tool_filter: str | None,
    use_tokens: bool,
    encoding_name: str | None,
) -> None:
    """Print the largest response overall or for a specific tool."""

    tokenizer = build_tokenizer(encoding_name) if use_tokens else None
    best_entry: ToolLogEntry | None = None
    best_value = -1.0

    for entry in entries:
        if tool_filter and entry.tool != tool_filter:
            continue

        if tokenizer is None:
            metric_value = entry.response_characters or 0
        else:
            metric_value = (
                tokenizer(entry.response_text()) if entry.response is not None else 0
            )

        if metric_value > best_value:
            best_value = metric_value
            best_entry = entry

    if best_entry is None:
        print("No matching tool call entries found.")
        return

    metric_label = "tokens" if tokenizer else "characters"
    print(f"Tool: {best_entry.tool}")
    print(f"Status: {best_entry.status}")
    print(f"Largest response ({metric_label}): {best_value}")
    print("Request:")
    print(best_entry.request_text())
    print("Response:")
    print(best_entry.response_text())


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""

    parser = argparse.ArgumentParser(description="Analyze MCP tool call logs")
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "logfile", type=Path, help="Path to log file with TOOL_CALL entries"
    )
    common.add_argument(
        "--tokens",
        action="store_true",
        help="Use token counts instead of character counts (requires tiktoken)",
    )
    common.add_argument(
        "--encoding",
        default="cl100k_base",
        help="Encoding name for token counting (default: cl100k_base)",
    )

    summary_parser = subparsers.add_parser(
        "summary", parents=[common], help="Show per-tool summaries"
    )
    summary_parser.set_defaults(func="summary")

    largest_parser = subparsers.add_parser(
        "largest", parents=[common], help="Show largest response"
    )
    largest_parser.add_argument(
        "--tool", help="Restrict to a specific tool", default=None
    )
    largest_parser.set_defaults(func="largest")

    args = parser.parse_args(argv)

    if args.func == "summary":
        summarize(
            load_entries(args.logfile),
            args.tokens,
            args.encoding if args.tokens else None,
        )
    elif args.func == "largest":
        largest(
            load_entries(args.logfile),
            args.tool,
            args.tokens,
            args.encoding if args.tokens else None,
        )
    else:  # pragma: no cover - defensive programming
        parser.error("Unknown command")

    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())
