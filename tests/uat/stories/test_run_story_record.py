"""Unit tests for run_story.append_result token-record threading."""

from __future__ import annotations

import json
from pathlib import Path

from uat.stories.run_story import append_result

_STORY = {"id": "s01", "category": "automation", "weight": 5}


def _bat_summary(test_phase: dict) -> dict:
    return {"agents": {"openai": {"test": test_phase, "aggregate": {}}}}


def _write_and_read(
    tmp_path: Path,
    test_phase: dict,
    *,
    model: str | None = None,
    quantization: str | None = None,
) -> dict:
    results_file = tmp_path / "results.jsonl"
    append_result(
        results_file,
        _STORY,
        "openai",
        sha="abc123",
        describe="test",
        branch=None,
        bat_summary=_bat_summary(test_phase),
        passed=True,
        model=model,
        quantization=quantization,
    )
    return json.loads(results_file.read_text().splitlines()[-1])


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
    """An explicit model arg (subprocess agents) lands in the record."""
    record = _write_and_read(tmp_path, {}, model="sonnet")
    assert record["model"] == "sonnet"


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
