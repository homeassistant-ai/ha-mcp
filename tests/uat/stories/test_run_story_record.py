"""Unit tests for run_story.append_result token-record threading."""

from __future__ import annotations

import json
from pathlib import Path

from uat.stories.run_story import _extract_model, append_result

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
