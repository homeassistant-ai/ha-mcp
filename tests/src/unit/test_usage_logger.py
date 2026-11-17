"""Unit tests for the usage logger with ring buffer.

What a tool call leaves in the usage log is reached the way the server
reaches it: ``ha_call_service`` on a real Core records the call, the report
``ha_report_issue`` builds lists it, and the server's own log file under its
data directory holds it once the logger has stopped.
"""

import asyncio
import builtins
import errno
import json
import logging
import os
import stat
import sys
import tempfile
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceCall

from ha_mcp import redaction
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.config import reset_global_settings
from ha_mcp.tools.tools_service import ServiceTools
from ha_mcp.utils.data_paths import get_data_dir
from ha_mcp.utils.usage_logger import (
    AVG_LOG_ENTRIES_PER_TOOL,
    DEFAULT_RING_BUFFER_SIZE,
    UsageLogger,
    get_recent_logs,
    get_usage_logger,
    log_tool_call,
    shutdown_usage_logger,
)

from ._report_issue import report_issue


@pytest.fixture(autouse=True)
def _fresh_usage_logger() -> Iterator[None]:
    """Each test starts the server's usage logger anew, under its own data dir."""
    shutdown_usage_logger()
    yield
    shutdown_usage_logger()


class TestUsageLoggerRingBuffer:
    """Test suite for the UsageLogger ring buffer functionality."""

    @pytest.fixture
    def temp_log_dir(self):
        """Create a temporary directory for log files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield tmpdir

    @pytest.fixture
    def logger(self, temp_log_dir):
        """Create a UsageLogger instance with small ring buffer for testing."""
        log_path = Path(temp_log_dir) / "test_usage.jsonl"
        logger = UsageLogger(str(log_path), ring_buffer_size=10)
        yield logger
        logger.shutdown()

    def test_ring_buffer_stores_entries(self, logger):
        """Test that entries are stored in the ring buffer."""
        logger.log_tool_usage(
            tool_name="ha_test_tool",
            parameters={"key": "value"},
            execution_time_ms=100.0,
            success=True,
        )

        entries = logger.get_recent_entries(10)
        assert len(entries) == 1
        assert entries[0]["tool_name"] == "ha_test_tool"
        assert entries[0]["success"] is True
        assert entries[0]["execution_time_ms"] == 100.0

    def test_ring_buffer_newest_first(self, logger):
        """Test that entries are returned newest-first."""
        for i in range(3):
            logger.log_tool_usage(
                tool_name=f"ha_tool_{i}",
                parameters={},
                execution_time_ms=float(i * 10),
                success=True,
            )

        entries = logger.get_recent_entries(10)
        assert len(entries) == 3
        # Newest first
        assert entries[0]["tool_name"] == "ha_tool_2"
        assert entries[1]["tool_name"] == "ha_tool_1"
        assert entries[2]["tool_name"] == "ha_tool_0"

    def test_ring_buffer_limit_works(self, logger):
        """Test that requesting fewer entries than available works."""
        for i in range(5):
            logger.log_tool_usage(
                tool_name=f"ha_tool_{i}",
                parameters={},
                execution_time_ms=0,
                success=True,
            )

        entries = logger.get_recent_entries(2)
        assert len(entries) == 2
        # Should get the 2 most recent
        assert entries[0]["tool_name"] == "ha_tool_4"
        assert entries[1]["tool_name"] == "ha_tool_3"

    def test_ring_buffer_overflow(self, logger):
        """Test that ring buffer correctly drops old entries when full."""
        # Logger has ring_buffer_size=10, so add 15 entries
        for i in range(15):
            logger.log_tool_usage(
                tool_name=f"ha_tool_{i}",
                parameters={},
                execution_time_ms=0,
                success=True,
            )

        entries = logger.get_recent_entries(20)
        # Should only have 10 entries (buffer size)
        assert len(entries) == 10
        # Should have entries 5-14 (oldest 0-4 should be dropped)
        assert entries[0]["tool_name"] == "ha_tool_14"  # newest
        assert entries[9]["tool_name"] == "ha_tool_5"  # oldest in buffer

    def test_ring_buffer_thread_safety(self, logger):
        """Test that ring buffer is thread-safe under concurrent access."""
        num_threads = 5
        entries_per_thread = 20
        errors = []

        def writer_thread(thread_id):
            try:
                for i in range(entries_per_thread):
                    logger.log_tool_usage(
                        tool_name=f"ha_thread_{thread_id}_entry_{i}",
                        parameters={},
                        execution_time_ms=0,
                        success=True,
                    )
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def reader_thread():
            try:
                for _ in range(50):
                    _ = logger.get_recent_entries(5)
                    time.sleep(0.001)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = []
        # Start writer threads
        for i in range(num_threads):
            t = threading.Thread(target=writer_thread, args=(i,))
            threads.append(t)
            t.start()

        # Start reader threads
        for _ in range(3):
            t = threading.Thread(target=reader_thread)
            threads.append(t)
            t.start()

        # Wait for all threads
        for t in threads:
            t.join(timeout=5.0)

        # No errors should have occurred
        assert len(errors) == 0

    def test_ring_buffer_empty(self, logger):
        """Test getting entries from empty buffer."""
        entries = logger.get_recent_entries(10)
        assert entries == []

    def test_ring_buffer_with_error_entries(self, logger):
        """Test that error entries are properly stored."""
        logger.log_tool_usage(
            tool_name="ha_failing_tool",
            parameters={"entity_id": "light.test"},
            execution_time_ms=50.0,
            success=False,
            error_message="Entity not found",
        )

        entries = logger.get_recent_entries(1)
        assert len(entries) == 1
        assert entries[0]["success"] is False
        assert entries[0]["error_message"] == "Entity not found"


class TestUsageLoggerDefaults:
    """Test UsageLogger default behavior."""

    @pytest.fixture(autouse=True)
    def _reset_data_dir_cache(self):
        get_data_dir.cache_clear()
        yield
        get_data_dir.cache_clear()

    def test_default_log_path(self, monkeypatch, tmp_path):
        """Default log path lives under ``~/.ha-mcp/logs/`` when no
        overrides are in play."""
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.delenv("HA_MCP_CONFIG_DIR", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        logger = UsageLogger()
        assert logger.log_file_path == tmp_path / ".ha-mcp" / "logs" / "mcp_usage.jsonl"
        logger.shutdown()

    def test_honors_ha_mcp_config_dir(self, monkeypatch, tmp_path):
        """``HA_MCP_CONFIG_DIR`` redirects logs the same way it redirects
        the settings-UI tool config — the two share ``get_data_dir()``."""
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        custom = tmp_path / "custom"
        monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(custom))
        logger = UsageLogger()
        assert logger.log_file_path == custom / "logs" / "mcp_usage.jsonl"
        logger.shutdown()


def test_shutdown_wakes_the_idle_writer_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An idle writer used to sleep out a full 0.1 s poll on a fresh Event
    before it saw the stop flag, so every shutdown (each server stop, each
    unit test) waited. It now idles on the stop event itself.

    The writer's waits are made to last until their event is set: one on
    the stop event ends at ``shutdown()``, one on any other never does."""
    before = set(threading.enumerate())
    logger = UsageLogger(str(tmp_path / "usage.jsonl"))
    (writer,) = set(threading.enumerate()) - before
    real_wait = threading.Event.wait
    idling = threading.Event()

    def wait_until_set(self: threading.Event, timeout: float | None = None) -> bool:
        if threading.current_thread() is not writer:
            return real_wait(self, timeout)
        idling.set()
        return real_wait(self, None)

    monkeypatch.setattr(threading.Event, "wait", wait_until_set)
    assert idling.wait(timeout=5)
    logger.shutdown()

    assert not writer.is_alive()


