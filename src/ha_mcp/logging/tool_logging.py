"""Asynchronous tool call logging built on FastMCP middleware."""

from __future__ import annotations

import atexit
import json
import logging
import queue
import time
from logging import Handler, Logger
from logging.handlers import QueueHandler, QueueListener
from pathlib import Path
from typing import Any

import zstandard
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.server.middleware.logging import default_serializer
from fastmcp.server.middleware.middleware import CallNext
from fastmcp.tools.tool import ToolResult


class ZstdNDJSONFileHandler(Handler):
    """Logging handler that writes NDJSON entries compressed with Zstandard."""

    def __init__(
        self, path: Path, *, level: int = logging.INFO, compression_level: int = 3
    ) -> None:
        super().__init__(level=level)
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._raw_file = self.path.open("wb")
        self._compressor = zstandard.ZstdCompressor(level=compression_level)
        self._stream_writer = self._compressor.stream_writer(self._raw_file)
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(
        self, record: logging.LogRecord
    ) -> None:  # pragma: no cover - thin wrapper
        try:
            message = self.format(record)
            if not message.endswith("\n"):
                message += "\n"
            self._stream_writer.write(message.encode("utf-8"))
        except Exception:  # pragma: no cover - delegated to logging infrastructure
            self.handleError(record)

    def close(self) -> None:
        try:
            if self._stream_writer is not None:
                self._stream_writer.flush(zstandard.FLUSH_FRAME)
                self._stream_writer.close()
        finally:
            if self._raw_file is not None:
                self._raw_file.close()
        super().close()


class AsyncToolLogManager:
    """Manage asynchronous logging infrastructure for tool call telemetry."""

    def __init__(self, path: Path, *, level: int = logging.INFO) -> None:
        self.path = Path(path)
        self._queue: queue.Queue[logging.LogRecord] = queue.Queue()
        self._queue_handler = QueueHandler(self._queue)
        self._file_handler = ZstdNDJSONFileHandler(self.path, level=level)
        self._listener = QueueListener(
            self._queue,
            self._file_handler,
            respect_handler_level=True,
        )
        self.logger = logging.getLogger("ha_mcp.tool_calls")
        self.logger.setLevel(level)
        self.logger.propagate = False

        # Remove any existing handlers inherited from previous runs to avoid duplicates.
        for handler in list(self.logger.handlers):
            self.logger.removeHandler(handler)

        self.logger.addHandler(self._queue_handler)
        self._listener.start()

        self._shutdown_registered = False
        atexit.register(self.shutdown)
        self._shutdown_registered = True

    def shutdown(self) -> None:
        """Stop asynchronous logging and flush remaining records."""

        if getattr(self, "_listener", None) is None:
            return

        listener = self._listener
        self._listener = None
        listener.stop()

        try:
            self.logger.removeHandler(self._queue_handler)
        finally:
            self._queue_handler.close()
            self._file_handler.close()

        if self._shutdown_registered:
            self._shutdown_registered = False
            try:
                atexit.unregister(self.shutdown)
            except AttributeError:  # pragma: no cover - Python <3.11 compatibility
                pass


class ToolCallLoggingMiddleware(Middleware):
    """FastMCP middleware that captures tool call requests and responses."""

    def __init__(self, logger: Logger) -> None:
        self.logger = logger

    async def on_call_tool(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Any],
    ) -> Any:
        start = time.perf_counter()
        request_payload = self._prepare_request(context)
        request_serialized, request_size = self._serialize_payload(request_payload)
        entry: dict[str, Any] = {
            "event": "tool_call",
            "tool": getattr(context.message, "name", "unknown"),
            "status": "success",
            "request": request_serialized,
            "request_characters": request_size,
        }

        try:
            result = await call_next(context)
        except Exception as exc:
            entry["status"] = "error"
            entry["error"] = repr(exc)
            entry["duration_ms"] = round((time.perf_counter() - start) * 1000, 3)
            self._log_entry(entry)
            raise

        response_payload = self._prepare_response(result)
        response_serialized, response_size = self._serialize_payload(response_payload)
        entry.update(
            {
                "response": response_serialized,
                "response_characters": response_size,
                "duration_ms": round((time.perf_counter() - start) * 1000, 3),
            }
        )
        self._log_entry(entry)
        return result

    def _prepare_request(self, context: MiddlewareContext[Any]) -> dict[str, Any]:
        arguments = getattr(context.message, "arguments", None) or {}
        return {"args": [], "kwargs": arguments}

    def _prepare_response(self, result: Any) -> Any:
        if isinstance(result, ToolResult):
            mcp_result = result.to_mcp_result()
            if isinstance(mcp_result, tuple):
                content, structured = mcp_result
                return {
                    "content": content,
                    "structured_content": structured,
                }
            return {"content": mcp_result}
        return result

    def _serialize_payload(self, payload: Any) -> tuple[Any, int]:
        if payload is None:
            return None, 0

        text: str
        try:
            text = default_serializer(payload)
        except Exception:
            text = json.dumps(payload, ensure_ascii=False, default=repr)

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = (
                payload
                if isinstance(payload, (dict, list, str, int, float, bool))
                else repr(payload)
            )
            text = json.dumps(data, ensure_ascii=False, default=repr)

        return data, len(text)

    def _log_entry(self, entry: dict[str, Any]) -> None:
        self.logger.info(json.dumps(entry, ensure_ascii=False, sort_keys=True))
