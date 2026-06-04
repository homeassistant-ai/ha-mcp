"""Unit tests for run_story.append_result token-record threading."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from uat.openai_agent import ContextWindowExceededError
from uat.stories.run_story import (
    _compute_passed,
    _extract_model,
    _run_completed,
    _run_test_prompt_inline,
    _suite_exit_code,
    append_abort_marker,
    append_result,
)

_STORY = {"id": "s01", "category": "automation", "weight": 5}


def _bat_summary(test_phase: dict, agent: str = "openai") -> dict:
    return {"agents": {agent: {"test": test_phase, "aggregate": {}}}}


def _write_and_read(
    tmp_path: Path,
    test_phase: dict,
    *,
    agent: str = "openai",
    model: str | None = None,
    quantization: str | None = None,
    session_file: str | None = None,
) -> dict:
    results_file = tmp_path / "results.jsonl"
    append_result(
        results_file,
        _STORY,
        agent,
        sha="abc123",
        describe="test",
        branch=None,
        bat_summary=_bat_summary(test_phase, agent),
        passed=True,
        model=model,
        quantization=quantization,
        session_file=session_file,
    )
    return json.loads(results_file.read_text().splitlines()[-1])


def _write_claude_session(tmp_path: Path, model: str = "claude-sonnet-4-6") -> str:
    """Write a minimal claude session JSONL with one assistant entry."""
    sf = tmp_path / "session.jsonl"
    sf.write_text(json.dumps({"type": "assistant", "message": {"model": model}}) + "\n")
    return str(sf)


def test_tokens_thoughts_threaded_into_record(tmp_path):
    """tokens_thoughts from the phase summary lands in the JSONL record."""
    record = _write_and_read(
        tmp_path,
        {"tokens_input": 100, "tokens_output": 235, "tokens_thoughts": 220},
    )
    assert record["tokens_thoughts"] == 220
    # reasoning is a subset of output, so billable must not double-count it.
    assert record["tokens_billable"] == 100 + 235


def test_tokens_thoughts_defaults_to_zero_when_absent(tmp_path):
    """A phase summary without tokens_thoughts records 0, not a crash."""
    record = _write_and_read(
        tmp_path,
        {"tokens_input": 100, "tokens_output": 50},
    )
    assert record["tokens_thoughts"] == 0


def test_model_from_call_site_param(tmp_path):
    """An explicit model= argument lands in the record."""
    record = _write_and_read(tmp_path, {}, model="sonnet")
    assert record["model"] == "sonnet"


def test_model_call_site_arg_wins_over_test_phase(tmp_path):
    """Both present: the explicit arg wins, pinning the OR order (live inline case)."""
    record = _write_and_read(tmp_path, {"model": "phase-model"}, model="arg-model")
    assert record["model"] == "arg-model"


def test_model_from_test_phase(tmp_path):
    """The inline path stamps the resolved model into test_phase; it is used."""
    record = _write_and_read(tmp_path, {"model": "qwen3.6-27b"})
    assert record["model"] == "qwen3.6-27b"


def test_model_defaults_to_none_when_absent(tmp_path):
    """No model anywhere records a present-but-None key, not a crash."""
    record = _write_and_read(tmp_path, {})
    assert "model" in record
    assert record["model"] is None


def test_quantization_threaded_into_record(tmp_path):
    """An explicit quantization arg lands in the record."""
    record = _write_and_read(tmp_path, {}, quantization="Q4_K_M")
    assert record["quantization"] == "Q4_K_M"


def test_quantization_defaults_to_none_when_absent(tmp_path):
    """No quantization records a present-but-None key, not a crash."""
    record = _write_and_read(tmp_path, {})
    assert "quantization" in record
    assert record["quantization"] is None


def test_extract_model_reads_claude_session(tmp_path):
    """Claude's resolved model id comes from message.model in the session file."""
    sf = _write_claude_session(tmp_path, "claude-sonnet-4-6")
    assert _extract_model(sf, "claude") == "claude-sonnet-4-6"


def test_extract_model_none_on_malformed_session(tmp_path):
    """A corrupt session line degrades to None, never aborting the record write."""
    sf = tmp_path / "session.jsonl"
    sf.write_text("{not valid json\n")
    assert _extract_model(str(sf), "claude") is None


def test_model_from_claude_session_fallback(tmp_path):
    """Bare claude run (no model arg, no test_phase model) records the session id."""
    sf = _write_claude_session(tmp_path, "claude-sonnet-4-6")
    record = _write_and_read(tmp_path, {}, agent="claude", session_file=sf)
    assert record["model"] == "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# Context-overflow hard failure (a crashed run must never be a clean PASS)