def test_calls_logged_just_before_shutdown_reach_the_disk(tmp_path: Path) -> None:
    """``shutdown()`` used to stop the writer at the stop flag with entries
    still queued, so calls logged just before it never reached the log file."""
    log_path = tmp_path / "usage.jsonl"
    logger = UsageLogger(str(log_path))
    for n in range(20):
        logger.log_tool_usage(
            tool_name=f"ha_tool_{n}", parameters={}, execution_time_ms=1.0, success=True
        )
    logger.shutdown()

    written = [
        json.loads(line)["tool_name"] for line in log_path.read_text().splitlines()
    ]
    assert written == [f"ha_tool_{n}" for n in range(20)]


class TestUsageLoggerConstants:
    """Test constants are properly defined."""

    def test_avg_log_entries_per_tool(self):
        """Test that AVG_LOG_ENTRIES_PER_TOOL is a reasonable value."""
        assert AVG_LOG_ENTRIES_PER_TOOL >= 1
        assert AVG_LOG_ENTRIES_PER_TOOL <= 10

    def test_default_ring_buffer_size(self):
        """Test that DEFAULT_RING_BUFFER_SIZE is reasonable."""
        assert DEFAULT_RING_BUFFER_SIZE >= 50
        assert DEFAULT_RING_BUFFER_SIZE <= 1000


