"""Unit tests for the ha_query.py failure contract.

``ha_query.py`` is the black-box verification leg of ``/bat-story-eval``: it
asks a live HA instance a question through an agent CLI and the answer is
scored. A CLI that fails after printing something partial used to be
indistinguishable from a real answer, so the script now returns the CLI's exit
code alongside the text and exits non-zero itself. These tests pin that.

The script lives under ``tests/uat/``, which no CI lane runs; loading it by
path from the unit suite is what puts the contract under CI.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2] / "uat" / "stories" / "scripts" / "ha_query.py"
)
spec = importlib.util.spec_from_file_location("ha_query", str(SCRIPT))
assert spec is not None and spec.loader is not None
ha_query = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ha_query)


def _completed(returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["agent"], returncode=returncode, stdout=stdout, stderr=stderr
    )


QUERY_FUNCS = [
    pytest.param(lambda: ha_query.run_gemini_query, id="gemini"),
    pytest.param(lambda: ha_query.run_claude_query, id="claude"),
]


@pytest.mark.parametrize("get_func", QUERY_FUNCS)
def test_successful_query_returns_text_and_zero(get_func):
    """A clean run returns the CLI's stdout and exit code 0, unannotated."""
    with patch.object(
        ha_query.subprocess, "run", return_value=_completed(0, stdout="the answer")
    ):
        text, returncode = get_func()("q", "http://ha.local:8123", "token")

    assert returncode == 0
    assert text == "the answer"
    assert "[exit" not in text


@pytest.mark.parametrize("get_func", QUERY_FUNCS)
def test_failed_query_reports_exit_code_even_without_stderr(get_func):
    """A non-zero exit is surfaced even when the CLI wrote nothing to stderr.

    This is the regression: the old code only annotated the output when stderr
    was non-empty, so a silent failure was consumed as a real answer.
    """
    with patch.object(
        ha_query.subprocess, "run", return_value=_completed(2, stdout="partial")
    ) as mock_run:
        text, returncode = get_func()("q", "http://ha.local:8123", "token")

    assert returncode == 2
    assert "[exit 2]" in text
    # The partial answer is kept — the marker is what makes it non-scorable.
    assert "partial" in text
    # check=False is load-bearing: with check=True the non-zero exit raises
    # CalledProcessError, which these functions do not catch, so neither the
    # annotation above nor the tuple return is ever reached. A return_value
    # mock cannot express that, so assert the kwarg directly.
    assert mock_run.call_args.kwargs["check"] is False


@pytest.mark.parametrize("get_func", QUERY_FUNCS)
def test_failed_query_appends_stderr_when_present(get_func):
    with patch.object(
        ha_query.subprocess,
        "run",
        return_value=_completed(1, stdout="", stderr="boom"),
    ):
        text, returncode = get_func()("q", "http://ha.local:8123", "token")

    assert returncode == 1
    assert "[exit 1]" in text
    assert "boom" in text


@pytest.mark.parametrize("get_func", QUERY_FUNCS)
def test_timed_out_query_is_annotated_and_keeps_its_partial_output(get_func):
    """A hung CLI is a failed query, not a missing one.

    Without this the exception escapes ``main()`` as a traceback: the answer
    is never printed, so the partial output the CLI did manage is lost along
    with the ``[exit N]`` marker the skill docs tell evaluators to look for.
    """
    timeout = subprocess.TimeoutExpired(cmd=["agent"], timeout=120, output=b"partial")
    with patch.object(ha_query.subprocess, "run", side_effect=timeout):
        text, returncode = get_func()("q", "http://ha.local:8123", "token")

    assert returncode == ha_query.TIMEOUT_EXIT
    assert f"[exit {ha_query.TIMEOUT_EXIT}]" in text
    assert "timed out after" in text
    assert "partial" in text


def test_gemini_json_envelope_is_unwrapped():
    """The gemini CLI's JSON envelope still yields the bare response text."""
    with patch.object(
        ha_query.subprocess,
        "run",
        return_value=_completed(0, stdout='{"response": "unwrapped"}'),
    ):
        text, returncode = ha_query.run_gemini_query("q", "http://ha", "token")

    assert (text, returncode) == ("unwrapped", 0)


# Both dispatch arms of main() unpack the new tuple, so both must be driven.
AGENTS = [("gemini", "run_gemini_query"), ("claude", "run_claude_query")]


def _fake_argv(agent: str) -> list[str]:
    return [
        "ha_query.py",
        "--ha-url",
        "http://ha",
        "--ha-token",
        "t",
        "--agent",
        agent,
        "question",
    ]


@pytest.mark.parametrize(("agent", "func_name"), AGENTS)
def test_main_exits_non_zero_when_the_query_failed(
    agent, func_name, monkeypatch, capsys
):
    """A failed query must not exit 0 — that is what let it be scored."""
    monkeypatch.setattr(ha_query.sys, "argv", _fake_argv(agent))
    monkeypatch.setattr(ha_query.shutil, "which", lambda _: f"/usr/bin/{agent}")
    monkeypatch.setattr(ha_query, func_name, lambda *a, **kw: ("partial\n[exit 2]", 2))

    with pytest.raises(SystemExit) as excinfo:
        ha_query.main()

    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    # The answer still reaches stdout — consumers read it regardless.
    assert "partial" in captured.out
    assert "exited with code 2" in captured.err


@pytest.mark.parametrize(("agent", "func_name"), AGENTS)
def test_main_exits_zero_on_success(agent, func_name, monkeypatch, capsys):
    monkeypatch.setattr(ha_query.sys, "argv", _fake_argv(agent))
    monkeypatch.setattr(ha_query.shutil, "which", lambda _: f"/usr/bin/{agent}")
    monkeypatch.setattr(ha_query, func_name, lambda *a, **kw: ("answer", 0))

    ha_query.main()

    assert "answer" in capsys.readouterr().out