# ---------------------------------------------------------------------------
def test_compute_passed_incomplete_run_fails_despite_verify():
    """A crashed run (completed=False) fails even when every ha_check passed.

    This is the core un-masking: verification confirms end state, but if the
    agent never finished, it isn't a clean pass.
    """
    assert (
        _compute_passed(
            exit_code=0,
            tool_calls=5,
            verify_results=[{"passed": True}],
            completed=False,
        )
        is False
    )


def test_compute_passed_completed_run_still_uses_verify():
    """A completed run is scored by verify as before (no behavior change)."""
    assert (
        _compute_passed(
            exit_code=1,
            tool_calls=5,
            verify_results=[{"passed": True}],
            completed=True,
        )
        is True
    )


@pytest.mark.asyncio
async def test_context_overflow_run_not_clean_pass(monkeypatch):
    """End-to-end: an overflow crash flows through the inline path to a FAIL.

    Exercises the real seam — the inline failure summary's completed flag and
    its consumption by _compute_passed — so a wiring mistake that re-introduces
    the mask is caught.
    """

    async def _raise(*_args, **_kwargs):
        raise ContextWindowExceededError(27220, 24576)

    monkeypatch.setattr("uat.openai_agent.run_scenario_inline", _raise)

    rc, summary = await _run_test_prompt_inline(
        "create an automation",
        agent_name="openai",
        openai_client=object(),
        mcp_client=object(),
        model="qwen3.6",
        openai_tools=[],
    )

    assert rc == 1
    test_phase = summary["agents"]["openai"]["test"]
    assert test_phase["completed"] is False
    assert test_phase["error"] == "context_window_exceeded"
    assert test_phase["context_overflow"] == {
        "requested_tokens": 27220,
        "context_size": 24576,
    }

    # Verification passes (entity created before the overflow), but the run
    # crashed mid-conversation, so it must NOT be scored as a clean pass.
    passed = _compute_passed(
        exit_code=rc,
        tool_calls=5,
        verify_results=[{"passed": True}],
        completed=test_phase.get("completed", True),
    )
    assert passed is False


def test_context_overflow_fields_recorded(tmp_path):
    """error and context_overflow are surfaced in the JSONL record."""
    record = _write_and_read(
        tmp_path,
        {
            "error": "context_window_exceeded",
            "context_overflow": {"requested_tokens": 27220, "context_size": 24576},
        },
    )
    assert record["error"] == "context_window_exceeded"
    assert record["context_overflow"] == {
        "requested_tokens": 27220,
        "context_size": 24576,
    }


def test_clean_record_omits_error_fields(tmp_path):
    """A normal run carries no error/context_overflow clutter."""
    record = _write_and_read(tmp_path, {"tokens_input": 10, "tokens_output": 5})
    assert "error" not in record
    assert "context_overflow" not in record


# ---------------------------------------------------------------------------
# _run_completed: the fail-closed seam that feeds _compute_passed's guard.
# Three reviewers flagged the subprocess crash (summary=None) path as the same
# masking class the inline fix closes, so pin it directly.
# ---------------------------------------------------------------------------
def test_run_completed_uses_explicit_flag():
    """An explicit completed flag in test_phase wins over the fallback."""
    assert _run_completed({"completed": True}, {"agents": {}}, 1) is True
    assert _run_completed({"completed": False}, {"agents": {}}, 0) is False


def test_run_completed_fail_closed_on_missing_summary():
    """A subprocess crash (summary None → empty test_phase) is never completed.

    Fail-closed even on exit_code 0, so a crash can't ride a passing ha_check to
    a green PASS on state left from an earlier turn.
    """
    assert _run_completed({}, None, 1) is False
    assert _run_completed({}, None, 0) is False


def test_run_completed_present_summary_missing_flag_uses_exit_code():
    """Defensive fallback: summary present but no completed key defers to exit code."""
    assert _run_completed({}, {"agents": {}}, 0) is True
    assert _run_completed({}, {"agents": {}}, 1) is False