class TestUsageLoggerGlobalFunctions:
    """Test module-level convenience functions."""

    def test_get_recent_logs_returns_list(self):
        """Test that get_recent_logs returns a list."""
        logs = get_recent_logs(10)
        assert isinstance(logs, list)

    def test_log_tool_call_and_retrieve(self):
        """Test logging via global function and retrieving."""
        log_tool_call(
            tool_name="ha_global_test",
            parameters={"test": True},
            execution_time_ms=25.0,
            success=True,
        )

        logs = get_recent_logs(5)
        assert len(logs) >= 1
        # Find our entry (there might be others from previous tests)
        our_entry = next(
            (entry for entry in logs if entry["tool_name"] == "ha_global_test"), None
        )
        assert our_entry is not None
        assert our_entry["success"] is True


@pytest.fixture
def known_secret(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A secret value the server has seen, with redaction switched on."""
    monkeypatch.setenv("REDACT_SECRETS", "true")
    reset_global_settings()
    remembered = redaction.known_secret_values()
    redaction.clear_known_secret_values()
    redaction.register_known_secret_values(["hunter2-secret"])
    yield "hunter2-secret"
    redaction.clear_known_secret_values()
    redaction.register_known_secret_values(remembered)


class TestLogToolUsageDecorator:
    """Tests for the @log_tool_usage decorator in ha_mcp.tools.helpers."""

    async def test_cancelled_call_is_not_logged_as_success(self):
        """A cancelled tool call never completed, so recording it as a success
        would make the usage log claim a result that was never produced.

        ``CancelledError`` skips ``except Exception``, so the decorator's
        handler must catch ``BaseException`` for the ``finally`` log to carry
        ``success: False``; ``str()`` is empty on a bare ``CancelledError``, so
        the class name stands in as the error message.
        """
        from ha_mcp.tools.helpers import log_tool_usage

        @log_tool_usage
        async def ha_cancelled_tool():
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await ha_cancelled_tool()

        entry = next(
            e for e in get_recent_logs(5) if e["tool_name"] == "ha_cancelled_tool"
        )
        assert entry["success"] is False
        assert entry["error_message"] == "CancelledError"

    async def test_a_known_secret_in_the_error_is_scrubbed(
        self, known_secret: str
    ) -> None:
        """The usage log keeps the error text, which RedactSecretsMiddleware
        only scrubs on its way to the client."""
        from ha_mcp.tools.helpers import log_tool_usage

        @log_tool_usage
        async def ha_failing_tool() -> None:
            raise ValueError(f"login failed for {known_secret}")

        with pytest.raises(ValueError, match=known_secret):
            await ha_failing_tool()

        entry = next(
            e for e in get_recent_logs(5) if e["tool_name"] == "ha_failing_tool"
        )
        assert entry["error_message"] == "login failed for <redacted>"


@pytest.fixture
def service_tools(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> ServiceTools:
    """``ha_call_service`` on a Core whose ``test.echo`` takes any data."""

    async def echo(call: ServiceCall) -> None:
        """A service without a schema, as many integrations register one."""

    hass.services.async_register("test", "echo", echo)
    return ServiceTools(core_client, None)


async def _call(service_tools: ServiceTools, data: dict[str, Any]) -> None:
    """``ha_call_service(data=data)``, recorded as every tool call is."""
    await service_tools.ha_call_service(domain="test", service="echo", data=data)


Reported = Callable[[dict[str, Any]], Awaitable[Any]]


@pytest.fixture
def reported(service_tools: ServiceTools, core_client: HomeAssistantClient) -> Reported:
    """What a bug report lists as the ``data`` of ``ha_call_service(data=...)``."""

    async def report(data: dict[str, Any]) -> Any:
        await _call(service_tools, data)
        listed = (await report_issue(core_client, fields="recent_logs"))["recent_logs"]
        assert listed[0]["tool_name"] == "ha_call_service"
        return listed[0]["parameters"]["data"]

    return report


def _shown(logged: Any, *values: str) -> list[str]:
    """Which of ``values`` the logged arguments still show."""
    dumped = json.dumps(logged)
    return [value for value in values if value in dumped]


def _log_file() -> Path:
    """The server's own usage log."""
    return get_data_dir() / "logs" / "mcp_usage.jsonl"


async def _stopped_log() -> str:
    """Stop the usage logger, as the server does on exit, and read its file."""
    await asyncio.to_thread(shutdown_usage_logger)
    return await asyncio.to_thread(_log_file().read_text)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _plant_file(path: Path, mode: int) -> None:
    """An existing log file of ``mode``, in a logs directory of this user."""
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_text("")
    path.chmod(mode)


class TestParameterRedaction:
    """Secrets in a call's arguments never reach the usage log (GHSA-mc92-ww4q-6fg4).

    ha_report_issue publishes the log and mcp_usage.jsonl keeps it in plain
    text; each call goes through ``ha_call_service``'s ``data``."""

    async def test_nested_code_redacted_sibling_preserved(
        self, reported: Reported
    ) -> None:
        """ha_call_service(data={'code': ...}) leaks the code via the log; the
        recursive key match must mask it while keeping debug-useful siblings."""
        logged = await reported(
            {"code": "code-4321", "entity_id": "lock.front", "brightness": 255}
        )
        assert _shown(logged, "code-4321") == []
        assert sorted(logged) == ["brightness", "code", "entity_id"]
        assert logged["entity_id"] == "lock.front"
        assert logged["brightness"] == 255

    async def test_code_inside_a_json_string_redacted(self, reported: Reported) -> None:
        """A stringified payload, such as an MQTT one, carries the code as text
        the key match cannot see."""
        logged = await reported(
            {
                "payload": '{"code": "code-4321", "entity_id": "lock.front"}',
                "extra": {"payload": '{"code": "code-9876"}'},
            }
        )
        assert _shown(logged, "code-4321", "code-9876") == []
        assert logged["payload"]["entity_id"] == "lock.front"

    @pytest.mark.parametrize(
        "truncated",
        [
            '{"code": "4321"',
            '[{"code": "4321"}',
            '[ [{"code": "4321"',
            '[0, {"code": "4321"}',
            '[{{"code":"4321"}]',
            'note {\\"code\\": \\"4321\\"}',
        ],
    )
    async def test_code_inside_unparsed_json_text_left_out(
        self, reported: Reported, truncated: str
    ) -> None:
        """Text that names a sensitive key as a JSON member but does not
        parse, whatever is wrong with it, still shows the code."""
        assert _shown(await reported({"payload": truncated}), "4321") == []

    async def test_unparsed_json_without_a_sensitive_key_stays_readable(
        self, reported: Reported
    ) -> None:
        """A broken payload is what a report reader debugs with; only one
        that names a sensitive key has to go."""
        data = {"payload": '{"entity_id": "light.kitchen", "brightness": 255'}
        assert await reported(data) == data

    async def test_templates_and_bracketed_text_stay_readable(
        self, reported: Reported
    ) -> None:
        """A Jinja template and a "[tag] message" start like JSON but are not;
        they are what a report reader debugs with."""
        data = {
            "template": "{{ states('sensor.x') }}",
            "block": "{% if a %}b{% endif %}",
            "list": "[{{ a }}, {{ b }}",
            "message": "[Alert] front door open",
        }
        assert await reported(data) == data

    async def test_credential_keys_redacted(self, reported: Reported) -> None:
        secrets = {
            "token": "tok-1",
            "password": "pw-2",
            "secret": "sec-3",
            "api_key": "key-4",
        }
        logged = await reported(secrets)
        assert sorted(logged) == sorted(secrets)
        assert _shown(logged, *secrets.values()) == []

    async def test_suffix_match_redacts_token_family(self, reported: Reported) -> None:
        logged = await reported({"auth_token": "tok-5", "device_secret": "sec-6"})
        assert _shown(logged, "tok-5", "sec-6") == []

    async def test_non_secret_code_key_suffixes_preserved(
        self, reported: Reported
    ) -> None:
        """status_code / cache_key are NOT secrets — _code/_key are deliberately
        excluded from suffix matching so debug value survives."""
        data = {"status_code": 500, "cache_key": "abc"}
        assert await reported(data) == data

    async def test_assist_sentence_redacted(self, reported: Reported) -> None:
        """ha_manage_pipeline(action='process') carries user speech, which
        ha_report_issue would otherwise publish verbatim."""
        logged = await reported(
            {"action": "process", "sentence": "unlock the front door for Alex"}
        )
        assert _shown(logged, "front door") == []
        assert logged["action"] == "process"

    async def test_case_insensitive(self, reported: Reported) -> None:
        logged = await reported({"CODE": "code-1234", "Token": "tok-7"})
        assert _shown(logged, "code-1234", "tok-7") == []

    async def test_benign_params_unchanged(self, reported: Reported) -> None:
        data = {"entity_id": "light.test", "key": "value", "nested": {"a": 1}}
        assert await reported(data) == data

    async def test_list_of_dicts_recursed(self, reported: Reported) -> None:
        logged = await reported({"items": [{"code": "code-1"}, {"entity_id": "x"}]})
        assert _shown(logged, "code-1") == []
        assert logged["items"][1] == {"entity_id": "x"}

    async def test_fail_closed_past_max_depth(self, reported: Reported) -> None:
        """A subtree nested past the recursion guard is masked wholesale, never
        returned verbatim, whatever its keys are called."""
        deep: dict[str, Any] = {"note": "deep-value"}
        for _ in range(20):  # far deeper than the logger walks
            deep = {"next": deep}
        logged = await reported({"shallow": "kept", "deep": deep})
        assert logged["shallow"] == "kept"
        assert _shown(logged, "deep-value") == []

    async def test_does_not_mutate_input(self, reported: Reported) -> None:
        """log_tool_usage receives the live kwargs dict — redaction must copy."""
        data = {"code": "code-4321"}
        await reported(data)
        assert data == {"code": "code-4321"}

    async def test_secrets_not_persisted_to_jsonl(
        self, service_tools: ServiceTools
    ) -> None:
        """The on-disk mcp_usage.jsonl must not contain raw secrets either — the
        redacted entry is what gets queued for the background disk write."""
        await _call(service_tools, {"code": "code-4321", "entity_id": "lock.front"})

        content = await _stopped_log()

        assert "code-4321" not in content
        [entry] = [json.loads(line) for line in content.splitlines()]
        assert sorted(entry["parameters"]["data"]) == ["code", "entity_id"]
        assert entry["parameters"]["data"]["entity_id"] == "lock.front"


def _disk_disabled_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "ha_mcp.utils.usage_logger" and r.levelno == logging.WARNING
    ]


