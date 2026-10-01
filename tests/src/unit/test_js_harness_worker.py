"""The JSDOM harness runs every test in one long-lived node process.

These tests pin what that sharing must not cost: one run's page state
reaching the next, and one hung or crashed run breaking every later test on
the same pytest worker.
"""

from __future__ import annotations

import subprocess

import pytest

from . import _js_harness
from ._js_harness import run_script, skip_if_unsupported

skip_if_unsupported()


@pytest.fixture(autouse=True)
def _own_worker():
    """Start and end each test without a harness worker."""
    _js_harness._stop_worker()
    yield
    _js_harness._stop_worker()


def _logged(result: _js_harness.HarnessResult) -> list[str]:
    return [arg for entry in result.console for arg in entry["args"]]


def test_a_page_global_does_not_reach_the_next_run():
    """A script that left state behind would make later tests pass or fail
    depending on which test ran before them on the same worker."""
    run_script("window.leaked = 1; globalThis.alsoLeaked = 1;")

    result = run_script(
        "console.log(typeof window.leaked, typeof globalThis.alsoLeaked);"
    )

    assert _logged(result) == ["undefined", "undefined"]


def test_a_run_that_hangs_times_out_and_the_next_run_still_works():
    """Without a restart, the next test would wait on a node process that
    is still stuck in the previous script."""
    # Start node first, so the timeout below can only come from the hang.
    run_script("")
    with pytest.raises(subprocess.TimeoutExpired):
        run_script("", invoke="while (true) {}", timeout_s=2)

    result = run_script("console.log('after');")

    assert _logged(result) == ["after"]


def test_a_run_that_kills_node_fails_alone_and_the_next_run_still_works():
    """An unhandled rejection ends the node process. That run must fail
    with node's output, and the next run must get a new process."""
    with pytest.raises(AssertionError, match="stray rejection"):
        run_script("Promise.reject(new Error('stray rejection'));")

    result = run_script("console.log('after');")

    assert _logged(result) == ["after"]


def test_node_dying_between_runs_is_reported_not_hidden():
    """Node can exit after a run has replied. Replacing it silently would
    hide that failure; the next run must report it, and the one after must
    work."""
    run_script("console.log('before');")
    worker = _js_harness._worker
    assert worker is not None
    worker._proc.kill()
    worker._proc.wait()

    with pytest.raises(AssertionError, match=r"exited .* between runs"):
        run_script("console.log('reported');")

    result = run_script("console.log('after');")

    assert _logged(result) == ["after"]


def test_many_runs_share_one_node_start(monkeypatch):
    """Starting node and loading jsdom for every run is what made the JSDOM
    tests slow; runs after the first must reuse the process."""
    starts = []
    real_popen = subprocess.Popen

    def counting_popen(*args, **kwargs):
        starts.append(args[0])
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(_js_harness.subprocess, "Popen", counting_popen)

    for _ in range(3):
        run_script("console.log('run');")

    assert len(starts) == 1