@pytest.mark.asyncio
async def test_context_overflow_degraded_counts_still_fails(monkeypatch):
    """Overflow with unparseable counts (None, None) still hard-fails.

    The safety net the design relies on: even when token counts can't be
    parsed, the run is marked incomplete and cannot be masked by passing checks.
    """

    async def _raise(*_args, **_kwargs):
        raise ContextWindowExceededError(None, None)

    monkeypatch.setattr("uat.openai_agent.run_scenario_inline", _raise)

    _rc, summary = await _run_test_prompt_inline(
        "create an automation",
        agent_name="openai",
        openai_client=object(),
        mcp_client=object(),
        model="qwen3.6",
        openai_tools=[],
    )

    test_phase = summary["agents"]["openai"]["test"]
    assert test_phase["completed"] is False
    assert test_phase["context_overflow"] == {
        "requested_tokens": None,
        "context_size": None,
    }
    passed = _compute_passed(
        exit_code=1,
        tool_calls=5,
        verify_results=[{"passed": True}],
        completed=_run_completed(test_phase, summary, 1),
    )
    assert passed is False


# ---------------------------------------------------------------------------
# Backend-unreachable fail-fast: a connection error (LLM gone, retries
# exhausted) must PROPAGATE out of the inline path so the story loop can abort,
# rather than being swallowed into a per-story FAIL summary that lets every
# remaining story time out.
# ---------------------------------------------------------------------------
async def _run_inline_with_raise(monkeypatch, exc):
    async def _raise(*_args, **_kwargs):
        raise exc

    monkeypatch.setattr("uat.openai_agent.run_scenario_inline", _raise)
    return await _run_test_prompt_inline(
        "create an automation",
        agent_name="openai",
        openai_client=object(),
        mcp_client=object(),
        model="qwen3.6",
        openai_tools=[],
    )


@pytest.mark.asyncio
async def test_backend_connection_error_propagates(monkeypatch):
    """An APIConnectionError is not swallowed into a FAIL summary; it propagates."""
    import httpx
    import openai

    req = httpx.Request("POST", "http://x/v1/chat/completions")
    with pytest.raises(openai.APIConnectionError):
        await _run_inline_with_raise(
            monkeypatch, openai.APIConnectionError(request=req)
        )


@pytest.mark.asyncio
async def test_backend_timeout_propagates_as_connection_error(monkeypatch):
    """APITimeoutError (an APIConnectionError subclass) propagates the same way.

    The loop catches APIConnectionError, so a timeout must be catchable as one.
    """
    import httpx
    import openai

    req = httpx.Request("POST", "http://x/v1/chat/completions")
    with pytest.raises(openai.APIConnectionError):
        await _run_inline_with_raise(monkeypatch, openai.APITimeoutError(request=req))


def _row(passed: bool) -> tuple:
    """A minimal all_results entry: (agent, sid, story, rc, summary, session, passed)."""
    return ("openai", "s01", {}, 0, {}, None, passed)


def test_suite_exit_code_abort_is_never_success():
    """A backend abort returns nonzero even when every story that ran passed.

    This is the invariant the fail-fast exists for: an incomplete suite is not a
    clean pass.
    """
    assert _suite_exit_code([_row(True), _row(True)], backend_aborted=True) == 1


def test_suite_exit_code_clean_pass():
    """All ran stories passed and no abort → success."""
    assert _suite_exit_code([_row(True), _row(True)], backend_aborted=False) == 0


def test_suite_exit_code_failure_without_abort():
    """A failed story (no abort) returns nonzero."""
    assert _suite_exit_code([_row(True), _row(False)], backend_aborted=False) == 1


def test_suite_exit_code_abort_with_no_completed_stories():
    """Backend dies on the very first story: empty results + abort still nonzero.

    `any(...)` over [] is False, so without the abort short-circuit this would
    return 0 (success) and re-open the masking bug.
    """
    assert _suite_exit_code([], backend_aborted=True) == 1


def test_abort_marker_is_self_describing(tmp_path):
    """The abort marker records the skip and carries no story/passed keys.

    Absence of story/passed is the contract: bat-story-eval counts per-story
    results by grepping those keys, so the marker must not be miscountable as a
    pass or fail for any story.
    """
    results_file = tmp_path / "results.jsonl"
    append_abort_marker(
        results_file,
        "openai",
        sha="abc123",
        describe="v7.6.0",
        branch=None,
        skipped_stories=["s02", "s03", "s04"],
        detail="APITimeoutError: Request timed out.",
    )
    record = json.loads(results_file.read_text().splitlines()[-1])
    assert record["aborted"] is True
    assert record["error"] == "backend_unreachable"
    assert record["skipped_stories"] == ["s02", "s03", "s04"]
    assert record["detail"] == "APITimeoutError: Request timed out."
    assert record["agent"] == "openai"
    assert "story" not in record
    assert "passed" not in record