def _watch_log_opens(
    patched: pytest.MonkeyPatch, log_path: Path, error: OSError | None = None
) -> threading.Event:
    """Set the returned event once the writer has tried to open ``log_path``;
    with ``error``, that open raises it instead."""
    tried = threading.Event()
    real_open = os.open

    def watched(
        path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        if str(path) != str(log_path):
            return real_open(path, flags, mode, dir_fd=dir_fd)
        try:
            if error is not None:
                raise error
            return real_open(path, flags, mode, dir_fd=dir_fd)
        finally:
            tried.set()

    patched.setattr(os, "open", watched)
    return tried


class TestLogFileMode:
    """mcp_usage.jsonl records tool arguments and results, so only the owner reads it."""

    @pytest.fixture
    def permissive_umask(self) -> Iterator[None]:
        """Run under the common ``022`` umask, which leaves new files ``0644``."""
        previous = os.umask(0o022)
        try:
            yield
        finally:
            os.umask(previous)

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    @pytest.mark.usefixtures("permissive_umask")
    async def test_new_log_file_is_owner_only(
        self, service_tools: ServiceTools
    ) -> None:
        await _call(service_tools, {"marker": "ha_written"})
        await _stopped_log()
        log_path = _log_file()
        assert await asyncio.to_thread(_mode, log_path) == 0o600
        assert await asyncio.to_thread(_mode, log_path.parent) == 0o700

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    async def test_existing_world_readable_log_is_tightened(
        self, service_tools: ServiceTools
    ) -> None:
        log_path = _log_file()
        await asyncio.to_thread(_plant_file, log_path, 0o644)
        await _call(service_tools, {"marker": "ha_written"})
        await _stopped_log()
        assert await asyncio.to_thread(_mode, log_path) == 0o600

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    async def test_log_that_cannot_be_tightened_stops_file_writes_and_names_the_file(
        self,
        service_tools: ServiceTools,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        log_path = _log_file()
        await asyncio.to_thread(_plant_file, log_path, 0o644)

        def refuse(self: Path, mode: int) -> None:
            raise PermissionError("not the owner")

        monkeypatch.setattr(Path, "chmod", refuse)
        caplog.set_level(logging.WARNING, logger="ha_mcp.utils.usage_logger")
        await _call(service_tools, {"marker": "ha_written"})
        content = await _stopped_log()
        # One warning, at startup: the file is never tried.
        [warning] = _disk_disabled_warnings(caplog)
        # The directory exists and is writable; only the file's mode is at fault,
        # so the operator must be pointed at the file, not told to fix a mkdir.
        assert str(log_path) in warning
        assert "could not create" not in warning
        assert content == ""


class TestLogPathControlledByOthers:
    """Another local user must not be able to redirect or read the usage log."""

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX owner and modes")
    async def test_log_directory_owned_by_another_user_stops_file_writes(
        self, service_tools: ServiceTools, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        log_path = _log_file()
        await asyncio.to_thread(log_path.parent.mkdir)
        real_euid = os.geteuid()
        monkeypatch.setattr(os, "geteuid", lambda: real_euid + 1)
        await _call(service_tools, {"marker": "ha_written"})
        await asyncio.to_thread(shutdown_usage_logger)
        assert not await asyncio.to_thread(log_path.exists)

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX owner and modes")
    async def test_refused_log_file_keeps_recent_calls_for_bug_reports(
        self,
        service_tools: ServiceTools,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The server's own usage log, in a logs directory of another user."""
        await asyncio.to_thread(_log_file().parent.mkdir)
        caplog.set_level(logging.WARNING, logger="ha_mcp.utils.usage_logger")
        real_euid = os.geteuid()
        with monkeypatch.context() as patched:
            patched.setattr(os, "geteuid", lambda: real_euid + 1)
            await _call(service_tools, {"marker": "ha_kept_in_memory"})

        report = await report_issue(core_client, fields="recent_logs")

        assert [e["parameters"]["data"] for e in report["recent_logs"]] == [
            {"marker": "ha_kept_in_memory"}
        ]
        [warning] = _disk_disabled_warnings(caplog)
        assert "owned by another user" in warning

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX owner and modes")
    async def test_world_writable_log_directory_is_closed_to_other_writers(
        self, service_tools: ServiceTools
    ) -> None:
        log_dir = _log_file().parent
        await asyncio.to_thread(log_dir.mkdir)
        await asyncio.to_thread(log_dir.chmod, 0o777)
        await _call(service_tools, {"marker": "ha_written"})
        assert "ha_written" in await _stopped_log()
        assert (
            await asyncio.to_thread(_mode, log_dir) & (stat.S_IWGRP | stat.S_IWOTH) == 0
        )

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
    async def test_symlinked_log_directory_is_refused_and_left_unchanged(
        self, service_tools: ServiceTools
    ) -> None:
        log_dir = _log_file().parent
        target = log_dir.with_name("shared")

        def plant() -> None:
            target.mkdir()
            target.chmod(0o777)
            log_dir.symlink_to(target, target_is_directory=True)

        await asyncio.to_thread(plant)
        await _call(service_tools, {"marker": "ha_written"})
        await asyncio.to_thread(shutdown_usage_logger)
        assert not await asyncio.to_thread((target / _log_file().name).exists)
        assert await asyncio.to_thread(_mode, target) == 0o777

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
    async def test_symlink_planted_before_startup_is_refused(
        self, service_tools: ServiceTools
    ) -> None:
        log_path = _log_file()
        target = log_path.parent.with_name("victim.txt")

        def plant() -> None:
            log_path.parent.mkdir(mode=0o700)
            target.write_text("")
            target.chmod(0o644)
            log_path.symlink_to(target)

        await asyncio.to_thread(plant)
        await _call(service_tools, {"marker": "ha_written"})
        await asyncio.to_thread(shutdown_usage_logger)
        assert await asyncio.to_thread(target.read_text) == ""
        assert await asyncio.to_thread(_mode, target) == 0o644

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX links")
    @pytest.mark.parametrize("link", ["symlink", "hardlink"])
    async def test_log_swapped_after_startup_receives_no_tool_data(
        self, service_tools: ServiceTools, link: str
    ) -> None:
        await asyncio.to_thread(get_usage_logger)
        log_path = _log_file()
        target = log_path.parent.with_name("planted.jsonl")

        def plant() -> None:
            os.close(os.open(target, os.O_CREAT | os.O_WRONLY, 0o600))
            if link == "symlink":
                log_path.symlink_to(target)
            else:
                os.link(target, log_path)

        await asyncio.to_thread(plant)
        for marker in ("ha_first", "ha_second"):
            await _call(service_tools, {"marker": marker})
        await asyncio.to_thread(shutdown_usage_logger)
        assert await asyncio.to_thread(target.read_text) == ""

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX owner and modes")
    @pytest.mark.parametrize("change", ["group_readable", "other_owner"])
    async def test_log_no_longer_private_after_startup_receives_no_tool_data(
        self,
        service_tools: ServiceTools,
        monkeypatch: pytest.MonkeyPatch,
        change: str,
    ) -> None:
        await asyncio.to_thread(get_usage_logger)
        log_path = _log_file()
        await asyncio.to_thread(_plant_file, log_path, 0o600)
        if change == "group_readable":
            await asyncio.to_thread(log_path.chmod, 0o644)
        else:
            real_euid = os.geteuid()
            monkeypatch.setattr(os, "geteuid", lambda: real_euid + 1)
        for marker in ("ha_first", "ha_second"):
            await _call(service_tools, {"marker": marker})
        assert await _stopped_log() == ""

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX FIFOs and modes")
    @pytest.mark.parametrize("swap", ["fifo_without_reader", "unwritable_file"])
    async def test_log_that_cannot_be_opened_after_startup_warns_once(
        self,
        service_tools: ServiceTools,
        caplog: pytest.LogCaptureFixture,
        swap: str,
    ) -> None:
        if swap == "unwritable_file" and os.geteuid() == 0:
            pytest.skip("root bypasses file modes")
        caplog.set_level(logging.WARNING, logger="ha_mcp.utils.usage_logger")
        await asyncio.to_thread(get_usage_logger)
        log_path = _log_file()
        if swap == "fifo_without_reader":
            await asyncio.to_thread(os.mkfifo, log_path, 0o600)
        else:
            await asyncio.to_thread(_plant_file, log_path, 0o000)
        for marker in ("ha_first", "ha_second"):
            await _call(service_tools, {"marker": marker})
        await asyncio.to_thread(shutdown_usage_logger)
        [warning] = _disk_disabled_warnings(caplog)
        assert str(log_path) in warning

    async def test_log_directory_removed_and_recreated_resumes_writing(
        self,
        service_tools: ServiceTools,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.WARNING, logger="ha_mcp.utils.usage_logger")
        await asyncio.to_thread(get_usage_logger)
        log_path = _log_file()
        with monkeypatch.context() as patched:
            tried = _watch_log_opens(patched, log_path)
            await asyncio.to_thread(log_path.parent.rmdir)
            await _call(service_tools, {"marker": "ha_while_gone"})
            assert await asyncio.to_thread(tried.wait, 5)
        await asyncio.to_thread(log_path.parent.mkdir, 0o700)
        await _call(service_tools, {"marker": "ha_after_recreate"})
        content = await _stopped_log()
        assert "ha_after_recreate" in content
        assert "ha_while_gone" not in content
        assert _disk_disabled_warnings(caplog) == []

    @pytest.mark.parametrize("err", [errno.EMFILE, errno.ENFILE])
    async def test_out_of_descriptors_keeps_the_log_file_on(
        self, service_tools: ServiceTools, monkeypatch: pytest.MonkeyPatch, err: int
    ) -> None:
        log_path = _log_file()
        with monkeypatch.context() as patched:
            tried = _watch_log_opens(patched, log_path, OSError(err, os.strerror(err)))
            await _call(service_tools, {"marker": "ha_while_exhausted"})
            assert await asyncio.to_thread(tried.wait, 5)
        await _call(service_tools, {"marker": "ha_after_exhausted"})
        content = await _stopped_log()
        assert "ha_after_exhausted" in content
        assert "ha_while_exhausted" not in content

    async def test_descriptor_is_closed_when_the_file_object_cannot_be_made(
        self, service_tools: ServiceTools, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        log_path = _log_file()
        opened: list[int] = []
        closed: list[int] = []
        real_open = os.open
        real_close = os.close
        real_file_open = builtins.open

        def tracking_open(
            path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
        ) -> int:
            fd = real_open(path, flags, mode, dir_fd=dir_fd)
            if str(path) == str(log_path):
                opened.append(fd)
            return fd

        def failing_open(file: Any, *args: Any, **kwargs: Any) -> Any:
            if file in opened:
                raise ValueError("cannot wrap the descriptor")
            return real_file_open(file, *args, **kwargs)

        def tracking_close(fd: int) -> None:
            closed.append(fd)
            real_close(fd)

        with monkeypatch.context() as patched:
            patched.setattr(os, "open", tracking_open)
            patched.setattr(os, "close", tracking_close)
            patched.setattr(builtins, "open", failing_open)
            await _call(service_tools, {"marker": "ha_written"})
            await asyncio.to_thread(shutdown_usage_logger)
        assert len(opened) == 1
        assert opened[0] in closed
