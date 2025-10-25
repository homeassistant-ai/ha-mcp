"""Validation tests for tool logging artifacts emitted during E2E runs."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ha_mcp.logging import LOG_FILENAME
from scripts import tool_log_stats


REQUIRE_LOG = os.getenv("TOOL_LOG_REQUIRED", "0") == "1"
LOG_DIR_ENV = "HOMEASSISTANT_TOOL_LOG_DIR"
DEFAULT_LOG_PATH = Path("artifacts") / LOG_FILENAME


@pytest.fixture(scope="module")
def log_path() -> Path:
    """Resolve the tool log path and ensure it exists when required."""

    env_dir = os.getenv(LOG_DIR_ENV)
    path = (Path(env_dir) / LOG_FILENAME) if env_dir else DEFAULT_LOG_PATH

    if path.exists():
        return path

    message = (
        "Tool log file not found. Expected E2E pytest run with "
        f"{LOG_DIR_ENV} set to the logging directory to populate '{path}'."
    )

    if REQUIRE_LOG:
        pytest.fail(message)

    pytest.skip(message)


@pytest.fixture(scope="module")
def log_entries(log_path: Path) -> list[tool_log_stats.ToolLogEntry]:
    """Load parsed tool log entries from the artifact file."""

    entries = list(tool_log_stats.load_entries(log_path))
    if not entries:
        if REQUIRE_LOG:
            pytest.fail(
                "Tool log file was present but no tool_call entries were detected."
            )
        pytest.skip("Tool log present but contained no tool_call entries.")

    return entries


@pytest.mark.integration
@pytest.mark.e2e
def test_tool_log_contains_requests(
    log_entries: list[tool_log_stats.ToolLogEntry],
) -> None:
    """Ensure at least one tool invocation was captured."""

    assert any(entry.request_characters > 0 for entry in log_entries)


@pytest.mark.integration
@pytest.mark.e2e
def test_tool_log_records_successful_tools(
    log_entries: list[tool_log_stats.ToolLogEntry],
) -> None:
    """Verify the server emitted successful tool responses into the log."""

    assert any(entry.status == "success" for entry in log_entries)


@pytest.mark.integration
@pytest.mark.e2e
def test_tool_log_stats_summary_executes(
    log_entries: list[tool_log_stats.ToolLogEntry], capsys: pytest.CaptureFixture[str]
) -> None:
    """Ensure the statistics helper runs without errors on captured entries."""

    tool_log_stats.summarize(log_entries, use_tokens=False, encoding_name=None)
    captured = capsys.readouterr().out

    assert "Tool" in captured
    assert "Calls" in captured


@pytest.mark.integration
@pytest.mark.e2e
def test_tool_log_stats_largest_executes(
    log_entries: list[tool_log_stats.ToolLogEntry], capsys: pytest.CaptureFixture[str]
) -> None:
    """Ensure largest-response helper operates on captured entries."""

    tool_log_stats.largest(
        log_entries,
        tool_filter=None,
        use_tokens=False,
        encoding_name=None,
    )
    captured = capsys.readouterr().out

    assert "Largest response" in captured
